# 结果表连接补全的 Schema Linking 机制说明

本文档说明“先表后列 + 不做 mention 提取 + 模糊列二次判定 + 结果表连接补全”的实验变体：得到全部结果列后，对含结果列的表两两配对，用 JEV choice 问题分别判断两侧最可能的连接列，再把连接列加入结果列并去重。

配置：`configs/pipeline_minidev_table_first_result_join.yaml`（`mentions.enabled: false` + `column_select_mode: ambiguous`）。

> 步骤 1、步骤 2、步骤 2b 与 [docs/jev_schema_linking_table_first_ambiguous.md](jev_schema_linking_table_first_ambiguous.md) 相同；该文档描述的“步骤 2 外键 Noul + 强制保留外键列”已在当前代码中移除，由本文的步骤 3 取代。更早的 [docs/jev_schema_linking_table_first.md](jev_schema_linking_table_first.md)、[docs/jev_schema_linking_table_first_no_mention.md](jev_schema_linking_table_first_no_mention.md) 同样早于该改动。

## 1. 与前一机制的差异

| 项目 | 前一机制（ambiguous） | 本实验（result_join） |
|---|---|---|
| 步骤 2 的请求内容 | 列 Score + 本表外键连接 Noul | 只有列 Score |
| 外键候选来源 | schema 的 `adjacent_tables` | 不使用任何表间信息 |
| 连接判定时机 | 列打分同时 | 结果列确定之后 |
| 连接判定范围 | 候选表之间的全部连接 | 只含结果列的表两两配对 |
| 判定问题类型 | `noul`（该连接是否需要） | `choice`（哪一列是连接列） |
| 判定输入 | 全库紧凑 schema + 连接候选 | 该配对两张表的 schema |
| 输出记录 | `stage_join.selected_links` | `stage_join.chosen_columns` |
| 列来源标记 | `forced_foreign_key` | `result_join` |

## 2. 总体流程

```text
NL query + evidence
        │
        ▼
[1] 不抽取 mention；把原始 query 包装为唯一 span
        │
        ▼
[2] 步骤 1：表级 Noul，保留候选表
        │
        ▼
[3] 步骤 2：按表提交，每个候选列 1 个 Score
        │
        ▼
[4] 双阈值分流；模糊列进入步骤 2b 二次判定
        │
        ▼
[5] 得到结果列集合（`linked_columns_union`）
        │
        ▼
[6] 步骤 3：筛选结果表 → 两两配对 → 每对两个 choice 问题
        │
        ▼
[7] 连接列加入结果列并去重
        │
        ▼
linked_columns / linked_columns_union / linked_tables
```

问题数量：

```text
N = T + Σ_t C_t + A + 2P
```

- `T`：全库表数（步骤 1）
- `C_t`：候选表 `t` 的列数（步骤 2；无 mention 时每个 span 覆盖整表列）
- `A`：模糊列数（步骤 2b）；`A = 0` 时跳过
- `P`：结果表配对数，即 `k(k-1)/2`（`k` 为结果表数）；`P = 0` 时跳过步骤 3

## 3. 步骤 3：结果表连接补全

### 3.1 结果表与配对

结果表 = 在 `linked_columns_union` 中至少出现一列的表，按首次出现顺序去重。对 `k` 个结果表生成全部无序配对，`pair_id` 依次为 `p1..pP`。

### 3.2 State

每次运行只有一次 JEV 请求，`pairs` 同时包含全部配对：

```json
{
  "task": "result_table_join_key_choice",
  "instruction": "Every pair below holds two result tables with their full column lists. For each pair, pick from the questioned table the single column that is most likely used to join it with the other table. Answer only the provided questions.",
  "context": {"database": "european_football_2"},
  "pairs": {
    "p1": {
      "left_table": {
        "table": "League",
        "primary_key": ["id"],
        "columns": [
          {"name": "id", "type": "integer"},
          {"name": "name", "type": "text"},
          {"name": "country_id", "type": "integer"}
        ]
      },
      "right_table": {
        "table": "Match",
        "primary_key": ["id"],
        "columns": [
          {"name": "id", "type": "integer"},
          {"name": "season", "type": "text"},
          {"name": "league_id", "type": "integer"}
        ]
      }
    }
  },
  "output_schema": {"choice": "one column name from the questioned table"}
}
```

配对只提供两张表各自的 schema（表名、主键、列名与类型），不提供跨表关系；本步骤的 state 不含 query 与 mention。选项描述中会带上各列的真实样本值。

### 3.3 Questions

每个配对两个方向各一个问题，ID 规则 `c3_<pair_id>_a` 与 `c3_<pair_id>_b`，`a` 对应 `left_table`、`b` 对应 `right_table`：

```json
{
  "c3_p1_a": {
    "type": "choice",
    "instructions": "In table League, which column is most likely the foreign key column that joins it with table Match?",
    "criteria": {
      "id": "League.id, a integer primary key column, values like 1",
      "name": "League.name, a text column, values like Belgium",
      "country_id": "League.country_id, a integer column, values like 1"
    }
  },
  "c3_p1_b": {
    "type": "choice",
    "instructions": "In table Match, which column is most likely the foreign key column that joins it with table League?",
    "criteria": {
      "id": "Match.id, a integer primary key column, values like 1",
      "season": "Match.season, a text column, values like 2016",
      "league_id": "Match.league_id, a integer column, values like 1"
    }
  }
}
```

`choice` 问题的 `criteria` 必须是记录（至少一个条目），键为候选项、值为该项的描述，端点即按 `criteria` 的键给出各候选项概率并从中选择。本步骤把该表的全部列作为候选项，键用列名本身，描述由列类型、是否主键与样本值生成。

### 3.4 Response

```json
{
  "answers": {
    "c3_p1_a": {
      "type": "choice",
      "choice": "country_id",
      "confidence": 0.88,
      "probabilities": {"id": 0.07, "name": 0.01, "country_id": 0.92}
    },
    "c3_p1_b": {
      "type": "choice",
      "choice": "league_id",
      "confidence": 0.93,
      "probabilities": {"id": 0.03, "season": 0.02, "league_id": 0.95}
    }
  },
  "usage": {"input_tokens": 812, "output_tokens": 96}
}
```

### 3.5 答案解析

依次尝试 `choice`、`answer`、`selected`、`option`、`column` 字段：

- 字符串：去除首尾空白、引号与反引号后按忽略大小写匹配候选项；形如 `League.id` 的前缀会被剥离，只比较列名
- 整数：在候选项范围内按 0 基下标取值
- 以上均未命中时，回退到 `probabilities` 中概率最高的、且能匹配候选项的键
- 无法解析或列名不在候选项中：该方向视为放弃，不加入连接列

### 3.6 结果列合并

两个方向各自解析出的列都加入结果列集合：

- 已在集合中：仅追加 `sources` 中的 `result_join`
- 不在集合中：新增条目 `best_score = null`、`mentions = []`、`sources = ["result_join"]`，`dtype` 取自该表的 schema
- 按 `(table, column)` 忽略大小写去重

`linked_columns`（按 span 分组）不包含连接列，只有 `linked_columns_union` 包含，与前一机制一致。

## 4. 输出记录

| 字段 | 含义 |
|---|---|
| `stage_join.num_result_tables` | 结果表数 `k` |
| `stage_join.num_pairs` | 配对数 `P` |
| `stage_join.pairs` | 配对清单（`pair_id` / `left_table` / `right_table`） |
| `stage_join.num_questions` | choice 问题数 `2P` |
| `stage_join.answers` | 全部 `c3_...` 答案 |
| `stage_join.chosen_columns` | 解析成功的连接列（含 `pair_id` / `table` / `column` / `dtype`） |
| `stage_join.num_added` | 去重后新增到结果列的条目数 |
| `stage_join.elapsed_seconds` / `usage` | 本步骤耗时与 usage |

`usage.result_join`、`elapsed.result_join_seconds` 同步写入顶层。

`stage_join` 不再有 `threshold`、`candidates`、`selected_links` 字段；`stage_column.tables[*]` 不再有 `num_join_candidates`。

## 5. 配置

```yaml
schema_path: outputs/schema/minidev_schema.json
output_dir: outputs/runs/jev_minidev_table_first_result_join

mentions:
  enabled: false

cascade:
  table_candidates: 5
  table_noul_threshold: 0.5
  table_column_batch: 0
  score_scale: [irrelevant, weak, relevant, strong]
  column_select_mode: ambiguous
  column_select_high: 2.5
  column_select_low: 1.0
  ambiguous_noul_threshold: 0.5
  column_score_threshold: 2.0   # hard 模式使用
```

步骤 3 无独立配置项，`cascade.fk_noul_threshold` 已废弃。

## 6. 运行与评测

```powershell
conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev_table_first_result_join.yaml --question-id 1025
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first_result_join.yaml
conda run -n znws python scripts/evaluate_schema_linking.py --config configs/pipeline_minidev_table_first_result_join.yaml
conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev_table_first_result_join.yaml
```

本实验使用独立 `output_dir`，与既有实验产物互不覆盖。

## 7. 限制

- `choice` 问题的 `criteria` 必须至少含一个条目，否则端点返回 400 `Choice questions require at least one criterion`
- 候选项为该表全部列，宽表的选项描述会显著拉长单题输入
- 连接列无条件加入结果列，不判断该连接是否为本题所需，多表题目可能引入与题目无关的 ID 列
- 两方向独立判定，可能出现两侧列名不相容（例如一侧选了主键、另一侧选了非外键列）而无法构成有效连接
- 结果表数 `k` 增大时问题数按 `k(k-1)` 增长，且多表题目的配对可能包含实际不需要连接的组合
- 仅依据列名与类型和主键推断连接列，缺少值域（如值包含依赖）证据
- 其余限制与基线一致，见 [docs/jev_schema_linking_table_first.md](jev_schema_linking_table_first.md) 第 12 节

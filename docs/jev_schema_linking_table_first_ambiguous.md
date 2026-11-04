# 模糊列二次判定的 Schema Linking 机制说明

本文档说明“先表后列 + 模糊列额外一轮 JEV 判定”的实验变体：把步骤 3 的单一硬阈值替换为“高分直接入选、低分直接放弃、中间区间再问一轮 Noul”。其余步骤与 [docs/jev_schema_linking_table_first.md](jev_schema_linking_table_first.md) 一致。

## 1. 与基线机制的差异

| 项目 | 基线（table_first） | 本实验（ambiguous） |
|---|---|---|
| 列筛选 | 单阈值 `column_score_threshold` | 高/低双阈值 + 中间区间二次判定 |
| 额外 JEV 轮次 | 无 | 有，仅对模糊列 |
| 判定依据 | 列 Score | 列 Score（直接入选/放弃）+ Noul（模糊列） |
| 其他阶段 | 表级 Noul、按表列打分、外键判断、强制外键列 | 相同 |

配置开关：

```yaml
cascade:
  column_select_mode: ambiguous
```

取 `hard` 时行为与基线一致。

## 2. 列筛选规则

对每个已打分列取全体 span 上的最高 Score `best_score`：

| 条件 | 处理 |
|---|---|
| `best_score >= column_select_high`（默认 2.5） | 直接入选 |
| `best_score < column_select_low`（默认 1.0） | 直接放弃 |
| `column_select_low <= best_score < column_select_high` | 进入额外一轮 Noul 判定 |

额外一轮的问题：

```text
Is it necessary to add the database column {table}.{column} to the already
selected column set in order to answer the question: {query}?
```

Noul 概率 `>= ambiguous_noul_threshold`（默认 0.5）时补充到已入选列集，否则放弃。

## 3. 总体流程

```text
NL query + evidence
        │
        ▼
[1] mention 抽取（m1..mM）
        │
        ▼
[2] 步骤 1：表级 Noul，保留候选表
        │
        ▼
[3] 步骤 2：按表提交 mention × 列 Score + 外键 Noul
        │
        ▼
[4] 每列取 best_score，按双阈值分流
        │
        ├── ≥ high ──────────────► 已入选列集
        ├── < low ───────────────► 放弃
        └── [low, high) ─────────► 步骤 2b
                                      │
                                      ▼
                         一次额外 JEV 请求（Noul / 模糊列）
                                      │
                            accept ───┴─── reject
                                │              │
                                ▼              ▼
                          已入选列集        放弃
        │
        ▼
[5] 对保留的外键连接强制保留两端列
        │
        ▼
linked_columns / linked_columns_union / linked_tables
```

问题数量：

```text
N = T + Σ_t (M × C_t + L_t) + A
```

- `A`：模糊列数（`best_score` 落在 `[low, high)` 的列数）
- 额外一轮只有一次请求，`A` 个 Noul 问题；`A = 0` 时跳过

## 4. 步骤 2b：模糊列判定

### 4.1 State

```json
{
  "task": "ambiguous_column_completion",
  "instruction": "The selected columns are already a candidate column set for the question. For each ambiguous column, decide whether the question cannot be answered without adding it to that set. Answer only the provided questions.",
  "context": {
    "database": "european_football_2",
    "question": "Give the name of the league had the most goals in the 2016 season?",
    "evidence": "...",
    "evidence_spans": ["... 全部 mention ..."],
    "schema_summary": "League(id integer PK, name text, country_id integer); Match(...); ..."
  },
  "selected_columns": [
    {"table": "League", "column": "name", "dtype": "text"},
    {"table": "Match", "column": "season", "dtype": "text"},
    {"table": "Match", "column": "home_team_goal", "dtype": "integer"},
    {"table": "Match", "column": "away_team_goal", "dtype": "integer"}
  ],
  "candidates": {
    "t7_c3": {
      "table_name": "Match",
      "column_name": "league_id",
      "dtype": "integer",
      "sample": 492,
      "date_format": null,
      "best_score": 1.99
    }
  },
  "output_schema": {"noul": "probability in [0, 1]"}
}
```

- `selected_columns`：已直接入选的列，只给表、列、类型
- `candidates`：模糊列的详细信息，含 `best_score`
- 候选 key 复用步骤 2 的 `candidate_id`（如 `t7_c3`）

### 4.2 Questions

每个模糊列一个 Noul，ID 规则 `s3_<candidate_id>`：

```json
{
  "s3_t7_c3": {
    "type": "noul",
    "instructions": "The column Match.league_id is only weakly related to the question. Is it necessary to add it to the already selected column set in order to answer the question: Give the name of the league had the most goals in the 2016 season?"
  }
}
```

### 4.3 Response

```json
{
  "answers": {"s3_t7_c3": {"type": "noul", "noul": 0.91}},
  "usage": {"input_tokens": 1984, "output_tokens": 25}
}
```

`noul >= ambiguous_noul_threshold` 时入选，并记录 `sources: ["ambiguous"]` 与 `ambiguous_noul`。

## 5. 最终输出

- `linked_columns`：每个 span 下最终入选列中该 span Score `>= column_select_low` 的列
- `linked_columns_union`：入选列去重，`sources` 取值：
  - `["score"]`：直接入选
  - `["ambiguous"]`：模糊列经二次判定入选
  - 含 `forced_foreign_key`：外键连接强制保留
- `linked_tables`：最终列涉及的表

示例（q1025）：

```json
[
  {"table": "League", "column": "name", "best_score": 2.98, "sources": ["score"]},
  {"table": "Match", "column": "season", "best_score": 2.98, "sources": ["score"]},
  {"table": "Match", "column": "home_team_goal", "best_score": 2.74, "sources": ["score"]},
  {"table": "Match", "column": "away_team_goal", "best_score": 2.71, "sources": ["score"]},
  {
    "table": "Match",
    "column": "league_id",
    "best_score": 1.99,
    "sources": ["ambiguous"],
    "ambiguous_noul": 0.91
  }
]
```

## 6. 配置

`configs/pipeline_minidev_table_first_ambiguous.yaml`：

```yaml
dataset: BIRD
split: MINIDEV
schema_path: outputs/schema/minidev_schema.json
questions_path: BIRD/MINIDEV/mini_dev_sqlite.json
databases_dir: BIRD/MINIDEV/dev_databases
output_dir: outputs/runs/jev_minidev_table_first_ambiguous

mention_llm: ...

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
  fk_noul_threshold: 0.5

jev: ...

eval: ...
analysis: ...
```

| 字段 | 默认值 | 作用 |
|---|---:|---|
| `column_select_mode` | `hard` | `hard`：单阈值；`ambiguous`：双阈值 + 二次判定 |
| `column_select_high` | 2.5 | 直接入选阈值 |
| `column_select_low` | 1.0 | 直接放弃阈值 |
| `ambiguous_noul_threshold` | 0.5 | 模糊列入选的 Noul 阈值 |
| `column_score_threshold` | 2.0 | 仅 `hard` 模式使用 |

与基线配置的唯一区别是 `column_select_mode` 及三个阈值，以及独立的 `output_dir`，两组实验产物互不覆盖。

## 7. 输出记录

新增 `stage_ambiguous`：

| 字段 | 含义 |
|---|---|
| `high_threshold` / `low_threshold` / `noul_threshold` | 本次使用的三个阈值 |
| `num_selected` | 直接入选列数 |
| `num_ambiguous` / `num_dropped` | 模糊列数 / 直接放弃列数 |
| `num_accepted` | 二次判定入选列数 |
| `candidates` | 模糊列清单（含 `best_score`） |
| `answers` | 全部 `s3_...` 答案 |
| `accepted_columns` | 入选的模糊列（含 `best_score` 与 `noul`） |
| `elapsed_seconds` / `usage` | 额外一轮耗时与 usage |

`usage.ambiguous`、`elapsed.ambiguous_seconds` 同步写入顶层。

## 8. 运行与评测

```powershell
conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev_table_first_ambiguous.yaml --question-id 1025
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first_ambiguous.yaml
conda run -n znws python scripts/evaluate_schema_linking.py --config configs/pipeline_minidev_table_first_ambiguous.yaml
conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev_table_first_ambiguous.yaml
```

产物与其他实验一致，写入 `<output_dir>` 下的 `batch_answers.jsonl`、`reports/` 与 `analysis/`。

## 9. 实测与限制

q1025：

```text
Stage 2b (ambiguous): 4 selected + 1 ambiguous columns = 1 Noul questions
Stage 2b accepted 1/1 ambiguous columns        # Match.league_id, best_score 1.99, Noul 0.91
```

q1471：

```text
Stage 2b (ambiguous): 1 selected + 1 ambiguous columns = 1 Noul questions
Stage 2b accepted 0/1 ambiguous columns        # 该列被放弃
```

限制：

- 二次判定只看“是否必须补充”，不做 mention 级消歧；歧义列仍可能被高置信度误收
- `best_score` 取全体 span 最大值，长问题的偶然高分会影响分流
- 阈值 `column_select_high`、`column_select_low`、`ambiguous_noul_threshold` 需要按实验调优
- 其余限制与基线一致，见 `docs/jev_schema_linking_table_first.md` 第 12 节

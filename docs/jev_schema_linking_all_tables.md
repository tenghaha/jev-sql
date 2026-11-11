# 全表列打分 + 模糊列二次判定 + 结果表连接补全的 Schema Linking 机制说明

本文档说明“取消选表 Stage + 不做 mention 提取 + 模糊列二次判定 + 结果表连接补全”的实验变体：不再做表级 Noul 选表，数据库中每一张表都直接进入列打分；列筛选、模糊列二次判定与结果表连接补全与前一机制完全一致。

配置：`configs/pipeline_minidev_all_tables.yaml`（`mentions.enabled: false` + `column_select_mode: ambiguous`）。

> 本机制是 [docs/jev_schema_linking_table_first_result_join.md](jev_schema_linking_table_first_result_join.md) 移除步骤 1（表级 Noul 选表）后的版本：步骤 2（列打分）、步骤 2b（模糊列二次判定）与步骤 3（结果表连接补全）逐字保持不变，仅把“候选表”换成“全库表”。更早的 [docs/jev_schema_linking_table_first.md](jev_schema_linking_table_first.md)、[docs/jev_schema_linking_table_first_no_mention.md](jev_schema_linking_table_first_no_mention.md)、[docs/jev_schema_linking_table_first_ambiguous.md](jev_schema_linking_table_first_ambiguous.md) 同样早于该改动。

## 1. 与前一机制的差异

| 项目 | 前一机制（result_join） | 本实验（all_tables） |
|---|---|---|
| 步骤 1 表级 Noul 选表 | 有，按 Noul 阈值 + top-k 保留候选表 | 无，取消该步骤 |
| 进入列打分的表 | `selected_tables`（≤ `table_candidates` 张） | 数据库全部表 |
| 表候选来源 | 表级 Noul 打分 | 直接取 schema 全部表，按库内顺序编号 `t1..tN` |
| 配置项 `table_candidates` / `table_noul_threshold` | 生效 | 已移除 |
| `stage_table` 记录 | `num_candidates` / `num_questions` / `selection` / `candidates` / `selected_tables` | `num_tables` / `selected_tables` |
| 步骤 2 起点问题数 | `T` 个表级 Noul 问题 | 0 |
| 步骤 2/2b/3 | 全部保留 | 完全相同，未改动 |

## 2. 总体流程

```text
NL query + evidence
        │
        ▼
[1] 不抽取 mention；把原始 query 包装为唯一 span
        │
        ▼
[2] 取全库表 t1..tN 作为打分对象（无表级筛选）
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
N = Σ_t C_t + A + 2P
```

- `C_t`：表 `t` 的列数（对全库每张表；无 mention 时每个 span 覆盖整表列）
- `A`：模糊列数（步骤 2b）；`A = 0` 时跳过
- `P`：结果表配对数，即 `k(k-1)/2`（`k` 为结果表数）；`P = 0` 时跳过步骤 3

相比前一机制，问题数去掉了表级 Noul 的 `T` 项，但 `Σ_t C_t` 覆盖全库全部表，总量通常显著增大。

## 3. 步骤 2：全表列打分

表的编号与列候选的编号沿用原逻辑：全库表按 schema 顺序编号 `t1..tN`，每张表的列编号为 `<table_id>_c1..cM`。区别仅在于不再根据表级 Noul 结果做筛选，`t1..tN` 全部提交列打分。

每一步接口（state / questions / answers）与 [docs/jev_schema_linking_table_first_ambiguous.md](jev_schema_linking_table_first_ambiguous.md) 第 5 节和 [docs/jev_schema_linking_table_first_result_join.md](jev_schema_linking_table_first_result_join.md) 第 3 节描述的一致，未做修改。

## 4. 后续步骤（未改动）

- 步骤 2 列筛选：`column_select_mode: ambiguous` 下按高低双阈值分流，模糊列进入步骤 2b
- 步骤 2b 模糊列判定：一次额外 Noul 请求
- 步骤 3 结果表连接补全：结果表两两配对，每对两个 `choice` 问题判定连接列，连接列加入结果列并去重

以上与 [docs/jev_schema_linking_table_first_result_join.md](jev_schema_linking_table_first_result_join.md) 第 3 节完全一致。

## 5. 输出记录

`stage_table` 记录全库表清单：

| 字段 | 含义 |
|---|---|
| `stage_table.num_tables` | 参与列打分的表数 `N`（等于全库表数） |
| `stage_table.selected_tables` | 全库表清单（`table_id` / `table`），按 schema 顺序 `t1..tN` |

`stage_table` 不再有 `num_candidates`、`num_questions`、`selection`、`candidates`、`elapsed_seconds`、`usage` 字段。

`stage_column`、`stage_ambiguous`、`stage_join` 的记录格式与前一机制一致。

顶层 `usage` 不再包含 `table` 项，`elapsed` 不再包含 `table_seconds` 项。

## 6. 配置

```yaml
schema_path: outputs/schema/minidev_schema.json
output_dir: outputs/runs/jev_minidev_all_tables

mentions:
  enabled: false

cascade:
  table_column_batch: 0
  score_scale: [irrelevant, weak, relevant, strong]
  column_select_mode: ambiguous
  column_select_high: 2.5
  column_select_low: 1.0
  ambiguous_noul_threshold: 0.5
  column_score_threshold: 2.0   # hard 模式使用
```

`table_candidates` 与 `table_noul_threshold` 已随步骤 1 一并移除；`cascade.fk_noul_threshold` 早于步骤 3 已废弃。

## 7. 运行与评测

```powershell
conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev_all_tables.yaml --question-id 1025
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_all_tables.yaml
conda run -n znws python scripts/evaluate_schema_linking.py --config configs/pipeline_minidev_all_tables.yaml
conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev_all_tables.yaml
```

本实验使用独立 `output_dir`，与既有实验产物互不覆盖。

## 8. 限制

- 列打分请求数随全库列数增长：`Σ_t C_t` 覆盖全库每张表，问题数与调用耗时显著高于选表后的版本；表多、列多的宽库尤为明显
- 缺少表级筛选，无关表也会产生大量列问题，最终列集的噪声与假阳性概率上升
- 步骤 3 的两两配对仅针对含结果列的表，与前一机制一致
- 其余限制与 [docs/jev_schema_linking_table_first_result_join.md](jev_schema_linking_table_first_result_join.md) 第 7 节一致

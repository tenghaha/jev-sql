# 无 Mention 提取的 Schema Linking 机制说明

本文档说明“先表后列 + 不做 mention 提取”的实验变体。除 mention 抽取外，其余步骤、阈值与输出结构与 [docs/jev_schema_linking_table_first.md](jev_schema_linking_table_first.md) 完全一致。

## 1. 与基线机制的差异

| 项目 | 基线（table_first） | 本实验（no_mention） |
|---|---|---|
| mention 抽取 | 一次生成式 LLM 调用，得到 `m1..mM` | 不调用 LLM |
| 参与打分的 span | `M` 个 mention | 原始 query 的 1 个 span |
| 步骤 2 列问题数 | `M × C_t` | `C_t` |
| 记录中的 `mentions` | LLM 抽取结果 | 单个 `query` span |
| 其他阶段 | 表级 Noul、外键判断、阈值过滤、强制外键列 | 相同 |

配置开关：

```yaml
mentions:
  enabled: false
```

为 `true` 时行为与基线一致。

## 2. 总体流程

```text
NL query + evidence
        │
        ▼
[1] 不抽取 mention；把原始 query 包装为唯一 span
        │  span_id = "query"
        ▼
[2] 步骤 1：表级 Noul（每张表一个问题）
        │
        ▼
[3] 保留 Noul ≥ 阈值的表，并截断到 top-k
        │
        ▼
[4] 步骤 2：按表提交
    · 每个候选列 1 个 Score（判定该列与原始 query 的相关性）
    · 每条涉及本表的候选外键连接 1 个 Noul
        │
        ▼
[5] 列 Score 阈值过滤
        │
        ▼
[6] 强制保留选中连接的外键列
        │
        ▼
linked_columns / linked_columns_union / linked_tables
```

问题数量：

```text
N = T + Σ_t (C_t + L_t)
```

- `T`：表数
- `C_t`：候选表 `t` 的列数
- `L_t`：涉及候选表 `t` 的外键连接数

相比基线（`T + Σ_t (M × C_t + L_t)`），列阶段的问题数降为原来的 `1/M`。

## 3. Query Span 表示

关闭 mention 提取后，`jev/pipeline.py` 构造唯一 span：

```json
{
  "span_id": "query",
  "text": "What is the ratio of customers who pay in EUR against customers who pay in CZK?",
  "span_type": "query"
}
```

该 span 同时用于：

- `context.evidence_spans`
- 步骤 2 列问题的匹配目标
- 输出 `linked_columns` 的分组

`context.question` 与 `context.evidence` 保持原样，仍作为整体问题描述。

## 4. 步骤 1：表级 Noul

与基线相同：一张表一个 Noul，问题 ID 为 `s1_<table_id>`。

```json
{
  "s1_t1": {
    "type": "noul",
    "instructions": "Is the database table customers needed to answer the question, given its evidence spans and column list?"
  }
}
```

`context.evidence_spans` 中只有一个 `query` span。

示例（q1471，`debit_card_specializing`）：

```text
customers        0.99   ← 保留
gasstations      0.02
transactions_1k  0.02
yearmonth        0.02
products         0.01
```

## 5. 步骤 2：按表列打分与外键判断

每张候选表一次请求。state 与基线一致（全库紧凑 schema + 本表列详情 + 连接候选），但列问题只有一个 span：

```json
{
  "s2_query_t1_c3": {
    "type": "score",
    "instructions": "How strongly does the database column customers.Currency match the evidence span \"What is the ratio of customers who pay in EUR against customers who pay in CZK?\"?",
    "criteria": ["irrelevant", "weak", "relevant", "strong"]
  },
  "j_l1": {
    "type": "noul",
    "instructions": "Is the join yearmonth.CustomerID = customers.CustomerID needed to connect the focus table customers with yearmonth for this question?"
  }
}
```

- 列问题 ID：`s2_query_<candidate_id>`
- 连接问题 ID 不变：`j_<link_id>`
- 每张表的问题数：`C_t + L_t`

示例（q1471，保留 `customers`）：

```text
Stage 2 table customers: 1 spans x 3 columns + 0 joins = 3 questions
Currency score = 2.97 → 保留
```

## 6. 步骤 3/4：过滤与强制外键列

与基线完全一致：

- 列 Score ≥ `column_score_threshold` 保留
- 连接的 Noul ≥ `fk_noul_threshold` 时保留，并强制保留两端列（`sources` 含 `forced_foreign_key`）
- `linked_tables` 由最终列去重得到

## 7. 配置

`configs/pipeline_minidev_table_first_no_mention.yaml`：

```yaml
dataset: BIRD
split: MINIDEV
schema_path: outputs/schema/minidev_schema.json
questions_path: BIRD/MINIDEV/mini_dev_sqlite.json
databases_dir: BIRD/MINIDEV/dev_databases
output_dir: outputs/runs/jev_minidev_table_first_no_mention

mentions:
  enabled: false

mention_llm:          # mentions.enabled=false 时不使用
  model_env: OPENAI_API_MODEL
  api_key_env: OPENAI_API_KEY
  base_url_env: OPENAI_API_BASE
  temperature: 0.0
  timeout: 120
  max_retries: 2

cascade:
  table_candidates: 5
  table_noul_threshold: 0.5
  table_column_batch: 0
  score_scale: [irrelevant, weak, relevant, strong]
  column_score_threshold: 2.0
  fk_noul_threshold: 0.5

jev:
  model: typesafe-ai/jev
  endpoint_env: JEV_API_BASE
  api_key_env: AI_GATEWAY_API_KEY
  timeout: 120

eval:
  preds_file: batch_answers.jsonl
  report_file: schema_linking.md
  details_file: schema_linking.json
  include_missing: false

analysis:
  sample_size: 12
  seed: 42
  error_cases_file: error_cases.jsonl
  report_file: error_analysis.md
```

与基线配置的唯一区别是 `mentions.enabled: false` 与独立的 `output_dir`，两组实验产物互不覆盖。

## 8. 输出记录

记录字段与基线一致，新增一个标记：

| 字段 | 含义 |
|---|---|
| `mentions_enabled` | `false` 表示本记录未做 mention 抽取 |
| `mentions` | 仅包含一个 `query` span |

`stage_table`、`stage_column`、`stage_join`、`linked_columns`、`linked_columns_union`、`linked_tables` 的结构不变；`stage_column.answers` 的 key 为 `s2_query_<candidate_id>`。

## 9. 运行与评测

```powershell
conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev_table_first_no_mention.yaml --question-id 1471
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first_no_mention.yaml
conda run -n znws python scripts/evaluate_schema_linking.py --config configs/pipeline_minidev_table_first_no_mention.yaml
conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev_table_first_no_mention.yaml
```

产物：

```text
<output_dir>/batch_answers.jsonl
<output_dir>/reports/schema_linking.md
<output_dir>/reports/schema_linking.json
<output_dir>/analysis/error_cases.jsonl
<output_dir>/reports/error_analysis.md
```

## 10. 限制

- 只用一个整体 query 打分，缺少 mention 级消歧，同名/相似列更容易混淆
- 表级 Noul 仍使用原始 query 与 evidence，不受本变体影响
- 其余限制与基线一致，见 `docs/jev_schema_linking_table_first.md` 第 12 节

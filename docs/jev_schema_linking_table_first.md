# JEV 先表后列 Schema Linking 机制说明

本文档说明表优先（table-first）机制的 JEV 请求/响应格式、四个步骤、筛选与强制外键规则、日志与输出结构。示例来自 BIRD MINIDEV：`question_id=1471`（`debit_card_specializing`）与 `question_id=1025`（`european_football_2`）。

与旧版列优先级联（`docs/jev_schema_linking_cascade.md`）的区别：先在表级做 Noul 召回，再按表提交列级 Score 与外键联系判断，最后强制保留选中连接的外键列。

## 1. 总体流程

```text
NL query + evidence
        │
        ▼
[1] 生成式 LLM 一次性抽取全部 mention
        │  m1..mM
        ▼
[2] 步骤 1：表级 Noul
    紧凑表 schema（表名、主键、列名与类型）作为候选；
    每张表问一次“该表是否与问题相关”
        │  noul(t1)..noul(tT)
        ▼
[3] 保留 Noul ≥ 阈值的表，并截断到 top-k
        │  候选表集 F
        ▼
[4] 步骤 2：按表提交，一次处理一张表
    · 每个 mention × 本表每一列 一个 Score
    · 每条涉及本表的候选外键连接 一个 Noul
    状态包含全库紧凑 schema + 本表列详情
        │
        ▼
[5] 步骤 3：列 Score 阈值过滤
        │  相关列
        ▼
[6] 步骤 4：强制保留选中外键连接的列
        │
        ▼
linked_columns / linked_columns_union / linked_tables
```

问题数量：

```text
N = T + Σ_t (M × C_t + L_t)
```

- `T`：数据库的表数
- `M`：mention 数
- `C_t`：候选表 `t` 的列数
- `L_t`：涉及候选表 `t` 的外键连接数（同一连接会出现在两端表的请求中，答案按最大 Noul 合并）

本流程只使用 Noul 与 Score，不使用 Choice。

## 2. 输入数据模型

Schema 结构不变：

```text
dataset
└── databases[]
    └── tables[]
        ├── name
        ├── primary_key[]
        ├── adjacent_tables[]      # 外键邻接，用于步骤 2 的候选连接
        └── columns[]
            ├── name
            ├── type
            ├── sample
            └── date_format
```

两种候选对象：

| 阶段 | 候选 ID | 内容 |
|---|---|---|
| 步骤 1 | `t1..tT` | 表名、主键、列名与类型（紧凑表 schema） |
| 步骤 2 | `t1_c1..tT_cM` | 列名、所属表、类型、样本、日期格式、角色提示 |

列角色提示 `column_role_hint`：`primary_key` / `temporal` / `measure` / `attribute`。

步骤 1 不展平列，也不携带样本值；列详情只在步骤 2 的单表状态中出现。

## 3. Mention 抽取

与旧机制一致：`jev/mentions.py` 每题调用一次生成式 LLM，一次抽全，`m1..mM` 去重编号，输出英文 Prompt 要求的 JSON。调用通过 LangChain `ChatOpenAI` 进行，并固定以 `extra_body={"enable_thinking": False}` 关闭思考模式。`span_type` 取值：

`entity` / `entity_attribute` / `numeric_filter` / `temporal_filter` / `aggregation` / `join_relation` / `other`

## 4. JEV 传输层

`jev/jev_client.py` 直接向 `JEV_API_BASE` POST：

```json
{
  "model": "typesafe-ai/jev",
  "state": { "...": "..." },
  "questions": { "...": "..." }
}
```

- Header：`Authorization: Bearer <AI_GATEWAY_API_KEY>`
- 环境变量：`JEV_API_BASE`、`AI_GATEWAY_API_KEY`
- `questions` 为空时跳过请求
- `state.output_schema` 只是提示，响应可能包含 `legend`、`probabilities`
- `--debug` 打印完整请求与响应

步骤 2 的 `questions` 同时包含 `score` 与 `noul` 两种原语，JEV 按每个问题的 `type` 返回。

## 5. 步骤 1：表级 Noul 候选

### 5.1 目的

用整条问题与全部 mention，对数据库中的每一张表判断“该表是否与问题相关”，得到候选表集。此步骤只使用紧凑表 schema。

### 5.2 State

```json
{
  "task": "table_linking",
  "instruction": "Decide whether each candidate table is needed to answer the question. Each candidate is a database table with its primary key and columns. Use the evidence spans as the natural-language mentions. Answer only the provided questions.",
  "context": {
    "database": "debit_card_specializing",
    "question": "What is the ratio of customers who pay in EUR against customers who pay in CZK?",
    "evidence": "ratio of customers who pay in EUR against customers who pay in CZK = count(Currency = 'EUR') / count(Currency = 'CZK').",
    "evidence_spans": [
      {"span_id": "m1", "text": "ratio", "span_type": "aggregation"},
      {"span_id": "m2", "text": "customers", "span_type": "entity"},
      {"span_id": "m3", "text": "EUR", "span_type": "other"}
    ],
    "schema_summary": "customers(CustomerID integer PK, Segment text, Currency text); gasstations(GasStationID integer PK, ...); ..."
  },
  "candidates": {
    "t1": {
      "table_name": "customers",
      "primary_key": ["CustomerID"],
      "columns": [
        {"name": "CustomerID", "type": "integer"},
        {"name": "Segment", "type": "text"},
        {"name": "Currency", "type": "text"}
      ]
    },
    "t2": {"table_name": "gasstations", "primary_key": ["GasStationID"], "columns": ["..."]}
  },
  "output_schema": {"noul": "probability in [0, 1]"}
}
```

`context.evidence_spans` 是全部 mention，只出现一次；`candidates` 覆盖全库所有表。

### 5.3 Questions

每张表一个 Noul，ID 规则 `s1_<table_id>`：

```json
{
  "s1_t1": {
    "type": "noul",
    "instructions": "Is the database table customers needed to answer the question, given its evidence spans and column list?"
  },
  "s1_t2": {
    "type": "noul",
    "instructions": "Is the database table gasstations needed to answer the question, given its evidence spans and column list?"
  }
}
```

### 5.4 Response

```json
{
  "answers": {
    "s1_t1": {"type": "noul", "noul": 0.99},
    "s1_t2": {"type": "noul", "noul": 0.02}
  },
  "usage": {"input_tokens": 1480, "output_tokens": 99}
}
```

### 5.5 候选表选择

按 `noul` 降序排序，先过滤 `noul >= table_noul_threshold`，再截断到 `table_candidates`：

```text
customers        0.99   ← 保留
gasstations      0.02
transactions_1k  0.02
yearmonth        0.02
products         0.01
```

上例在 `table_noul_threshold=0.5` 下只保留 `customers`。`table_noul_threshold=0` 表示不做概率过滤，只按 top-k 截断。

记录中保留全部表的 Noul，便于分析表级召回：

```json
{"table_id": "t1", "table": "customers", "noul": 0.99}
```

## 6. 步骤 2：按表的列 Score 与外键联系

### 6.1 目的

对每个候选表，独立判断本表每一列与每个 mention 的匹配强度；同时判断“本表与其他候选表之间的候选外键连接”是否为回答问题所需。

一表一次提交；当表的列数超过 `table_column_batch` 时按列分批，外键问题只放在第一批。

### 6.2 外键候选的来源

候选外键来自生成的 gold schema（如 `outputs/schema/minidev_schema.json`）中每张表的 `adjacent_tables`：

- 每个 `foreign_keys` 二元组 `[child.column, parent.column]` 转换为一条候选连接
- 双向存储的同一对连接按规范化后的四元组去重
- 不读取 gold SQL
- schema 中不存在的表或列会被丢弃
- 只保留两端都属于候选表集的连接

每条连接分配 `l1..lL`，例如：

```json
{"link_id": "l1", "child_table": "yearmonth", "child_column": "CustomerID", "parent_table": "customers", "parent_column": "CustomerID"}
```

### 6.3 State

```json
{
  "task": "table_column_linking",
  "instruction": "Score how strongly each evidence span matches each candidate column of the focus table. Also decide whether each listed join candidate is needed to connect the focus table with another candidate table. The full database schema is provided as context. Answer only the provided questions.",
  "context": {
    "database": "debit_card_specializing",
    "question": "...",
    "evidence": "...",
    "evidence_spans": ["... 全部 mention ..."],
    "schema_summary": "customers(...); gasstations(...); ..."
  },
  "focus_table": {
    "table_id": "t1",
    "table_name": "customers",
    "primary_key": ["CustomerID"]
  },
  "candidates": {
    "t1_c1": {
      "column_name": "CustomerID",
      "table_name": "customers",
      "dtype": "integer",
      "sample": 3,
      "date_format": null,
      "column_role_hint": "primary_key"
    },
    "t1_c3": {
      "column_name": "Currency",
      "table_name": "customers",
      "dtype": "text",
      "sample": "EUR",
      "date_format": null,
      "column_role_hint": "attribute"
    }
  },
  "join_candidates": {
    "l1": {
      "link_id": "l1",
      "child_table": "yearmonth",
      "child_column": "CustomerID",
      "parent_table": "customers",
      "parent_column": "CustomerID",
      "other_table": "yearmonth"
    }
  },
  "output_schema": {
    "score": "expected level index",
    "noul": "probability in [0, 1]",
    "confidence": "probability in [0, 1]"
  }
}
```

- `context.schema_summary` 是全库紧凑 schema，供外键联系判断使用
- `candidates` 只包含本表的列
- `join_candidates` 只包含涉及本表、且另一端也在候选表集中的连接

### 6.4 Questions

列问题，ID 规则 `s2_<span_id>_<candidate_id>`：

```json
{
  "s2_m3_t1_c3": {
    "type": "score",
    "instructions": "How strongly does the database column customers.Currency match the evidence span \"EUR\"?",
    "criteria": ["irrelevant", "weak", "relevant", "strong"]
  }
}
```

外键问题，ID 规则 `j_<link_id>`：

```json
{
  "j_l1": {
    "type": "noul",
    "instructions": "Is the join yearmonth.CustomerID = customers.CustomerID needed to connect the focus table customers with yearmonth for this question?"
  }
}
```

同一连接会出现在两端表的请求中，得到相同 ID 的答案；合并时取较大的 Noul。

### 6.5 Response

```json
{
  "answers": {
    "s2_m3_t1_c3": {
      "type": "score",
      "score": 2.92,
      "confidence": 0.92,
      "legend": {"0": "irrelevant", "1": "weak", "2": "relevant", "3": "strong"},
      "probabilities": {"0": 0.02, "1": 0.06, "2": 0.22, "3": 0.70}
    },
    "j_l1": {"type": "noul", "noul": 0.87}
  }
}
```

- `score`：概率加权期望等级，范围 `[0, 3]`
- `noul`：连接必要性概率，范围 `[0, 1]`

### 6.6 分批

`table_column_batch` 控制每张表每个请求包含多少列，默认 `0`（整表一次提交）：

- 每批 state 都包含完整 query、mentions、全库 schema 与本表列详情
- `candidates` 只包含该批的列
- `join_candidates` 只放在第一批
- 各批 answers 合并后统一过滤

实测 `european_football_2.Match` 有 115 列，`M=6` 时一次提交 691 个问题（约 62k input tokens）可以通过；列数更多时可设置 `table_column_batch` 例如 `40`。

## 7. 步骤 3：列 Score 阈值过滤

`jev/column_candidates.py::filter_columns`：

1. 遍历全部候选列（只来自候选表）
2. 读取 `s2_<mention>_<candidate>` 的 `score`
3. 保留 `score >= column_score_threshold`，默认 `2.0`
4. 按 score 降序排列

| score | 含义 | 默认是否保留 |
|---:|---|---|
| 0 | irrelevant | 否 |
| 1 | weak | 否 |
| 2 | relevant | 是 |
| 3 | strong | 是 |

未通过阈值的 mention 仍保留在输出中，`columns` 为空，便于分析召回。

## 8. 步骤 4：强制保留外键列

对每条 Noul ≥ `fk_noul_threshold`（默认 `0.5`）的连接，把两端的列强制写入最终列集合：

```text
yearmonth.CustomerID = customers.CustomerID
→ 强制保留 yearmonth.CustomerID 与 customers.CustomerID
```

规则：

- 已在阈值结果中的列不重复添加，只追加 `sources: ["score", "forced_foreign_key"]`
- 新增列 `best_score = null`，`mentions = []`，`sources = ["forced_foreign_key"]`
- 强制列仍带 `table` / `column` / `dtype`，参与评测与最终表集合
- 最终表集合 `linked_tables` 由最终列去重得到

输出示例：

```json
[
  {
    "table": "customers",
    "column": "Currency",
    "dtype": "text",
    "best_score": 2.98,
    "mentions": ["m3", "m4", "m5"],
    "sources": ["score"]
  },
  {
    "table": "customers",
    "column": "CustomerID",
    "dtype": "integer",
    "best_score": null,
    "mentions": [],
    "sources": ["forced_foreign_key"]
  },
  {
    "table": "yearmonth",
    "column": "CustomerID",
    "dtype": "integer",
    "best_score": null,
    "mentions": [],
    "sources": ["forced_foreign_key"]
  }
]
```

## 9. 配置项

`configs/pipeline_minidev_table_first.yaml`：

```yaml
dataset: BIRD
split: MINIDEV
schema_path: outputs/schema/minidev_schema.json
questions_path: BIRD/MINIDEV/mini_dev_sqlite.json
databases_dir: BIRD/MINIDEV/dev_databases
output_dir: outputs/runs/jev_minidev_table_first

mention_llm:
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

| 字段 | 默认值 | 作用 |
|---|---:|---|
| `output_dir` | - | 单组实验输出根目录；本机制使用新目录，避免覆盖旧结果 |
| `databases_dir` | - | SQLite 根目录，供评测探查 GT 结构 |
| `table_candidates` | 5 | 步骤 1 最多保留的表数 `k` |
| `table_noul_threshold` | 0.5 | 表级 Noul 阈值；`0` 表示只按 top-k 截断 |
| `table_column_batch` | 0 | 步骤 2 每个请求的列数；`0` 表示整表一次提交 |
| `score_scale` | 4 级 | 列 Score 的有序等级 |
| `column_score_threshold` | 2.0 | 列过滤阈值 |
| `fk_noul_threshold` | 0.5 | 连接保留阈值，同时决定强制外键列 |
| `eval.*` | - | 评测文件，相对 `output_dir` |
| `analysis.*` | - | 错误分析文件，相对 `output_dir` |

## 10. 日志与 `--debug`

```powershell
conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev_table_first.yaml --question-id 1471 --debug
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first.yaml --limit 5 --debug
```

`INFO`：

- mention 数
- 步骤 1 表数与 Noul 问题数、保留表名
- 候选外键连接数
- 步骤 2 每张表每批的 mention × 列 + 连接 = 问题数
- 最终表数、mention 链接数、连接数、唯一列数

`DEBUG`：

- mention 原始输出
- 全部表的 Noul 排序
- 完整 JEV request / response
- 最终 `linked_columns`

## 11. 输出记录

批量结果：`<output_dir>/batch_answers.jsonl`，每题一行。

顶层字段：

| 字段 | 含义 |
|---|---|
| `question_id` / `db_id` / `question` / `evidence` | 原问题 |
| `mentions` | 抽取的 mention |
| `stage_table` | 步骤 1 记录 |
| `stage_column` | 步骤 2 列打分记录 |
| `stage_join` | 步骤 2 外键判断记录 |
| `linked_columns` | 按 mention 的最终列 |
| `linked_columns_union` | 去重最终列（含强制外键列） |
| `linked_tables` | 最终表集合 |
| `usage` | 表级 / 列级 token usage |
| `elapsed` | mention / table / column / total 耗时 |
| `status` | `ok` / `failed` |

`stage_table`：

| 字段 | 含义 |
|---|---|
| `num_candidates` | 全库表数 `T` |
| `num_questions` | `T` |
| `selection` | `limit` 与 `noul_threshold` |
| `candidates` | 全部表的 Noul |
| `selected_tables` | 候选表集 |
| `elapsed_seconds` / `usage` | 步骤 1 耗时与 usage |

`stage_column`：

| 字段 | 含义 |
|---|---|
| `score_scale` / `score_threshold` | Score 等级与阈值 |
| `num_tables` / `num_questions` | 候选表数与列问题总数（不含外键问题） |
| `tables` | 每张表：列清单、批次数、问题数、耗时、usage |
| `answers` | 全部 `s2_...` 答案 |
| `elapsed_seconds` / `usage` | 列级累计 |

`stage_join`：

| 字段 | 含义 |
|---|---|
| `threshold` | `fk_noul_threshold` |
| `num_candidates` / `candidates` | 候选连接 |
| `answers` | 全部 `j_...` 答案（两端表答案已按最大 Noul 合并） |
| `selected_links` | 保留的连接及 Noul |

外键问题随表级请求提交，因此其耗时与 usage 计入 `stage_column`。

## 12. 成本与限制

### 12.1 问题数

```text
N = T + Σ_t (M × C_t + L_t)
```

实测：

| 题目 | 表数 T | 候选表 | mentions M | 步骤 1 | 步骤 2 | 合计 |
|---|---:|---|---:|---:|---:|---:|
| q1471 | 5 | customers | 6 | 5 | 18 | 23 |
| q1025 | 7 | Match, League | 6 | 7 | 691 + 19 | 717 |

q1025 的 `Match` 有 115 列，单表一次提交 691 个问题、约 62k input tokens 可以通过。

### 12.2 相对旧机制的变化

- 表级只提交 `T` 个问题，不再为每一列提交 Noul
- 列 Score 只在候选表的列上提交，问题数由“全库列数 + M×候选列数”变为“M×候选表列数”
- 宽表不再需要按 15 列切分全库候选；改为单表提交，必要时按表内列分批

### 12.3 限制

- 表级召回错误无法在列级恢复：未进入候选表集的表，其列不会再被评分
- 外键候选只来自 schema JSON 的 `adjacent_tables`；schema 未收录的关系不会被强制保留
- schema 的 `adjacent_tables` 可能遗漏实际 join（例如 `european_football_2.Match.league_id → League.id` 未被收录），此时该连接不会进入候选，也不会强制保留外键列
- 强制外键列可能引入少量 FP，需通过评测观察收益与代价
- 阈值 `table_noul_threshold`、`column_score_threshold`、`fk_noul_threshold` 需要按实验结果调优
- 当前流程输出相关表与列，不生成最终 SQL

## 13. 模块结构与扩展点

| 模块 | 职责 | 替换/修改场景 |
|---|---|---|
| `jev/mentions.py` | 一次生成式 LLM 抽取 mention | 修改抽取 Prompt 或模型 |
| `jev/candidates.py` | 共享原语：紧凑 schema、Noul/Score 解析 | 修改解析口径 |
| `jev/table_candidates.py` | 表候选、ID、Noul 排序与过滤 | 修改表级召回策略 |
| `jev/fk_candidates.py` | 从 gold schema 提取外键连接、连接 ID、连接选择 | 修改外键候选来源或阈值 |
| `jev/column_candidates.py` | 列候选、列问题 ID、Score 过滤 | 修改列级过滤规则 |
| `jev/request_builder.py` | 步骤 1 / 步骤 2 的 state 与 questions | 修改问题格式、指令或输出约束 |
| `jev/table_stage.py` | 步骤 1 执行（构建、提交、选表） | 修改表级阶段 |
| `jev/column_stage.py` | 步骤 2 执行（按表提交、分批、合并） | 修改列级阶段或分批策略 |
| `jev/finalize.py` | 步骤 3/4：阈值过滤与强制外键 | 修改最终输出规则 |
| `jev/cascade.py` | 四步编排，组装记录 | 增加/替换步骤 |
| `jev/jev_client.py` | JEV HTTP 传输 | 修改端点、鉴权或协议 |
| `jev/pipeline.py` | 加载资源、抽取 mention、调用 cascade、组装记录 | 修改整体流水线 |
| `jev/evaluation.py` | 从 GT SQL 探查表/列、计算 P/R/EM | 修改评测口径 |
| `jev/error_analysis.py` | 关联错例与原始 answers，写报告 | 修改归因或报告 |
| `jev/stage_utils.py` | 分批、usage 合并、答案合并 | 修改阶段公共逻辑 |

依赖方向：

```text
pipeline
  ├── mentions
  └── cascade
        ├── table_stage    -> table_candidates / request_builder / jev_client
        ├── column_stage   -> column_candidates / fk_candidates / request_builder / jev_client
        └── finalize       -> column_candidates / fk_candidates
```

各模块单向依赖，无循环。步骤之间只通过普通字典传递数据，替换某个步骤不影响其他步骤。

## 14. 运行方式

环境：conda `znws`，环境变量从项目根目录 `.env` 加载。

单题：

```powershell
conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev_table_first.yaml --question-id 1471
conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev_table_first.yaml --question-id 1471 --debug
```

批量：

```powershell
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first.yaml
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first.yaml --limit 5
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first.yaml --resume
```

`--resume` 只跳过此前 `status=ok` 的题目。

## 15. 评测与错误分析

评测与推理共用配置文件：

```powershell
conda run -n znws python scripts/evaluate_schema_linking.py --config configs/pipeline_minidev_table_first.yaml
conda run -n znws python scripts/evaluate_schema_linking.py --config configs/pipeline_minidev_table_first.yaml --limit 20
```

输出：

```text
<output_dir>/reports/schema_linking.md
<output_dir>/reports/schema_linking.json
```

GT 表/列由 SQLite authorizer 探查，不使用字面匹配。预测表集合来自 `linked_columns_union`，因此强制外键列会同时影响列指标与表集合。

错误分析：

```powershell
conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev_table_first.yaml
conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev_table_first.yaml --sample-size 20 --seed 7
```

输出：

```text
<output_dir>/analysis/error_cases.jsonl
<output_dir>/reports/error_analysis.md
```

对新记录的少选列归因：

- `not_in_stage1`：该列所属表未进入候选表集，或该列未参与打分
- `below_threshold`：已打分但 Score 低于阈值
- `no_answer`：缺少对应 Score 答案
- `selected_but_missing`：已过阈值但未进入最终结果

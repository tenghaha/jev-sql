# JEV 两阶段 Schema Linking 机制说明

本文档说明当前实验代码中的 JEV 请求/响应格式、两阶段级联机制、筛选规则、日志与输出结构。示例数据来自 BIRD MINIDEV 的 `question_id=1471`（`debit_card_specializing`）。

## 1. 总体流程

```text
NL query + evidence
        │
        ▼
[1] 生成式 LLM 一次性抽取全部 mention
        │  m1..mM
        ▼
[2] 列候选索引：把数据库中 C 个列扁平化为 c1..cC
        │
        ▼
[3] Stage 1：C 个 Noul
    query + 全部 mentions 作为共享 state；
    每个列问一次“该列是否与问题相关”
        │  noul(c1)..noul(cC)
        ▼
[4] 按 Noul 排序，保留前 a 个候选列
        │
        ▼
[5] Stage 2：M × a 个 Score
    每个 mention 对每个候选列问一次匹配强度
        │  score(m1,c)..score(mM,c)
        ▼
[6] 按阈值过滤，得到最终相关列
        │
        ▼
linked_columns / linked_columns_union
```

问题数量：

```text
N = C + M × a
```

- `C`：当前数据库的列数
- `M`：抽取出的 mention 数
- `a`：Stage 1 保留的候选列数，默认 12

本流程不再使用 Choice 原语。

## 2. 输入数据模型

Schema 由 `jev/schema/bird_json.py` 生成，结构为：

```text
dataset
└── databases[]
    └── tables[]
        ├── name
        ├── primary_key[]
        ├── adjacent_tables[]
        │   ├── table
        │   └── foreign_keys[[child.column, parent.column], ...]
        └── columns[]
            ├── name
            ├── type
            ├── sample
            └── date_format
```

每个列在进入 JEV 前会被扁平化为一个候选对象：

```json
{
  "column_name": "Currency",
  "table_name": "customers",
  "dtype": "text",
  "sample": "EUR",
  "date_format": null,
  "table_neighbors": [
    {
      "table": "yearmonth",
      "via": "yearmonth.CustomerID -> customers.CustomerID",
      "direction": "incoming"
    }
  ],
  "column_role_hint": "attribute"
}
```

字段含义：

| 字段 | 含义 |
|---|---|
| `column_name` | 列名 |
| `table_name` | 所属表名 |
| `dtype` | BIRD 类型：`text` / `integer` / `real` / `date` |
| `sample` | 从真实 SQLite 抽取的 1 条非空样本；全空列为 `null` |
| `date_format` | 日期列的 `strptime` 格式，如 `%Y-%m-%d` |
| `table_neighbors` | 一跳邻接表、外键路径与方向；`incoming` 表示邻表引用本表 |
| `column_role_hint` | `primary_key` / `foreign_key` / `temporal` / `measure` / `attribute` |

候选 ID 按 schema 顺序稳定分配：`c1..cC`。Stage 1 与 Stage 2 使用同一套 ID。

## 3. Mention 抽取

`jev/mentions.py` 对每道题只调用一次生成式 LLM。

Prompt 规则（英文）：

- 一次抽取全部 mention
- 文本完全按问题或 evidence 中的原样复制
- 相同文本只保留一次，并重新编号 `m1..mM`
- `span_type` 取值：`entity` / `entity_attribute` / `numeric_filter` / `temporal_filter` / `aggregation` / `join_relation` / `other`
- 只返回 JSON，不允许解释或 Markdown 代码块

输出示例：

```json
{
  "mentions": [
    {"span_id": "m1", "text": "ratio", "span_type": "aggregation"},
    {"span_id": "m2", "text": "customers", "span_type": "entity"},
    {"span_id": "m3", "text": "EUR", "span_type": "other"}
  ]
}
```

## 4. JEV 传输层

`jev/jev_client.py` 直接向 `JEV_API_BASE` 发送 HTTP POST，不再尝试 OpenAI 兼容接口。

请求顶层结构：

```json
{
  "model": "typesafe-ai/jev",
  "state": { "...": "..." },
  "questions": { "...": "..." }
}
```

- Header：`Authorization: Bearer <AI_GATEWAY_API_KEY>`
- 环境变量：`JEV_API_BASE`、`AI_GATEWAY_API_KEY`
- `timeout` 由配置控制，默认 120s
- `questions` 为空时跳过网络请求，返回空答案
- 返回内容原样保留 `answers`、`usage`、`provider_metadata`
- `state.output_schema` 只是提示；实际响应可能包含 `legend`、`probabilities` 等额外字段

`state` 与 `questions` 在 Stage 1 / Stage 2 中分别构造。

## 5. Stage 1：全列 Noul 筛选

### 5.1 目的

使用整条问题和全部 mention，对数据库中的每个列判断一次“该列是否与问题相关”。

### 5.2 State

```json
{
  "task": "schema_linking_stage1",
  "instruction": "Decide whether the question refers to each candidate column, using the evidence spans as its mentions. Each candidate is a column of the database. Answer only the provided questions.",
  "context": {
    "database": "debit_card_specializing",
    "question": "What is the ratio of customers who pay in EUR against customers who pay in CZK?",
    "evidence": "ratio of customers who pay in EUR against customers who pay in CZK = count(Currency = 'EUR') / count(Currency = 'CZK').",
    "evidence_spans": [
      {"span_id": "m1", "text": "ratio", "span_type": "aggregation"},
      {"span_id": "m2", "text": "customers", "span_type": "entity"},
      {"span_id": "m3", "text": "pay", "span_type": "other"},
      {"span_id": "m4", "text": "EUR", "span_type": "other"},
      {"span_id": "m5", "text": "CZK", "span_type": "other"},
      {"span_id": "m6", "text": "Currency", "span_type": "entity_attribute"},
      {"span_id": "m7", "text": "count", "span_type": "aggregation"}
    ],
    "schema_summary": "customers(CustomerID integer PK, Segment text, Currency text); gasstations(...); ..."
  },
  "candidates": {
    "c1": {
      "column_name": "CustomerID",
      "table_name": "customers",
      "dtype": "integer",
      "sample": 3,
      "date_format": null,
      "table_neighbors": [
        {
          "table": "yearmonth",
          "via": "yearmonth.CustomerID -> customers.CustomerID",
          "direction": "incoming"
        }
      ],
      "column_role_hint": "primary_key"
    },
    "c3": {
      "column_name": "Currency",
      "table_name": "customers",
      "dtype": "text",
      "sample": "EUR",
      "date_format": null,
      "table_neighbors": [
        {
          "table": "yearmonth",
          "via": "yearmonth.CustomerID -> customers.CustomerID",
          "direction": "incoming"
        }
      ],
      "column_role_hint": "attribute"
    }
  },
  "output_schema": {
    "noul": "probability in [0, 1]"
  }
}
```

`context.evidence_spans` 就是全部 mention，只在 state 中出现一次；`schema_summary` 是紧凑的全库 schema 文本，主键标 `PK`，不包含样本值。为便于阅读，上例的 `candidates` 只展示 2 个；实际每个请求包含 `stage1_column_batch` 个候选。

### 5.3 Questions

每个列一个 Noul：

```json
{
  "s1_c1": {
    "type": "noul",
    "instructions": "Is the database column customers.CustomerID (integer) relevant to the question, given its evidence spans? Use the column name, role, sample value, and neighboring tables as evidence."
  },
  "s1_c3": {
    "type": "noul",
    "instructions": "Is the database column customers.Currency (text) relevant to the question, given its evidence spans? Use the column name, role, sample value, and neighboring tables as evidence."
  }
}
```

问题 ID 规则：

```text
s1_<candidate_id>
```

### 5.4 Response

```json
{
  "model": "typesafe-ai/jev",
  "answers": {
    "s1_c1": {"type": "noul", "noul": 0.21},
    "s1_c3": {"type": "noul", "noul": 0.99}
  },
  "usage": {"input_tokens": 4242, "output_tokens": 419}
}
```

- `noul`：该列与问题相关的概率，范围 `[0, 1]`
- `answers` 的 key 与提交的 question ID 一一对应
- 上例为答案节选；`usage` 是整个 Stage 1（所有批次）的累计值

### 5.5 候选选择

Stage 1 结束后，对全部 `C` 个列按 `noul` 降序排序：

```text
c3  customers.Currency       0.99
c19 yearmonth.CustomerID     0.34
c1  customers.CustomerID     0.21
...
```

保留前 `a` 个作为 Stage 2 候选，默认 `a=12`。排序稳定，分数相同时按候选 ID 升序。

记录中保存：

```json
{
  "candidate_id": "c3",
  "table": "customers",
  "column": "Currency",
  "dtype": "text",
  "noul": 0.99
}
```

### 5.6 分批提交

`stage1_column_batch` 控制每个 JEV 请求包含多少列，默认 15。

- 每批 state 仍包含完整的 query、全部 mentions、schema summary
- `candidates` 只包含该批的列
- 每个批次提交 `batch_size` 个 Noul 问题
- 所有批次的 answers 合并后统一排序

这是传输层的分批，不改变语义：每个列仍然只问一次。

分批是必要的：BIRD 中最大库有 455 列。实测在 199 列的库上：

- 30 列/批（30 个候选 + 30 个问题）→ 部分批次仍会 `400`
- **15 列/批稳定通过**

设置 `stage1_column_batch: 0` 表示单次提交全部列，但大库可能触发上下文上限。

## 6. Stage 2：Mention × 候选列 Score

### 6.1 目的

在 Stage 1 的 `a` 个候选列中，为每个 mention 判断它与每个候选列的匹配强度，用于消歧与阈值过滤。

### 6.2 State

与 Stage 1 相同，但 `task` 和 `candidates` 不同：

```json
{
  "task": "schema_linking_stage2",
  "instruction": "Score how strongly each mention matches each candidate column. Only the listed candidates are under consideration. Answer only the provided questions.",
  "context": {
    "database": "debit_card_specializing",
    "question": "...",
    "evidence": "...",
    "evidence_spans": ["... 全部 mention ..."],
    "schema_summary": "..."
  },
  "candidates": {
    "c3": {"...": "customers.Currency"},
    "c19": {"...": "yearmonth.CustomerID"}
  },
  "output_schema": {
    "score": "expected level index",
    "confidence": "probability in [0, 1]"
  }
}
```

`candidates` 只包含 Stage 1 选中的 `a` 个列。上例只展示 2 个候选。

### 6.3 Questions

每个 `(mention, candidate)` 一个 Score：

```json
{
  "s2_m1_c3": {
    "type": "score",
    "instructions": "How strongly does the database column customers.Currency match the mention \"ratio\"?",
    "criteria": ["irrelevant", "weak", "relevant", "strong"]
  },
  "s2_m1_c19": {
    "type": "score",
    "instructions": "How strongly does the database column yearmonth.CustomerID match the mention \"ratio\"?",
    "criteria": ["irrelevant", "weak", "relevant", "strong"]
  }
}
```

问题 ID 规则：

```text
s2_<mention_span_id>_<candidate_id>
```

问题数：

```text
M × a
```

### 6.4 Response

```json
{
  "answers": {
    "s2_m1_c3": {
      "type": "score",
      "score": 0.19,
      "confidence": 0.8,
      "legend": {
        "0": "irrelevant",
        "1": "weak",
        "2": "relevant",
        "3": "strong"
      },
      "probabilities": {
        "0": 0.86,
        "1": 0.1,
        "2": 0.02,
        "3": 0.02
      }
    },
    "s2_m1_c19": {
      "type": "score",
      "score": 0.07,
      "confidence": 0.91,
      "legend": {
        "0": "irrelevant",
        "1": "weak",
        "2": "relevant",
        "3": "strong"
      },
      "probabilities": {
        "0": 0.94,
        "1": 0.04,
        "2": 0.01,
        "3": 0.01
      }
    }
  }
}
```

- `score`：概率加权的期望等级，范围 `[0, 3]`
- `confidence`：模型给出的置信度
- `legend`：等级索引到标签的映射
- `probabilities`：各等级的概率分布

### 6.5 不使用的原语

当前流程只使用：

- Noul：Stage 1 的列级相关性
- Score：Stage 2 的 mention-列匹配强度

不使用 Choice。候选消歧由 Stage 2 的 Score 排序与阈值完成。

## 7. 最终过滤与输出

`jev/candidates.py::filter_by_score` 对每个 mention：

1. 遍历 Stage 1 的 `a` 个候选
2. 读取对应 `s2_<mention>_<candidate>` 的 `score`
3. 保留 `score >= stage2_score_threshold` 的列，默认阈值 `2.0`
4. 按 score 降序排列

阈值含义：

| score | 含义 | 默认是否保留 |
|---:|---|---|
| 0 | irrelevant | 否 |
| 1 | weak | 否 |
| 2 | relevant | 是 |
| 3 | strong | 是 |

输出一：按 mention 组织：

```json
{
  "span_id": "m4",
  "text": "EUR",
  "span_type": "other",
  "columns": [
    {
      "candidate_id": "c3",
      "table": "customers",
      "column": "Currency",
      "dtype": "text",
      "score": 2.92,
      "confidence": 0.92
    }
  ]
}
```

没有通过阈值的 mention 仍会保留，`columns` 为空，便于分析召回情况。

输出二：去重后的最终相关列：

```json
{
  "table": "customers",
  "column": "Currency",
  "dtype": "text",
  "best_score": 2.98,
  "mentions": ["m3", "m4", "m5", "m6"]
}
```

- `best_score`：该列在所有 mention 上的最高 Score
- `mentions`：命中该列的 mention 列表
- 按 `best_score` 降序排列

## 8. 配置项

`configs/pipeline_minidev.yaml`：

```yaml
dataset: BIRD
split: MINIDEV
schema_path: outputs/schema/minidev_schema.json
questions_path: BIRD/MINIDEV/mini_dev_sqlite.json
databases_dir: BIRD/MINIDEV/dev_databases
output_dir: outputs/runs/jev_minidev_cascade

mention_llm:
  model_env: OPENAI_API_MODEL
  api_key_env: OPENAI_API_KEY
  base_url_env: OPENAI_API_BASE
  temperature: 0.0
  timeout: 120
  max_retries: 2

cascade:
  stage1_candidates: 12      # a
  stage1_column_batch: 15    # 每批列数；0 表示单次提交全部列
  score_scale: [irrelevant, weak, relevant, strong]
  stage2_score_threshold: 2.0

jev:
  model: typesafe-ai/jev
  endpoint_env: JEV_API_BASE
  api_key_env: AI_GATEWAY_API_KEY
  timeout: 120

# All paths below are relative to output_dir.
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
| `output_dir` | - | 单组实验的统一输出根目录 |
| `databases_dir` | - | BIRD SQLite 数据库根目录，供评测探查结构 |
| `stage1_candidates` | 12 | Stage 1 保留的候选列数 `a` |
| `stage1_column_batch` | 15 | Stage 1 每个 JEV 请求的列数；`0` 为单次全量 |
| `score_scale` | 4 级 | Score 的有序等级 |
| `stage2_score_threshold` | 2.0 | 最终保留候选的 Score 阈值 |
| `eval.preds_file` | `batch_answers.jsonl` | 预测文件，相对 `output_dir` |
| `eval.report_file` | `schema_linking.md` | 评测报告，相对 `output_dir/reports` |
| `eval.details_file` | `schema_linking.json` | 逐样本明细，相对 `output_dir/reports` |
| `eval.include_missing` | `false` | 是否把缺失预测计为空集 |
| `analysis.sample_size` | 12 | 详细分析抽样的错例数 |
| `analysis.seed` | 42 | 错例抽样随机种子 |
| `analysis.error_cases_file` | `error_cases.jsonl` | 中间关联文件，相对 `output_dir/analysis` |
| `analysis.report_file` | `error_analysis.md` | 错误分析报告，相对 `output_dir/reports` |
| `mention_llm.*` | - | 生成式 LLM 的环境变量名与超时 |
| `jev.*` | - | JEV 模型、端点、密钥环境变量与超时 |

## 9. 日志与 `--debug`

`jev/logging_utils.py` 统一配置日志。推理脚本均支持 `--debug`：

```powershell
conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev.yaml --question-id 1471 --debug
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev.yaml --limit 5 --debug
```

`INFO` 级别输出：

- 抽取的 mention 数
- Stage 1 每批列数与问题数
- Stage 1 保留的候选数
- Stage 2 问题数
- 最终 mention 链接数与唯一列数
- 每阶段耗时与 token usage

`DEBUG` 级别额外输出：

- mention 抽取的原始 LLM 输出
- **完整 JEV request**（`model`、`state`、`questions`）
- **完整 JEV response**（`answers`、`usage`、`provider_metadata`）
- Stage 1 选中候选详情
- 最终 `linked_columns`

日志默认写到 stderr；单题的最终 JSON 打到 stdout，批量结果写入 JSONL。

## 10. 输出记录

### 10.1 单题

`<output_dir>/single/q<question_id>.json`

### 10.2 批量

`<output_dir>/batch_answers.jsonl`，每题一行。

顶层字段：

| 字段 | 含义 |
|---|---|
| `question_id` | BIRD 题号；无该字段时用记录下标 |
| `db_id` | 数据库 |
| `question` / `evidence` | 原问题与证据 |
| `mentions` | 抽取出的 mention 列表 |
| `stage1` | Stage 1 汇总与选中候选 |
| `stage2` | Stage 2 答案 |
| `linked_columns` | 按 mention 组织的最终相关列 |
| `linked_columns_union` | 去重后的最终相关列 |
| `usage` | 两阶段 token usage |
| `elapsed` | mention / stage1 / stage2 / total 耗时 |
| `status` | 批量记录中的 `ok` / `failed` |

`stage1` 子字段：

| 字段 | 含义 |
|---|---|
| `num_candidates` | 全库列数 `C` |
| `num_questions` | Stage 1 问题数（等于 `C`） |
| `num_batches` | Stage 1 批次数 |
| `column_batch` | 每批列数 |
| `selection.limit` | `a` |
| `selected_candidates` | 选中的候选及 Noul 分数 |
| `elapsed_seconds` | 所有 Stage 1 批次累计耗时 |
| `usage` | 所有 Stage 1 批次累计 usage |

`stage2` 子字段：

| 字段 | 含义 |
|---|---|
| `num_candidates` | `a` |
| `num_questions` | `M × a` |
| `score_scale` | Score 等级 |
| `score_threshold` | 过滤阈值 |
| `answers` | 全部 Stage 2 答案 |
| `elapsed_seconds` | Stage 2 耗时 |
| `usage` | Stage 2 token usage |

为控制批量 JSONL 体积，**Stage 1 的全量 Noul 答案不写入记录**；只在 `--debug` 日志中保留。记录中保留 Stage 1 选中候选的 Noul 分数与 Stage 2 全部答案。

## 11. 成本与限制

### 11.1 问题数

```text
C + M × a
```

实测：

| 题目 | 列数 C | mentions M | Stage 1 | Stage 2 | 合计 |
|---|---:|---:|---:|---:|---:|
| q1471 | 21 | 7 | 21 | 84 | 105 |
| q1025 | 199 | 8 | 199 | 96 | 295 |

### 11.2 上下文限制

Stage 1 每批都需要重复输入 schema summary 与候选状态。列数越多、每批列数越大，输入 token 越高。

- 15 列/批在 199 列库上稳定通过
- 30 列/批在部分批次会超过上下文上限并返回 `400`
- 455 列的 train 库建议保持 15 或更小

### 11.3 其他限制

- Stage 1 判断的是“列是否与问题相关”，不区分具体 mention；mention 级消歧由 Stage 2 完成
- `a=12` 可能漏掉低 Noul 但实际需要的列；可增大 `stage1_candidates`，代价是 Stage 2 问题数线性增加
- 阈值 `2.0` 较严格；若召回不足可降到 `1.5` 或 `1.0`
- 当前流程只输出相关列，尚未生成最终 SQL

## 12. 模块结构与扩展点

| 模块 | 职责 | 替换/修改场景 |
|---|---|---|
| `jev/schema/bird_json.py` | 从 BIRD 元数据与 SQLite 构建 schema JSON | 修改 schema 建模字段 |
| `jev/mentions.py` | 一次生成式 LLM 抽取 mention | 修改抽取 Prompt 或模型 |
| `jev/candidates.py` | 列扁平化、候选 ID、Stage 1 排序、阈值过滤 | 修改候选策略或过滤规则 |
| `jev/request_builder.py` | Stage 1 / Stage 2 的 state 与 question 组装 | 修改问题格式、指令或输出约束 |
| `jev/cascade.py` | 两阶段编排、分批、阶段间数据传递 | 修改级联逻辑或增加阶段 |
| `jev/jev_client.py` | JEV HTTP 传输与日志 | 修改端点、鉴权或传输协议 |
| `jev/pipeline.py` | 加载资源、抽取 mention、调用 cascade、组装记录 | 修改整体流水线 |
| `jev/evaluation.py` | 从 GT SQL 探查表/列、计算 P/R/EM、写报告 | 修改评测口径或指标 |
| `jev/error_analysis.py` | 关联原始 answers 与错例、归因、写分析报告 | 修改错误分类或报告结构 |
| `jev/logging_utils.py` | 日志配置 | 修改日志级别与格式 |

依赖方向：

```text
pipeline
  ├── mentions
  └── cascade
        ├── candidates
        ├── request_builder -> candidates
        └── jev_client
```

各模块单向依赖，无循环。修改 Stage 1/2 的筛选逻辑只需改 `candidates.py`；修改 JEV 请求格式只需改 `request_builder.py`；更换 JEV 接口只需改 `jev_client.py`。

## 13. 运行方式

环境：conda `znws`，环境变量从项目根目录 `.env` 加载。

单题：

```powershell
conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev.yaml --question-id 1471
conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev.yaml --question-id 1471 --debug
```

批量：

```powershell
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev.yaml --limit 5
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev.yaml --resume
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev.yaml --limit 5 --debug
```

`--resume` 只跳过此前 `status=ok` 的题目；失败题会重试。

## 14. 评测

评测与推理共用同一份配置：

```powershell
conda run -n znws python scripts/evaluate_schema_linking.py --config configs/pipeline_minidev.yaml
conda run -n znws python scripts/evaluate_schema_linking.py --config configs/pipeline_minidev.yaml --limit 20
```

默认读取 `<output_dir>/batch_answers.jsonl`，报告写入：

```text
<output_dir>/reports/schema_linking.md
<output_dir>/reports/schema_linking.json
```

可用 `--preds`、`--report`、`--details` 覆盖路径。

GT SQL 的表和列由 SQLite authorizer 在 `EXPLAIN` 阶段探查，不使用字面匹配；支持别名、CTE、子查询、UNION 与 `SELECT *`。指标包括：

- 表 Precision / Recall / F1
- 列 Precision / Recall / F1（`table.column` 限定）
- EM：表集合与列集合同时完全一致
- Table EM / Column EM
- 按数据库拆分的 P/R/EM

主指标为跨样本 micro 汇总；`status=failed` 的预测会跳过并单独计数。

## 15. 错误分析

错误分析读取评测明细和原始 pipeline answers，生成中间关联文件与 Markdown 报告：

```powershell
conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev.yaml
conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev.yaml --sample-size 20 --seed 7
conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev.yaml --preds <path> --threshold 2.0
```

可用覆盖参数：`--eval-details`、`--preds`、`--cases`、`--report`、`--sample-size`、`--seed`、`--threshold`。

输入：

- `<output_dir>/reports/schema_linking.json`
- `<output_dir>/batch_answers.jsonl`
- `questions_path` 中的问题、evidence 与 gold SQL

输出：

```text
<output_dir>/analysis/error_cases.jsonl
<output_dir>/reports/error_analysis.md
```

`error_cases.jsonl` 每个评测样本一行，包含：

- GT / 预测的表与列、多选与少选
- 原始 mentions、Stage 1 选中候选、Stage 2 answers、`linked_columns`
- 少选列归因：未进入 Stage 1、Score 低于阈值、缺少答案、已过阈值但未进入最终结果
- 多选列归因：best score、关联 mention 与 Score

Markdown 报告包含总体指标、表/列的多选/少选分布、平均 FP/FN、少选归因、多选 Score 分布、高频错误列、按数据库拆分，以及固定随机种子的错例抽样详细分析。

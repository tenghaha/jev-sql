# JEV Schema Linking 实验平台

在 BIRD（如 MINIDEV）上运行基于 JEV 决策模型的 schema linking 实验：生成式 LLM 抽取问题中的 mention，JEV 判断表/列相关性，输出最终表与列，并提供评测与错误分析。

所有实验由 YAML 配置驱动，一个配置文件对应一组实验，产物统一写入该配置的 `output_dir`。

## 目录结构

```text
jev/                         核心库
  schema/bird_json.py        从 BIRD 元数据与 SQLite 构建结构化 schema
  mentions.py                一次生成式 LLM 调用抽取全部 mention
  candidates.py              JEV 答案解析、紧凑 schema 等共享原语
  table_candidates.py        步骤 1：表级候选与 Noul 排序
  table_stage.py             步骤 1：构建请求、提交、选表
  fk_candidates.py           步骤 3：结果表配对、连接列解析
  column_candidates.py       步骤 2：列候选、Score 过滤
  column_stage.py            步骤 2：按表提交列打分
  ambiguous_stage.py         步骤 2b：模糊列二次判定（`column_select_mode: ambiguous`）
  finalize.py                步骤 3：列筛选与最终列集合
  result_join_stage.py       步骤 3：结果表连接列补全
  cascade.py                 四步流程编排
  request_builder.py         各步骤 state / questions 组装
  jev_client.py              JEV HTTP 传输
  pipeline.py                单题流水线：mention → cascade → 记录
  evaluation.py              评测：GT 表/列探查、P/R/F1、EM
  error_analysis.py          错例关联、归因与报告
  config.py / logging_utils.py / stage_utils.py

scripts/                     CLI 入口
configs/                     YAML 实验配置
docs/                        机制文档
outputs/                     运行产物（gitignore）
BIRD/                        数据集（gitignore）
```

## 环境准备

- 需要 Python 3.11（开发环境为 conda `znws`，Python 3.11.15）。
- 第三方依赖见 `requirements.txt`，只包含代码显式 import 的包及其锁定版本（`langchain-core`、`openai`、`tiktoken` 为 `langchain-openai` 的关键传递依赖，一并锁定）。

```powershell
conda create -n znws python=3.11 -y
conda activate znws
pip install -r requirements.txt
```

- 环境变量从项目根目录 `.env` 读取（`.env` 已在 `.gitignore` 中）：

```text
OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_API_MODEL     生成式 LLM（mention 抽取）
AI_GATEWAY_API_KEY / JEV_API_BASE                      JEV 决策模型
```

- 数据集 `BIRD/` 与运行产物 `outputs/` 默认不入库，需本地准备后放在项目根目录。
- 所有命令统一用 `conda run -n znws python ...` 执行。

- mention 抽取通过 LangChain `ChatOpenAI` 调用，并固定传入 `enable_thinking=false` 关闭思考模式。

## 实验流程

```text
schema 构建 → 单题/批量推理 → schema linking 评测 → 错误分析 → EX 评测
```

## 配置

当前分支（`dev_tc`，先表后列）使用 [configs/pipeline_minidev_table_first.yaml](configs/pipeline_minidev_table_first.yaml)：

| 字段 | 说明 |
|---|---|
| `dataset` / `split` | 数据集与划分名 |
| `schema_path` | 结构化 schema JSON |
| `questions_path` | BIRD 问题文件 |
| `databases_dir` | SQLite 根目录，供评测探查 GT 结构 |
| `output_dir` | 本组实验产物根目录 |
| `mention_llm.*` | 生成式 LLM 的环境变量名、温度、超时 |
| `cascade.table_candidates` | 步骤 1 最多保留的候选表数 |
| `cascade.table_noul_threshold` | 表级 Noul 阈值，`0` 表示只按 top-k 截断 |
| `cascade.table_column_batch` | 步骤 2 每个请求的列数，`0` 表示整表一次提交 |
| `cascade.score_scale` | 列 Score 等级 |
| `cascade.column_score_threshold` | 列 Score 阈值（`hard` 模式） |
| `cascade.column_select_mode` | 列筛选模式：`hard` 单阈值 / `ambiguous` 双阈值 + 二次判定 |
| `jev.*` | JEV 模型、端点与密钥环境变量 |
| `eval.*` / `analysis.*` | schema linking 评测与错误分析产物文件名（相对 `output_dir`） |
| `sql_generation.*` | EX 评测中一次性 SQL 生成的生成式 LLM 环境变量、温度与超时 |
| `ex.*` | EX 评测的输入/输出文件名与执行超时（相对 `output_dir`） |

新增实验时复制配置文件后修改 `output_dir`，避免覆盖既有结果。

## 脚本用法

### 1. 构建 schema

```powershell
conda run -n znws python scripts/build_schema.py --config configs/schema_minidev.yaml
```

配置字段：`tables_json`、`databases_dir`、`output_path`、`db_ids`。产物：

```text
outputs/schema/minidev_schema.json
```

### 2. 单题推理

```powershell
conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev_table_first.yaml --question-id 1471
conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev_table_first.yaml --question-id 1471 --debug
conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev_table_first.yaml --question-id 1471 --no-save
```

| 参数 | 说明 |
|---|---|
| `--config` | 实验配置 |
| `--question-id` | BIRD `question_id`；不存在时回退为记录下标 |
| `--debug` | 输出完整 JEV 请求/响应与中间结果 |
| `--no-save` | 只打印，不写单题记录 |

产物：`<output_dir>/single/q<question_id>.json`（未加 `--no-save` 时）。

### 3. 批量推理

```powershell
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first.yaml
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first.yaml --limit 5
conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first.yaml --resume
```

| 参数 | 说明 |
|---|---|
| `--limit` | 只跑前 N 题，`0` 为全部 |
| `--resume` | 跳过此前 `status=ok` 的题目 |
| `--debug` | 输出完整 JEV 请求/响应 |

产物：`<output_dir>/batch_answers.jsonl`，每题一行，`status` 为 `ok` / `failed`。

### 4. 评测

```powershell
conda run -n znws python scripts/evaluate_schema_linking.py --config configs/pipeline_minidev_table_first.yaml
conda run -n znws python scripts/evaluate_schema_linking.py --config configs/pipeline_minidev_table_first.yaml --limit 20
```

| 参数 | 说明 |
|---|---|
| `--preds` / `--report` / `--details` | 覆盖默认预测、报告与明细路径 |
| `--limit` | 只评测前 N 题 |
| `--include-missing` | 将缺失预测按空集计入 |

指标：表 P/R/F1、列 P/R/F1（`table.column`）、EM（表与列同时完全匹配）、Table EM、Column EM、按数据库拆分。GT 表/列通过 SQLite authorizer 探查 gold SQL，不做字面匹配。

产物：

```text
<output_dir>/reports/schema_linking.md
<output_dir>/reports/schema_linking.json
```

### 5. 错误分析

```powershell
conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev_table_first.yaml
conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev_table_first.yaml --sample-size 20 --seed 7
```

| 参数 | 说明 |
|---|---|
| `--eval-details` / `--preds` | 覆盖评测明细与预测路径 |
| `--cases` / `--report` | 覆盖中间关联文件与报告路径 |
| `--sample-size` / `--seed` | 抽样数量与随机种子 |
| `--threshold` | 列 Score 阈值，用于少选归因 |

产物：

```text
<output_dir>/analysis/error_cases.jsonl
<output_dir>/reports/error_analysis.md
```

报告包含总体指标、表/列多选与少选分布、平均 FP/FN、少选归因、多选 Score 分布、高频错误列、按数据库拆分，以及错例抽样详细分析。

### 6. EX 评测（端到端执行准确率）

```powershell
conda run -n znws python scripts/evaluate_ex.py --config configs/pipeline_minidev_table_first_result_join.yaml
conda run -n znws python scripts/evaluate_ex.py --config configs/pipeline_minidev_table_first_result_join.yaml --limit 20
conda run -n znws python scripts/evaluate_ex.py --config configs/pipeline_minidev_table_first_result_join.yaml --resume
conda run -n znws python scripts/evaluate_ex.py --config configs/pipeline_minidev_table_first_result_join.yaml --no-generate
```

| 参数 | 说明 |
|---|---|
| `--limit` | 只跑前 N 题，`0` 为全部 |
| `--resume` | 复用已生成的 SQL，跳过重复生成 |
| `--no-generate` | 不调用 LLM，直接评测已有生成缓存 |
| `--workers` | SQL 执行并行度，缺省按 CPU 自动 |

流程：读取批量推理的 `batch_answers.jsonl`，把每题 `linked_columns_union` 渲染为紧凑 schema，用 `sql_generation` 指定的生成式 LLM 一次性生成 SQL，在 SQLite 上执行并与 gold SQL 的结果集比较。

指标：EX（结果集相等，官方口径）、表/字段 P/R/F1（SQLite authorizer 在 `EXPLAIN` 阶段提取实际读取的表与列，micro 口径），并给出分难度与分数据库拆分。

产物：

```text
<output_dir>/ex_predictions.jsonl    每题生成的 SQL（可 --resume 复用）
<output_dir>/ex_results.json         执行结果与指标汇总（含每题明细）
<output_dir>/reports/execution.md    Markdown 评测报告
```

## 输出结构

```text
<output_dir>/
├── single/q<question_id>.json     单题完整记录
├── batch_answers.jsonl            批量记录（含 mentions、各阶段答案与最终列）
├── ex_predictions.jsonl           每题生成的 SQL（EX 评测）
├── ex_results.json                EX 执行结果与指标汇总
├── analysis/error_cases.jsonl     错例与原始 answers 的关联文件
└── reports/
    ├── schema_linking.md          评测报告
    ├── schema_linking.json        逐样本评测明细
    ├── error_analysis.md          错误分析报告
    └── execution.md               EX 评测报告
```

## 机制说明

本分支（`dev_tc`）采用**先表后列**：

1. 表级 Noul：用紧凑表 schema 判断每张表是否需要；
2. 按表打分：每个候选表单独提交，做 mention × 列 Score；
3. 列筛选：`hard` 模式按单阈值，`ambiguous` 模式按双阈值并对模糊列追加一轮 Noul；
4. 结果表连接补全：对含结果列的表两两配对，用两个 choice 问题分别判断两侧最可能的连接列，把结果加入列集合并去重。

连接列由 JEV 依据两张表的 schema 判定，不使用 schema 中的表间信息，也不使用 gold SQL。

详细说明见 [docs/jev_schema_linking_table_first.md](docs/jev_schema_linking_table_first.md)。

另一分支 `dev_2step` 保留列优先的两阶段级联机制：全列 Noul 粗筛后，对候选列做 mention × 列 Score 并按阈值过滤，说明见该分支的 `docs/jev_schema_linking.md`。

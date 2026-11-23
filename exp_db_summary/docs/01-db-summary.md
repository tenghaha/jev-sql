# 01 · 离线数据库摘要（表级卡片 + 连通图）

代码：`dbprofile/`（`sqlite_meta.py` 取元数据、`graph.py` 建图、`summarize.py` 写摘要、
`build.py` 组装、`llm.py` 可选的模型调用）。
配置：`configs/*.yaml`（一个 split 一个实验）。产物：`outputs/db_summary/<split>.json`。

## 1. 目的与边界

这一阶段只解决一件事：**在不看具体列的前提下，让下游模型大致知道每个库由哪些表组成、每张表是什么、
表之间怎么连**。所以产物是"压缩后的表级视图"，而不是完整 schema：

- **有**：表名、行数（量级）、列数、主键、外键（连到谁、用哪些列）、邻居表、几条样例取值、一句话摘要；
- **没有**：整张表的列清单（这是设计决定：列级信息留给后续阶段按需取，避免把 token 花在几十个
  用不到的列上）。

本阶段不组装 prompt、不调用模型做端到端推理；`summarizer: llm` 只是一条可选的离线摘要实现。

## 2. 输入与范围

| 配置 | 数据集 | 库数 | 来源 |
|---|---|---:|---|
| `bird_dev.yaml` | BIRD dev | 11 | `BIRD/dev_20240627/dev_databases/<db>/<db>.sqlite` |
| `bird_train.yaml` | BIRD train | 69 | `BIRD/train/train_databases/<db>/<db>.sqlite` |
| `bird_minidev.yaml` | BIRD MINIDEV | 11 | `BIRD/MINIDEV/dev_databases/<db>/<db>.sqlite` |
| `spider2_lite_local.yaml` | Spider2.0 lite-local | 30 | `Spider2/.../spider2-localdb/local_sqlite.zip` |

数据库一律以 **只读** 方式打开（`file:<path>?mode=ro`），不会修改任何数据集文件。
Spider2.0 的本地 SQLite 随数据集以 zip 形式发布，脚本第一次会把所需的 `.sqlite` 解到
`outputs/cache/spider2_lite_local/`（约 1.6 GB），之后复用，不重复解压。

## 3. 表级摘要

### 3.1 元数据（确定性，全部来自 PRAGMA）

| 字段 | 来源 |
|---|---|
| 表清单 | `sqlite_master WHERE type='table'`（排除 `sqlite_*`） |
| 列数与类型 | `PRAGMA table_info(t)` |
| 主键 | `PRAGMA table_info(t)` 的 `pk` 标记 |
| 外键 | `PRAGMA foreign_key_list(t)`，按约束 id 聚合成 `(from_col → to_col)` 对 |
| 行数 | `SELECT COUNT(*) FROM (SELECT 1 FROM t LIMIT cap+1)`，`cap=200000` |
| 样例 | `SELECT * FROM t LIMIT 2`，每个取值截断到 28 字符 |

行数故意做成**有上限的计数**：`rows_capped=true` 表示"至少 20 万行"。表级卡片不需要精确行数，
但一次全量跑不能被某张大表拖死（第一版无上限时 69 个库跑了 2 分钟以上）。

### 3.2 卡片字段

```json
{"name": "atom", "rows": 12333, "rows_capped": false, "n_columns": 3,
 "pk": ["atom_id"],
 "fk_out": [{"to_table": "molecule", "pairs": [["molecule_id", "molecule_id"]]}],
 "neighbours": ["molecule"],
 "sample": ["TR000_1", "TR000", "cl"],
 "summary": "atom: 12,333 rows, 3 cols; joins molecule; e.g. TR000_1, TR000, cl",
 "summary_chars": 66, "summary_source": "template"}
```

`sample` 只保留几条**互不相同、非空、非结构化**的取值（跳过 JSON/数组 blob），用来提示领域；
`summary` 是给人/模型读的一句话。

### 3.3 两种摘要实现

| `summarizer` | 做什么 | 是否需要网络 |
|---|---|---|
| `llm`（**当前产物用的就是这个**） | 用 `.env` 里的 OpenAI 兼容接口，让模型写**一句**不超过 `summary_max_words` 词的话 | 是 |
| `template`（确定性回退） | 按固定模板拼接：`表名: 行数 rows, 列数 cols; joins 邻居; e.g. 样例` | 否 |

两条路径写同一个字段（`summary`），只在 `summary_source` 上不同，产物 schema 不变。
本次 1,084 张表全部是 `summary_source: "llm"`，`llm_fallbacks = 0`。

四条工程约束：

1. **缓存**：`outputs/cache/<scope>/llm_summaries.json`，键是 `dataset|db|table|model|prompt_version`
   （**不含 split**：prompt 里没有 split，所以 MINIDEV 直接复用 dev 的 75 条摘要，不重复付费）。
   重跑同一批数据 6 s 完成、0 次 API 调用。
2. **预算闸门**：`llm_max_tables`（默认 10，本次设 0 = 不限）限制单次运行里**未缓存**的调用数，
   超预算直接退出并提示，防止误触全量付费。
3. **失败回退**：单次调用失败（网络/HTTP/空回答）回退到 template，不中断整库；
   `stats` 里分开记：`llm_calls`（**本次运行**的真实 API 调用）、`llm_cache_hits`、
   `llm_fallbacks`，以及 `summary_sources`（产物里每张卡的来源，重跑命中缓存时它仍是
   `{"llm": N}`，而 `llm_calls` 为 0）。
4. **推理模型的 token 预算**：本机模型是 reasoning 模型，会先写 `reasoning_content` 再写答案。
   第一版 `max_tokens=120` 时**回答为空**（`finish_reason=length`，token 全花在推理上）。
   现在 `llm_max_tokens=600`，且当 `content` 为空时自动用 2× 预算重试一次。

摘要 prompt（英文，输入含列名与样例，输出禁止列清单）：

```
You are describing one database table for a text-to-SQL assistant.
The description is a compressed memory aid, not a schema dump.

Database: <db_id>
Table: <table>
Rows: <rows>
Columns (<n>): <non-PK column names>
Primary key: <pk>
Foreign keys: <from->to to table>
Sample rows: <2 rows as JSON, truncated>

Write ONE sentence of at most <k> words that says what this table stores and
what a single row represents.
Rules:
- do not enumerate columns, do not write SQL, no markdown, no preamble;
- use only what the table name, keys and samples imply; do not invent domains;
- if the row grain is unclear, describe it in generic terms.
Return only the sentence.
```

## 4. 连通图

- **节点**：该库所有表。
- **边**：外键（child → parent），带 `pairs: [[from_col, to_col], ...]` 与 `on_delete/on_update`；
  `edge_source` 固定为 `foreign_key`，标明"只用了声明的外键"，没有任何启发式推断。
- **无向视图**：`adjacency`、`degree`、`components`（并查集，按大小降序）、`isolated`。

```json
{"nodes": ["frpm", "satscores", "schools"],
 "edge_source": "foreign_key",
 "edges": [{"from": "frpm", "to": "schools", "pairs": [["CDSCode", "CDSCode"]]},
           {"from": "satscores", "to": "schools", "pairs": [["cds", "CDSCode"]]}],
 "adjacency": {"frpm": ["schools"], "satscores": ["schools"], "schools": ["frpm", "satscores"]},
 "degree": {"frpm": 1, "satscores": 1, "schools": 2},
 "components": [["frpm", "satscores", "schools"]],
 "isolated": []}
```

## 5. 全量 schema（与摘要同时产出）

摘要是压缩视图，另有一份**列级全量 schema**，格式参照 `scripts/build_schema.py`
（`jev/schema/bird_json.py`），由同一次遍历同时产出：

```json
{"dataset": "BIRD", "split": "dev", "include_adjacent_tables": true,
 "stats": {"databases": 11, "tables": 75, "columns": 798},
 "databases": [
   {"db_id": "california_schools",
    "tables": [
      {"name": "frpm",
       "columns": [{"name": "CDSCode", "type": "text",
                    "sample": "01100170109835", "date_format": null}, ...],
       "primary_key": ["CDSCode"],
       "adjacent_tables": [{"table": "schools",
                            "foreign_keys": [["frpm.CDSCode", "schools.CDSCode"]]}]}]}]}
```

与参考实现的差异（都是为了同时覆盖 BIRD 与 Spider2）：

| 项 | 参考 `build_schema.py` | 这里 |
|---|---|---|
| 表/列清单 | BIRD `tables.json` | PRAGMA（`table_info`），Spider2 也有 |
| 类型 | BIRD `column_types` | 有 `tables_json` 时用 BIRD 的类型（与参考一致），否则由声明类型归一化（integer/real/date/text） |
| 样例 | 每列 `WHERE col IS NOT NULL LIMIT 1` | 每表一条 `SELECT * LIMIT 200` 取首个非空；窗口内仍为空的行再走一次相同的定向查询 |
| `date_format` | 对 `type == date` 的列按 `strptime` 推断 | 同 |
| `adjacent_tables` | 对称邻接，`table.column` 对 | 同（按表名排序） |
| 视图 | 不含 | 不含 |

**与仓库已有产物的等价性校验**（`outputs/schema/minidev_schema.json`，同一批 11 库）：

| 检查 | 结果 |
|---|---|
| 列集合 | 798 / 798 完全一致（无缺、无多） |
| `type` | 0 处不同 |
| `date_format` | 0 处不同 |
| `primary_key` | 0 处不同 |
| `sample` | 34 / 798（4.3%）不同：约 2/3 是本模块对超长值做了 120 字符截断（参考实现保留整段 XML/HTML），其余是 SQLite 走索引时的扫描顺序差异 |
| `adjacent_tables` | 8 张表不同：BIRD `tables.json` 与实际 SQLite 声明不一致（如 `european_football_2` 的 DB 多声明了 Country/League 外键），另有部分只是数组顺序不同 |

## 6. 产物总览（实测）

摘要（`outputs/db_summary/`）：

| 产物 | 库 | 表 | 外键边 | 孤立表 | 无边的库 | 摘要均长 |
|---|---:|---:|---:|---:|---:|---:|
| `bird_dev.json` | 11 | 75 | 104 | 3 | 0 | 69.2 字符 |
| `bird_minidev.json` | 11 | 75 | 104 | 3 | 0 | 69.1 字符 |
| `bird_train.json` | 69 | 522 | 552 | 26 | 3 | 75.2 字符 |
| `spider2_lite_local.json` | 30 | 412 | 171 | 282 | 20 | 66.4 字符 |

全量 schema（`outputs/schema/`，同样的库范围）：

| 产物 | 库 | 表 | 列 | 体积 |
|---|---:|---:|---:|---:|
| `bird_dev.json` / `bird_minidev.json` | 11 | 75 | 798 | 0.15 MB |
| `bird_train.json` | 69 | 522 | 3,539 | 0.75 MB |
| `spider2_lite_local.json` | 30 | 412 | 2,891 | 0.49 MB |

摘要（LLM 生成）被压到平均 **~93 字符/表**：dev 全部 75 张表合计 6,780 字符（≈1.9k tokens），
远小于原始 schema 的规模。这就是"压缩 token"的量化含义。

LLM 调用账（`temperature=0`、`deepseek-flash`、并发 12）：

| split | 表 | 真实调用 | 缓存命中 | 回退 | 摘要均长 | 建库耗时 |
|---|---:|---:|---:|---:|---:|---:|
| bird_dev | 75 | 75 | 0 | 0 | 90.4 | ~2 min |
| bird_train | 522 | 522 | 0 | 0 | 94.3 | ~3 min |
| bird_minidev | 75 | **0** | 75 | 0 | 90.4 | <1 s |
| spider2_lite_local | 412 | 412 | 0 | 0 | 92.5 | ~2 min |
| 合计 | 1,084 | **1,009** | 75 | 0 | 93.1 | ~7 min |

## 7. 复现

```bat
cd D:\JEV\exp_db_summary
scripts\run_all.cmd C:\ProgramData\miniconda3\python.exe
```

> 本机执行策略为 `AllSigned`，未签名的 `.ps1` 无法直接运行（`run_all.ps1` 会报
> "is not digitally signed"）。因此提供 `scripts\run_all.cmd`：批处理不受执行策略约束，内部以
> `-ExecutionPolicy Bypass -File` 调 `run_all.ps1`，**不修改系统/用户策略**。等价写法：
> `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_all.ps1 -Python <python>`。

```powershell
# 单个库 / 覆盖摘要方式
python -u scripts/build_db_summary.py --config configs\bird_dev.yaml --db california_schools
python -u scripts/build_db_summary.py --config configs\bird_dev.yaml --summarizer llm --db california_schools --limit-tables 3
```

配置字段：

| 字段 | 含义 |
|---|---|
| `dataset` / `split` | 写进产物的标识 |
| `db_backend` | `sqlite_dir`（BIRD）或 `sqlite_zip`（Spider2 local） |
| `databases_dir` + `db_pattern` | `sqlite_dir`：库文件根目录与通配 |
| `db_zip` | `sqlite_zip`：压缩包路径 |
| `cache_dir` | 解压缓存与 LLM 摘要缓存的位置 |
| `output_path` | 摘要 JSON 路径（相对实验目录） |
| `schema_output_path` | 全量 schema JSON 路径（相对实验目录） |
| `tables_json` | 可选的 BIRD `*_tables.json`：提供时列类型以它为准 |
| `schema_sample_rows` | 每表取样例的行窗口（默认 200） |
| `summarizer` | `template` / `llm` |
| `summary_max_chars` | 单表摘要硬上限（默认 160） |
| `summary_max_words` | LLM 摘要词数上限（默认 18） |
| `llm_max_tokens` | 单次调用的 completion 预算（默认 600，含推理 token） |
| `llm_concurrency` | 每库并发调用数（默认 6，本次用 12） |
| `sample_rows` / `sample_values` | 采样行数 / 卡片保留的取值个数 |
| `llm_max_tables` | 未缓存 LLM 调用的单次上限（0 = 不限） |

## 8. 已知限制（都是实测结论，不是猜测）

1. **Spider2.0 本地库大多没有声明外键**：30 个库里有 **20 个** 拉不出任何外键（412 张表里 282 张
   孤立）。这些库是从 CSV/Postgres dump 生成的，约束没被带上。所以 `PRAGMA` 能给 BIRD 提供完整的
   连接图，但对 Spider2 local 只能给出"哪些表相邻"的空图。**本阶段不引入列名/类型推断来补边**——
   那属于后续的 schema linking，且会改变这张图的语义（`edge_source` 现在是 `foreign_key`，
   一目了然）。
2. **行数有上限**（20 万），大表显示为 `rows_capped: true`，精确行数不进入卡片。
3. **只看表，不含视图**：`sqlite_master` 里 `type='view'` 的对象被跳过。
4. **摘要由模型生成，不是确定性的**：可复现性靠 `outputs/cache/*/llm_summaries.json`
   （1,084 条全在缓存里）；换模型或改 prompt 时记得同步提升 `_PROMPT_VERSION`，否则会命中旧缓存。
   另外 1,084 条摘要有 **78 条字符串重复**（多为结构相同的小表，如分区表/字典表），内容无误但
   区分度低。
5. **LLM 的成本与延迟**：1,009 次调用、约 7 分钟（并发 12）。prompt 里含全部列名与 2 行样例，
   输入约 700 tokens/次；若后续做更大规模的库，建议先按表数预算。
5. **样例取值是"随机两条"**（`LIMIT 2`，无 `ORDER BY`），不保证有代表性；它的作用只是给摘要
   与后续链接阶段一个领域提示。

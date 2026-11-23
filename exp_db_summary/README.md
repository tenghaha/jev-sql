# exp_db_summary — 离线数据库摘要（BIRD / Spider2.0 本地 SQLite）

理解阶段的第一步，一次遍历产出两份 JSON：

1. **压缩视图**：每库的**表级卡片 + 外键连通图**，让下游模型大致知道"有哪些表、每张表是什么、
   表之间怎么连"，**不提供整表的列清单**（省 token）。每张表的一句话摘要由 **LLM 生成**
   （`deepseek-flash`，1,084 表全部 `summary_source: "llm"`，0 回退），平均 ~93 字符/表。
2. **全量 schema**：每个库的**列级** schema（列名/类型/样例/date_format/主键/邻接表），格式与
   `scripts/build_schema.py` 一致，供后需要精确列信息的阶段直接使用。

本阶段不组装 prompt、不做端到端推理。

## 产物

压缩视图（`outputs/db_summary/`）：

| 文件 | 内容 | 规模 |
|---|---|---|
| `bird_dev.json` | 11 库 / 75 表 / 104 条外键边 | 0.09 MB |
| `bird_train.json` | 69 库 / 522 表 / 552 条边 | 0.55 MB |
| `bird_minidev.json` | 11 库 / 75 表 / 104 条边 | 0.09 MB |
| `spider2_lite_local.json` | 30 库 / 412 表 / 171 条边 | 0.31 MB |

摘要（LLM）平均 **~93 字符/表**（dev 全库合计 6,780 字符 ≈ 1.9k tokens）。

LLM 账：1,084 张表，**1,009 次真实调用**（MINIDEV 的 75 表直接复用 dev 缓存），0 次回退，
并发 12 时约 7 分钟；重跑全量（命中缓存）**6 秒、0 次调用**。缓存键是
`dataset|db|table|model|prompt_version`，在 `outputs/cache/*/llm_summaries.json`。

全量 schema（`outputs/schema/`，同样的库范围）：

| 文件 | 库 | 表 | 列 | 体积 |
|---|---:|---:|---:|---:|
| `bird_dev.json` / `bird_minidev.json` | 11 | 75 | 798 | 0.15 MB |
| `bird_train.json` | 69 | 522 | 3,539 | 0.75 MB |
| `spider2_lite_local.json` | 30 | 412 | 2,891 | 0.49 MB |

与仓库已有的 `outputs/schema/minidev_schema.json` 对比：798 列的集合、`type`、`date_format`、
`primary_key` **完全一致**；`sample` 有 34 列（4.3%）不同（超长值截断到 120 字符 + 索引扫描顺序），
`adjacent_tables` 有 8 张表不同（BIRD `tables.json` 与 SQLite 实际声明不一致）。

## 一条命令

```bat
cd D:\JEV\exp_db_summary
scripts\run_all.cmd C:\ProgramData\miniconda3\python.exe
```

> 本机执行策略拒绝未签名的 `.ps1`（`AllSigned`），所以入口是 `.cmd` 包装器（批处理不受该策略约束），
> 它内部以 `-ExecutionPolicy Bypass` 调用 `run_all.ps1`，不修改任何机器设置。
> 想直接用 PowerShell 就跑
> `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_all.ps1 -Python <python>`。

单库 / 换摘要方式：

```powershell
python -u scripts\build_db_summary.py --config configs\bird_dev.yaml --db california_schools
python -u scripts\build_db_summary.py --config configs\bird_dev.yaml --summarizer llm --db california_schools --limit-tables 3
```

## 一个卡片

```json
{"name": "atom", "rows": 12333, "rows_capped": false, "n_columns": 3,
 "pk": ["atom_id"],
 "fk_out": [{"to_table": "molecule", "pairs": [["molecule_id", "molecule_id"]]}],
 "neighbours": ["molecule"],
 "sample": ["TR000_1", "TR000", "cl"],
 "summary": "atom: 12,333 rows, 3 cols; joins molecule; e.g. TR000_1, TR000, cl",
 "summary_chars": 66, "summary_source": "template"}
```

连通图（同一 JSON 的 `graph` 字段）：

一份 schema 条目：

```json
{"db_id": "california_schools", "tables": [{"name": "frpm",
  "columns": [{"name": "CDSCode", "type": "text", "sample": "01100170109835", "date_format": null}],
  "primary_key": ["CDSCode"],
  "adjacent_tables": [{"table": "schools", "foreign_keys": [["frpm.CDSCode", "schools.CDSCode"]]}]}]}
```

连通图（同一 JSON 的 `graph` 字段）：

```json
{"nodes": ["frpm","satscores","schools"], "edge_source": "foreign_key",
 "edges": [{"from":"frpm","to":"schools","pairs":[["CDSCode","CDSCode"]]}, ...],
 "adjacency": {...}, "degree": {...}, "components": [["frpm","satscores","schools"]],
 "isolated": []}
```

## 目录

```
configs/        一个 split 一个实验（BIRD dev/train/MINIDEV、Spider2 lite-local）
dbprofile/      sqlite_meta（PRAGMA）· graph（连通图）· schema（全量列级）· summarize（template/llm）· build · llm
scripts/        build_db_summary.py（主入口）· run_all.ps1（全量重建）
docs/           01-db-summary.md（设计、字段、实测、限制）
outputs/        db_summary/*.json（压缩视图）· schema/*.json（全量 schema）· cache/（解压的 sqlite + LLM 缓存）
```

## 设计要点

- 元数据全部来自 **PRAGMA**（`table_info` / `foreign_key_list`），行数用**有上限的计数**
  （`rows_capped`），样例 `LIMIT 2`；库以只读方式打开，不改数据集。外键若未指定目标列（SQLite
  允许 `REFERENCES parent`），解析到父表主键，产物里不会出现 `table.None`。
- 全量 schema 的列类型：有 `tables_json`（BIRD）时以 BIRD 的类型为准，否则由 SQLite 声明类型归一化；
  列样例每表一条 `SELECT * LIMIT 200`，窗口内为空再定向取首个非空值。
- 摘要两种实现：`llm`（当前产物，`.env` 的 OpenAI 兼容接口 + 缓存 + 并发 + 预算闸门 + 失败回退）
  与 `template`（确定性回退）。两者产物 schema 相同，只有 `summary_source` 不同。
  模型是 reasoning 模型，`max_tokens` 必须留出推理空间（120 时回答为空，现为 600 且空回答自动 2× 重试）。
- 连通图只来自**声明的外键**（`edge_source: "foreign_key"`），不做任何启发式补边。

## 已知限制（详见 doc）

- **Spider2.0 本地库 20/30 没有声明外键**，图上没有边（412 表里 282 孤立）；这些库的 join 路径
  需要后续 schema linking 阶段处理，不在本阶段。
- 行数上限 20 万（大表标记 `rows_capped`）；不含视图。
- 摘要由模型生成、非确定性：可复现性靠 `outputs/cache/*/llm_summaries.json`；改 prompt 需同步提升
  `_PROMPT_VERSION`，否则会命中旧缓存。1,084 条摘要有 78 条字符串重复（结构相同的小表），区分度偏低。

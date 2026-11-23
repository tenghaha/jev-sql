# exp_ast_slot — gold SQL → AST → 角色模板（与 schema、取值解耦）

把 **BIRD（5 个 split）与 Spider2（2 个 split）共 12,838 条 gold SQL** 解析为 AST，再把每个
具体列替换成一个**角色槽位**，得到脱离数据库 schema 的查询模板 + 一份 silver 标注。

| 阶段 | 结果 |
|---|---|
| 解析（CFG 递归下降 + Pratt 优先级） | **12,838 / 12,838 = 100.00%**，7 个 split，5 个方言 |
| 保真度（字面量守恒 + 结构不变式） | **0 problems** |
| 角色槽位 | **6 个角色**（`TABLE` `JOINKEY` `MEASURE` `DIMENSION` `FILTER` `SORT`）× **4 种 kind**（table/column/value/field），12,838 条全部模板化 |
| 角色往返 | `template + slots` 能逐节点重建原 AST：**12,838 / 12,838 完全一致** |
| 模板解耦 | 表名/列名/取值全槽化；把 gold 里所有名字与取值改名后模板逐字节不变：**12,838 / 12,838 通过** |

## 交付物

1. **各 split 的 AST 集**：`outputs/ast/ast_<split>.json`（+ `.jsonl`、`rules.json`）
2. **角色设计**：`outputs/roles/role_schema.json` + [docs/03-roles.md](docs/03-roles.md)
3. **角色模板 + 槽位标注**：`outputs/roles/role_ast_<split>.json`（`template` + `slots`）
4. **方法与流程**：[docs/](docs/) 四篇 + 报告 `outputs/reports/{parse_report,role_report}.md`

## 快速开始

```bat
cd D:\JEV\exp_ast_slot
scripts\run_all.cmd C:\ProgramData\miniconda3\python.exe                # 全量重建（parse/roles/3 项校验）
```

或逐步执行：

```powershell
cd D:\JEV\exp_ast_slot
python -m sqlast.cli parse --config configs/datasets.yaml                 # AST 集，约 12 s
python -m sqlast.cli roles --config configs/datasets.yaml --show-example  # 角色模板，约 16 s
python tools\verify_ast.py                                                # AST 保真度，0 problems
python tools\verify_roles.py                                              # 角色往返，0 problems
```

单条查看（模板 + 槽表一起打印）：

```powershell
python -m sqlast.cli show --sql "SELECT COUNT(DISTINCT x) FROM t WHERE a = 1" --dialect sqlite --roles
```

## 核心结构：枝干保留，表/列/取值换成槽

```
原 AST                                       模板
Query                                        Query  cardinality=TOP_K
└─ Select                                    └─ Select
   ├─ items[0] BinaryOp("/")                    ├─ items[0] BinaryOp("/")
   │   ├─ Column `Free Meal Count (K-12)` ──►   │   ├─ Slot{MEASURE, column, 1}
   │   └─ Column `Enrollment (K-12)`      ──►   │   └─ Slot{MEASURE, column, 2}
   ├─ from [Table frpm]                   ──►   ├─ from [Table name=Slot{TABLE, table, 3}]
   ├─ where BinaryOp("=")                       ├─ where BinaryOp("=")
   │   ├─ Column `County Name`            ──►   │   ├─ Slot{FILTER, column, 4}
   │   └─ Literal 'Alameda'               ──►   │   └─ Slot{FILTER, value, 5}
   └─ order_by / limit ...                      └─ order_by / limit ...
```

| role | 来源子句 | kind |
|---|---|---|
| `TABLE` | `FROM` / `JOIN` 的每个表引用 | table |
| `JOINKEY` | `JOIN ... ON` / `USING`；`IN`/`=`/`EXISTS` 子查询投影 | column |
| `MEASURE` | `SELECT` 列表的每个列/值；聚合函数内部的列 | column / value |
| `DIMENSION` | `GROUP BY` 键（及窗口 `PARTITION BY`、`DISTINCT ON`） | column / value |
| `FILTER` | `WHERE`/`HAVING`/`QUALIFY` 谓词；`LIMIT`/`OFFSET` 计数 | column / value |
| `SORT` | `ORDER BY` 键（含窗口 `ORDER BY`） | column / value |

槽节点只带 `{role, kind, sid}`；gold 里填了什么在侧表
（`column/table/value` + `path` + `binding/data_type/key_role/agg`）——模板与库无关，标注才是 silver label。

每个 `Query` 节点另带一个**意图**字段 `cardinality ∈ {SINGLE, TOP_K, GROUPED, LIST, SET}`
（`SINGLE` 一行 / `TOP_K` 有 LIMIT / `GROUPED` 分组 / `LIST` 列表 / `SET` 去重行），
两层产物（`ast_*` 与 `role_ast_*`）都有；由 `sqlast/intent.py` 后处理赋值。

dev 分布：`LIST` 35.7% · `SINGLE` 34.0% · `TOP_K` 17.5% · `SET` 10.8% · `GROUPED` 2.1%
（Spider2 明显更"列表/取前 k/分组"：lite 里 `LIST` 40.2%、`TOP_K` 33.6%、`GROUPED` 14.8%，
`SINGLE` 只有 9.0%）。

## 实测要点

| 读数 | 值 |
|---|---|
| 槽位数 / 每条查询 | dev 14,796（9.65）；train 89,829（9.53）；Spider2 45.9–56.9 |
| 槽位构成（dev） | column 8,121 · value 3,598 · table 3,077 |
| 列槽角色（dev） | `JOINKEY` 37% · `MEASURE` 31% · `FILTER` 27% · `SORT` 3.4% · `DIMENSION` 1.7% |
| 取值槽位置（dev） | 过滤 73.7% · 投影 21.9% · join 3.0% · 排序 1.1% |
| 跨库模板迁移（BIRD train → dev，库完全不重叠） | 名字保留 **0.0%** → 列→角色 **0.1%** → 表/列/取值全槽化 **31.8%** |
| `JOINKEY` 落在 PK/FK | dev 94.2%，train 91.9% |
| `SUM/AVG` 绑到 schema 的 text 列 | dev 4.8%（BIRD 已知数据性质：数值存在 text 列里） |

## 文档

| 文档 | 内容 |
|---|---|
| [docs/01-grammar.md](docs/01-grammar.md) | CFG 规范（[grammar.ebnf](docs/grammar.ebnf)）、产生式↔规则 id 对照、方言差异表、为 gold 保留的宽松规则 |
| [docs/02-parser.md](docs/02-parser.md) | 递归下降 + Pratt 绑定力表、12 轮失败簇驱动迭代、覆盖率与保真度双指标 |
| [docs/03-roles.md](docs/03-roles.md) | 6 角色 × 4 kind、槽位字段、完整例子、去重与跨库迁移、**解耦审计**（残留扫描 + 重命名不变性） |
| [docs/04-pipeline.md](docs/04-pipeline.md) | 配置驱动流程、加数据集/改角色映射/扩文法的固定循环、产物规范、环境 |

## 目录

```
configs/datasets.yaml     split 定义（加数据集只改这里）
sqlast/                   lexer · parser · ast_json · dataset · schema · roles · cli
tools/                    diagnose_parse.py（失败簇）· verify_ast.py · verify_roles.py · verify_template.py
docs/                     文法 · 解析器 · 角色 · 流程
outputs/ast/              AST 集（70 MB）
outputs/roles/            角色模板 + 槽位标注（128 MB）
outputs/reports/          覆盖率报告 · 角色报告 · 汇总 JSON
```

## 覆盖情况

| split | 方言 | 条数 | 解析 | 结构特征（该 split 的触发次数） |
|---|---|---:|---:|---|
| bird_dev | sqlite | 1534 | 100% | JOIN 1427、子查询 71、CTE 13、窗口 5、集合运算 3 |
| bird_train | sqlite | 9428 | 100% | JOIN 9542、`USING` 18、集合运算 24、子查询 488 |
| bird_minidev_sqlite | sqlite | 500 | 100% | 与 dev 同构 |
| bird_minidev_mysql | mysql | 500 | 100% | 反引号 + `LIMIT a,b` + `NOTNULL` |
| bird_minidev_postgresql | postgres | 500 | 100% | `::type` 19、`EXTRACT` 40、`NULLS LAST` |
| spider2_lite | bigquery / snowflake / sqlite | 256 | 100% | CTE 619、`UNNEST` 65、逗号连接 125、窗口 130、`col:field` 118 |
| spider2_snow | snowflake | 120 | 100% | `::` 转换 176、CTE 403、集合运算 94、`col:field` 144 |

## 与上一轮 IR 原型的区别

上一轮（`outputs/ir_roundtrip/`）是"反编译器 + lowering + 执行比对"，BIRD dev 98.9%，
未建模 CTE / 窗口 / 集合运算。本轮是**新的前端 + 新的角色层**：(a) 覆盖 5 个方言、7 个 split，
全部 100%；(b) 不为 lowering 牺牲语法面（CTE/窗口/集合运算/`UNNEST`/`LATERAL` 全实现）；
(c) 保真度用"字面量守恒 + 结构不变式"验证，不需要数据库；(d) 语义信息只保留 5 个角色，
其余（类型、粒度、去重）留给槽位侧表与执行校验。

## 已知限制

- Spider2 不提供 `tables.json`，因此 `data_type` / `key_role` 在两个 spider2 split 上缺席。
- `LIMIT k` 与 `ORDER BY 1` 的序数都是取值槽，所以模板不记录"前 3 条"里的 3（`cardinality` 仍标 `TOP_K`）。
- 表名、列名、取值全部槽化；别名 / CTE 名 / 窗口名保留（它们不是 schema）。
- `MEASURE` 不分是否聚合（靠侧表 `agg` 区分）；`DIMENSION` 只表示 `GROUP BY` 粒度，因此占比很小。
- `SORT` 只统计真实列：`ORDER BY <投影别名>` 保留为 `AliasRef`，不占槽。

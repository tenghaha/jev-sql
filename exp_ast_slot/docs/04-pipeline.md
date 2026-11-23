# 04 · 可复用流程

本目录是一套"**gold SQL → AST → 角色模板（schema-free）+ 槽位标注**"的可复用流水线：
配置驱动、无第三方解析库依赖、每一步都有独立可运行的校验。

## 1. 目录

```
exp_ast_slot/
├─ configs/datasets.yaml      # split 定义（路径、方言、schema），加数据集只改这里
├─ sqlast/
│  ├─ lexer.py                # 方言相关词法（引号/转义/注释/参数/数字）
│  ├─ parser.py               # 递归下降 + Pratt；规则 id 计数
│  ├─ ast_json.py             # AST 序列化（丢 None，稳定 schema）
│  ├─ intent.py               # Query.cardinality（SINGLE/TOP_K/GROUPED/LIST/SET）
│  ├─ dataset.py              # BIRD json / Spider2 目录 / 逐行 SQL 三种加载器
│  ├─ schema.py               # BIRD tables.json → 类型 / PK / FK
│  ├─ roles.py                # 列→角色槽位、模板签名、迁移/去重统计
│  └─ cli.py                  # parse / roles / show 三个子命令
├─ tools/
│  ├─ diagnose_parse.py       # 失败簇聚类（扩文法用）
│  ├─ verify_ast.py           # AST 保真度（字面量守恒 + 结构不变式）
│  ├─ verify_roles.py         # 角色往返：template + slots 重建 AST
│  └─ verify_template.py      # 模板解耦：残留扫描 + 重命名不变性
├─ docs/                      # 01 文法 · 02 解析器 · 03 slot · 04 本文件
└─ outputs/
   ├─ ast/     ast_<split>.jsonl + .json + rules.json
   ├─ roles/   role_ast_<split>.jsonl + .json + role_schema.json
   └─ reports/ parse_report.md · role_report.md · *_summary.json · role_stats.json · role_transfer.json
```

## 2. 三条命令

```powershell
cd D:\JEV\exp_ast_slot

# 1) 解析全部 split → AST 集（约 12 s）
python -m sqlast.cli parse --config configs/datasets.yaml

# 2) 列→角色槽位 → schema-free 模板 + 侧表（约 16 s）
python -m sqlast.cli roles --config configs/datasets.yaml --show-example
python -m sqlast.cli roles --config configs/datasets.yaml --anon-tables   # 表名也洞化

# 3) 三级校验（都应输出 0 problems）
python tools\verify_ast.py       # AST 保真度
python tools\verify_roles.py     # 角色往返：template + slots 重建 AST
python tools\verify_template.py  # 模板解耦：残留扫描 + 重命名不变性
```

单条调试：

```powershell
python -m sqlast.cli show --sql "SELECT COUNT(DISTINCT x) FROM t WHERE a = 1" --dialect sqlite
python -m sqlast.cli show --sql "..." --dialect snowflake --roles
python -m sqlast.cli parse --config configs/datasets.yaml --split bird_dev --spans   # 保留字符偏移
```

`--split` 可以给多个名字；省略即全部。`--spans` 会给每个节点加 `"span": [start, end)`
（用于把 slot 映射回原文，产物体积约 +25%）。

## 3. 加一个新的数据集（3 步，不改代码）

1. 在 `configs/datasets.yaml` 的 `splits` 下加一项：

```yaml
  - name: bird_minidev_duckdb
    kind: bird_json            # bird_json | spider2_dir | sql_lines
    path: BIRD/MINIDEV/mini_dev_duckdb.json
    tables: BIRD/MINIDEV/dev_tables.json   # 可选：有 schema 才能填 schema 类 slot
    dialect: sqlite            # sqlite | mysql | postgres | bigquery | snowflake
```

2. 跑 `parse`；若有失败，跑 `python tools\diagnose_parse.py <name>` 看失败簇。
3. 跑 `roles`。没有 schema 时 `data_type` / `key_role` 缺席（如实缺省，不是猜）。

`spider2_dir` 支持混合方言：用 `dialect_by_prefix` 按 instance-id 前缀分派
（`bq*`→bigquery、`sf*`→snowflake、`local*`→sqlite，最长前缀优先）。

## 4. 扩文法的循环（这是本实验真正"可复用"的部分）

```
全量跑 → 失败簇聚类 → 每簇一条规则 → 回归（覆盖率 + 保真度）→ 重复
```

- **失败簇**：`tools/diagnose_parse.py` 按 `(错误消息, 出错 token)` 聚合，并打印出错位置上下文。
  一个簇通常对应一个缺失产生式或一条过宽的规则。
- **加规则**：在 `parser.py` 对应方法里加分支，并 `self.hit("<rule.id>")` 计数——**每条规则都
  必须能被统计**，否则"放宽了多少"无法回答。
- **回归断言两条**：`parse` 覆盖率不回退 + `verify_ast` 仍为 0 problems。
- **纪律**：新规则在 **train** 上开发（69 库 / 9428 条），**dev 只做验收**（11 库 / 1534 条）。
  dev 与 train 的库完全不重叠，所以两侧覆盖率接近才能说明"学的是 SQL 语法，不是这几个库的
  写法"。

本次的真实迭代记录（12 轮、每轮一个失败簇）见 [02-parser.md](02-parser.md) §4。其中第 10 轮
（`::DATE < x` 被当成 `ARRAY<...>` 的泛型括号）是**覆盖率无法发现**的错误，只有保真度检查
才能抓到——这也是为什么 §4 的回归断言必须是两条而不是一条。

## 5. 改角色映射（唯一需要动的地方是 `_CLAUSE_ROLE`）

角色只有 5 个，映射是一张表：

```python
_CLAUSE_ROLE = {"join": "JOINKEY", "where": "FILTER", "having": "FILTER",
                "qualify": "FILTER", "group": "DIMENSION", "select": "MEASURE",
                "order": "SORT", "window_partition": "DIMENSION",
                "window_order": "SORT"}
```

要加一个角色（例如把"聚合列"与"输出字段"从 `MEASURE` 里拆开），三步：
① 在 `roles.py::ROLES` 里加名字、在 `ROLE_SPEC` 里写清 `clause` / `meaning` / `check`；
② 在 `_slot()` 的角色判定里给出条件；③ 报告与 `role_schema.json` 自动跟着变。
**不要**引入与"位置"无关的字段（类型、粒度、去重……）：那会回到 49 字段版本的老路。

## 6. 产物规范

| 路径 | 内容 | 体积 |
|---|---|---:|
| `outputs/ast/ast_<split>.jsonl` | 每条 gold SQL 一行：`id/db_id/dialect/sql/question/evidence/status/ast`（AST 的每个 `Query` 带 `cardinality`；失败的记录带 `error`） | 合计 70 MB |
| `outputs/ast/ast_<split>.json` | 同一批记录的 JSON 数组（"各 split 的 AST 集"） | 最大 `bird_train` 28 MB |
| `outputs/ast/rules.json` | 每个 split 的规则触发次数（文法覆盖的量化证据） | <1 MB |
| `outputs/roles/role_ast_<split>.jsonl/.json` | `template`（表/列/取值→槽）+ `slots` 侧表 + 三个签名层级 | 合计 128 MB |
| `outputs/roles/role_schema.json` | 角色定义与 `clause → role` 映射 | <1 MB |
| `outputs/reports/parse_report.md` | 覆盖率 + 规则表 + 未解析簇 | — |
| `outputs/reports/role_report.md` | 角色分布、模板去重、跨 split 迁移、校验 | — |

所有路径都由 `configs/datasets.yaml` 的 `out_dir` 决定，脚本里没有硬编码输出目录。

## 7. 环境

本机没有 `znws` conda 环境（`conda` 不在 PATH），也没有 `sqlglot` / `sqlparse`，因此全部实现
只依赖标准库 + `PyYAML`（读配置），用 base python 3.12 运行：

```powershell
& C:\ProgramData\miniconda3\python.exe -m sqlast.cli parse --config configs/datasets.yaml
```

若在配置好的 `znws` 环境里跑，命令退化为 `conda run -n znws python -m sqlast.cli ...`，无需改代码
（没有任何第三方解析依赖）。12,838 条 gold SQL 的实测耗时：`parse` 12 s、`roles` 20 s、
`verify_ast` 3 s、`verify_roles` 10 s、`verify_template` 25 s。

## 8. 与论文/后续实验的接口

- **AST 集**是"可反编译、可验证"的底座：任意预测出的 SQL 都可以走同一条解析路径落到同一个
  节点集合上，因此可以做结构比对（形状签名）、错误定位（哪一层错）。
- **角色模板**是可迁移的那一半：枝干 + `Slot{role, sid}` + 字面量 + 别名（可选 `<T>` 表洞）。
  跨库实验用 `--anon-tables`，BIRD train → dev 的模板命中率从 0.2% 升到 30.1%（实测）。
- **侧表**是 silver label：`sid → column/table/data_type/key_role` 全部由 gold SQL(+schema)
  确定性推出，可以直接算 slot 级 P/R（角色级准确率、每角色每候选集的命中率）。
- **一个槽 = 一道选择题**：`role` 决定候选集（`MEASURE`→数值列、`JOINKEY`→键列……），
  `data_type`/`key_role` 决定校验规则，模板的 `sid` 决定写回位置。

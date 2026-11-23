# 02 · 解析器：递归下降 + Pratt 优先级

代码：`sqlast/lexer.py`（词法）、`sqlast/parser.py`（递归下降 + Pratt）。
产物：`outputs/ast/ast_<split>.{json,jsonl}`、`outputs/ast/rules.json`、
`outputs/reports/parse_report.md`。

## 1. 三层结构

```
SQL 文本
  └─ lexer.tokenize()            方言相关的词法：引号、转义、注释、参数、数字
       → Tok(kind, text, start, end, value, quote)   每 token 带 [start,end) 偏移
  └─ Parser.parse_statement()    递归下降：一个产生式一个方法（stmt/clause/FROM/…）
       └─ Parser.parse_expr(bp)  Pratt：运算符优先级与结合性
  └─ AST（dict，每个节点带 "type"） → ast_json.prune() → JSONL/JSON
```

AST 是**纯语法**的：它记录"这条 SQL 说了什么"，不做语义推断。唯一的例外是每个 `Query` 节点上的
`cardinality`（答案形状：`SINGLE/TOP_K/GROUPED/LIST/SET`），它由 `sqlast/intent.py` 在解析后
作为一个确定性后处理赋值，不在 `parser.py` 里。`Func` 节点上的
`is_aggregate` 是唯一的例外标注——因为"这个名字是不是聚合函数"是词法事实（`COUNT`/
`SUM` 是关键字级函数名），而不是语义猜测。

节点集合共 **49 种**，在 12,838 条 gold SQL 上共 **298,074 个节点**，平均每条 23.2 个节点
（最大 506）。最高频的节点：`Column` 79,441、`BinaryOp` 37,766、`Literal` 33,175、
`Table` 28,346、`SelectItem` 22,885、`Select` 15,646、`Query` 15,453、`Join` 13,381、
`Func` 13,312。

## 2. Pratt 解析：优先级是数据，不是控制流

`parse_expr(min_bp)`：

```python
left = parse_prefix()
while True:
    bp = peek_infix_bp()          # (lbp, rbp) 或 None
    if bp is None or bp[0] < min_bp: break
    left = parse_infix(left, *bp) # 右操作数用 parse_expr(rbp) 递归
```

左结合编码为 `rbp = lbp + 1`，右结合或"特殊形式"单独处理。完整绑定力表
（`parser.BINDING_POWER`）：

| 优先级 | 运算符 | lbp / rbp | 说明 |
|---:|---|---|---|
| 1 | `OR` | 1 / 2 | 最低 |
| 3 | `AND` | 3 / 4 | |
| 5 | `NOT`（前缀） | — | **比比较运算符松**：`NOT a = b` → `NOT (a = b)` |
| 6 | `= == != <> < <= > >= <=>` | 6 / 7 | 比较族共享一层，因此 `a > b` 与 `a IS NULL` 同级 |
| 6 | `IS` / `IN` / `BETWEEN` / `LIKE` / `ILIKE` / `REGEXP` / `GLOB` | 6 / 7 | 特殊形式在 `parse_infix` 内分支 |
| 8 | `\|`（位或） | 8 / 9 | 位运算排在比较之后，避免 `a & b = c` 被误切 |
| 9 | `&` / `^` | 9 / 10 | |
| 10 | `\|\|`（字符串拼接） | 10 / 11 | |
| 12 | `+` `-` | 12 / 13 | |
| 14 | `*` `/` `%` `DIV` `MOD` | 14 / 15 | |
| 15 | 一元 `-` `+` `~` | — | 比 `*` 紧：`-a * b` → `(-a) * b` |
| 16 | `::`（后缀转换） | — | `-a::int` → `-(a::int)` |
| 17 | `[...]` / `.field` / `:path` / `NOTNULL` / `ISNULL` / `IGNORE NULLS` | — | 后缀最高 |

四个需要特殊处理的固定形式（不能当普通二元）：

- `BETWEEN lo AND hi`：`lo`/`hi` 都用 `parse_expr(7)` 解析，避免把 `AND` 吃掉；
- `[NOT] IN (...) / BETWEEN / LIKE ... ESCAPE`：`NOT` 在**中缀位置**才被识别为取反；
- `IS [NOT] {NULL|TRUE|FALSE|UNKNOWN|DISTINCT FROM}`：`IS` 不是普通比较，右侧是受限文法制；
- `a = ANY(...)` / `ALL(...)`：`ANY`/`ALL` 走函数调用路径（`ALL(x)` 在 `x` 是子查询时形态不同）。

## 3. 一个必须写下来的坑：`<` 既是比较符也是类型括号

`x::DATE < y` 与 `ARRAY<INT64>` 都以 `<` 开头。第一版把 `<` 一律当作参数化类型的开始，
于是 `citing_app."date"::DATE > core_app."date"::DATE AND ... THEN` 之后的整条查询被吞掉、
报 `expected THEN at EOF`——而**覆盖率指标仍然显示 100%**（因为它"成功"解析了，只是解析错了）。

修法是词法级约束：只有当 `<` **紧贴**前一个 token（`self.peek().start == prev.end`）时才当
泛型括号。这条规则同时满足 `ARRAY<INT64>` 与 `NUMBER(38, 9)`，且 `DATE < x`（有空格的比较）
永远走比较路径。

**结论**：解析成功率不代表解析正确率，必须配一个独立的保真度检查（§5）。

## 4. 迭代方法：失败簇 → 一条规则

开发纪律：**在 train 上找缺口，dev 只做验收**（BIRD dev 与 train 的数据库完全不重叠，
train 69 库 / dev 11 库，所以"规则学的是 SQL 语法而不是这几个库的写法"这件事是可检验的）。

真实迭代过程（每轮都由 `tools/diagnose_parse.py` 的失败簇驱动）：

| 轮 | 失败簇（计数） | 现象 | 补的规则 |
|---|---|---|---|
| 1 | `expected ';' at '.'` ×239 | mysql 写 `T1.\`Col\``，名字链只吃了带反引号的首段 | `read_name_parts` 支持"引号段 + 点"任意拼接 |
| 2 | 同上 | `expected ')' at '.'`（函数实参里） | 同上（一处修复覆盖 3 个簇） |
| 3 | `expected identifier at 'FROM'` ×12 | gold 里 `SELECT a, FROM t` 多余逗号 | `select.trailing_comma` |
| 4 | `expected ')' at 'LAST'` ×59 | `ORDER BY x DESC NULLS LAST` | `NULLS FIRST/LAST` 正确消费 |
| 5 | `expected table reference at '"IDC"'` ×10 | spider2-snow 的 `sf_bq*` 被按 BigQuery 词法切（`"` → 字符串） | 方言映射修正 + 允许双引号字符串作标识符 |
| 6 | `expected table reference at '"PATENTS"'` ×9 | Snowflake `"DB"."SCHEMA"."TABLE"` | 同上 |
| 7 | `expected ')' at '=>'` ×12 | `FLATTEN(INPUT => x)` 命名实参 | `expr.named_arg`（表函数路径复用调用实参解析） |
| 8 | `expected ')' at '-'` ×15 | `bigquery-public-data.x.y` 未加引号 | 表名允许紧贴的 `-` |
| 9 | `expected ')' at ':'` ×12 | `value:"field"::NUMBER` 半结构化路径 | `:` 改成符号 + `expr.json_path` |
| 10 | `expected THEN at EOF` ×1 | §3 的 `::DATE < x` | `<` 需紧贴才当泛型括号 |
| 11 | `expected SELECT at 'CREATE'` ×1 | BigQuery `CREATE TEMP FUNCTION` | `stmt.create_function` |
| 12 | `expected THEN at 'core_app'` ×1 | `CASE WHEN a >= DATEADD(...) AND ...` 中 `>` 未消费 | 同 §3 |

最终 **7 个 split / 12,838 条 gold SQL 全部解析成功**（100.00%）：

| split | 方言 | 条数 | 覆盖 |
|---|---|---:|---:|
| bird_dev | sqlite | 1534 | 100% |
| bird_train | sqlite | 9428 | 100% |
| bird_minidev_sqlite | sqlite | 500 | 100% |
| bird_minidev_mysql | mysql | 500 | 100% |
| bird_minidev_postgresql | postgres | 500 | 100% |
| spider2_lite | bq/snow/sqlite | 256 | 100% |
| spider2_snow | snowflake | 120 | 100% |

分方言看，覆盖率从最低的 5.8%（snow，初版）走到 100%，其中 **Spider2 贡献了 BIRD 完全没有的
语法面**：CTE 619 次、`UNNEST` 65 次、`LATERAL FLATTEN`、`QUALIFY`、`NUMBER(38,9)` 转换、
`col:field` 路径 118 次。只用 BIRD 调解析器，会做出一个对 Spider2 完全不可用的前端。

## 5. 保真度：解析成功 ≠ 解析正确

`tools/verify_ast.py` 做两类与方言无关的检查：

1. **字面量守恒**：AST 里字符串/数字字面量的多重集必须等于源文本里 `string`/`num` token 的
   多重集，只允许"被重新归类"的原子（用作别名的字符串、`DECIMAL(10,2)` 里的类型参数、
   `T2.'Group'` 这种被当作标识符的字符串）。
2. **结构不变式**：`Select` 必有投影、`SetOp` 必有两侧、`Query`/`CTE` 必有 body、
   `Between` 必有两端、`DISTINCT` 聚合必有实参。

实测（12,838 条）：

```
bird_dev             1534 asts  literal-mismatch=0  invariant-violation=0  explained-atoms=0
bird_train           9428 asts  literal-mismatch=0  invariant-violation=0  explained-atoms=2
bird_minidev_sqlite   500 asts  literal-mismatch=0  invariant-violation=0  explained-atoms=0
bird_minidev_mysql    500 asts  literal-mismatch=0  invariant-violation=0  explained-atoms=2
bird_minidev_postgres 500 asts  literal-mismatch=0  invariant-violation=0  explained-atoms=0
spider2_lite          256 asts  literal-mismatch=0  invariant-violation=0  explained-atoms=4
spider2_snow          120 asts  literal-mismatch=0  invariant-violation=0  explained-atoms=12
total 12838 ASTs, 0 problems, 20 re-classified atoms
```

同时顺手量化了两个"看起来像文法缺口、其实是数据事实"的现象，作为独立统计而不是失败：

| 现象 | dev | train | lite | snow |
|---|---:|---:|---:|---:|
| `JOIN` 没有 `ON`/`USING`（笛卡尔积） | 0 | 12 | 5 | 7 |
| 字符串被当标识符 | 0 | 2 | 0 | 0 |

**这套检查抓到了真实 bug**：第 10 轮之前，`COUNT(DISTINCT CASE WHEN a::DATE > b::DATE ...)`
的解析会吞掉后面的整段（覆盖率依然是 100%）。只靠"解析成功"这一个指标，这个 bug 永远不会
被发现——这也是为什么后续的角色层必须建立在"已校准的 AST"上，而不是"能解析的 AST"上。

## 6. 与上一轮原型的差异

上一轮（`outputs/ir_roundtrip/`）是"反编译器 + lowering + 执行比对"，BIRD dev 98.9%，
未建模 CTE/窗口/集合运算。本轮是**新的前端**（不是补丁）：

| 维度 | 上一轮原型 | 本轮 |
|---|---|---|
| 目标 | 语义 IR（为 lowering 服务） | 语法 AST（为角色标注服务） |
| 方言 | sqlite 为主 | 5 个方言，BIRD 3 方言 + Spider2 2 方言 |
| 覆盖 | BIRD dev 98.9% / train 99.5% | 全部 7 split 100% |
| 未支持 | CTE 9、窗口 5、集合运算 3 | 无（集合运算、CTE、窗口、`UNNEST`、`LATERAL` 均实现） |
| 保真验证 | 执行 round-trip（需要 DB） | 字面量守恒 + 结构不变式（不需要 DB） |
| 语义信息 | 节点里的 role/cardinality 字段 | 列→角色槽位，模板 schema-free（见 03） |

## 7. 命令

```powershell
cd D:\JEV\exp_ast_slot
python -m sqlast.cli parse --config configs/datasets.yaml            # 全部 split
python -m sqlast.cli parse --config configs/datasets.yaml --split bird_dev --spans
python -m sqlast.cli show --sql "SELECT COUNT(*) FROM t" --dialect sqlite --rules
python tools\diagnose_parse.py bird_dev --top 12                     # 失败簇
python tools\verify_ast.py                                           # 保真度
```

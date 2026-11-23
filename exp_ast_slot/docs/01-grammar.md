# 01 · 文法（CFG）与方言差异

本实验把 gold SQL 的解析当作一次**编译器前端**任务：先写下上下文无关文法（CFG）作为
规范，再用手写递归下降实现它（见 [02-parser.md](02-parser.md)）。文法文件
[grammar.ebnf](grammar.ebnf) 是可读的规范版本，`sqlast/parser.py` 是唯一实现；两者用
"产生式 → 解析函数 → 规则 id"三列对照，规则 id 同时是运行期计数器（`Parser.hits`），
因此**每条规则在语料上被触发多少次、从来没有被触发过**都是可以自动统计的。

## 1. 为什么用 CFG 而不是直接用现成解析器

- 环境里没有 `sqlglot` / `sqlparse`（base python 3.12 只有标准库），且禁止随意装库。
- 更重要的是**可审计**：gold SQL 是评测基准，解析器的每一次"放宽"都会影响后续 AST /
  下游角色槽位的可信度。手写 CFG 让每条放宽都有名字（`from.comma_list`、`select.trailing_comma`）、
  有计数、可以单独关掉做消融；黑盒解析器做不到这一点。
- 五个方言（sqlite / mysql / postgres / bigquery / snowflake）只有**词法与少量语法**差异，
  把差异做成数据（`sqlast/lexer.py::DIALECTS` + 三处语法分支）比维护五套解析器便宜。

## 2. 覆盖范围（本实验实现的语法面）

按"BIRD + Spider2 gold SQL 实际出现"来裁剪，但**不做一次性简化**——CTE、窗口函数、集合运算
全部实现，因为 IR/AST 的节点集合一旦定下来，后面加节点会让形状签名与已存轨迹全部失效
（上一轮的结论）。已实现：

| 语法面 | 说明 |
|---|---|
| 脚本 | 多条语句、`DECLARE ... DEFAULT`、`SET`、`CREATE [TEMP] FUNCTION` |
| 查询 | `WITH [RECURSIVE]`、`UNION/INTERSECT/EXCEPT [ALL]`、`VALUES`、括号化查询 |
| SELECT | `DISTINCT`/`DISTINCT ON`、`AS STRUCT/VALUE`、`QUALIFY`、`WINDOW` 子句、`HAVING` |
| 投影 | 通配符、`t.*`、`* EXCEPT(...)`、`* EXCLUDE(...)`、`* REPLACE(...)`、别名（含引号别名） |
| FROM | 基本表、派生表（带列别名表）、括号化 join、`UNNEST ... WITH OFFSET`、表函数、`LATERAL` |
| JOIN | `INNER/LEFT/RIGHT/FULL/CROSS/NATURAL/STRAIGHT_JOIN`、`ON`、`USING`、无条件 join |
| 表达式 | Pratt 优先级、算术、字符串拼接、比较、`IS [NOT] NULL/TRUE/FALSE/DISTINCT FROM`、`IN`（列表/子查询/`UNNEST`）、`BETWEEN`、`LIKE/ILIKE/RLIKE/GLOB/REGEXP`、`NOT`、`EXISTS`、`CASE`、`CAST/TRY_CAST/SAFE_CAST`、`::`、`EXTRACT`、`INTERVAL`、数组/结构体、`[...]` 下标、半结构化路径 `col:field`、窗口函数 `OVER(...)` + frame、`FILTER (WHERE ...)`、`WITHIN GROUP`、`IGNORE/RESPECT NULLS`、`SUBSTRING(x FROM a FOR b)`、聚合内 `ORDER BY`、命名实参 `k => v` |
| 方言 | 反引号/双引号标识符、`"` 作字符串（mysql/bq）、反斜杠转义、`LIMIT a,b`（sqlite/mysql）、`x NOTNULL`/`ISNULL`（sqlite/mysql）、`#` 注释、BigQuery 未加引号的 `project-name.dataset.table`、`SAFE_OFFSET` 之类函数式下标 |

## 3. EBNF

完整版本见 [grammar.ebnf](grammar.ebnf)，这里是主干（`expr` 的递归细节由 Pratt 表描述，
见 02）：

```ebnf
script       ::= statement (";" statement)* ";"?
statement    ::= declare | set_var | create_function | query_expr

query_expr   ::= ["WITH" ["RECURSIVE"] cte ("," cte)*] set_expr [order_by] [limit_offset]
set_expr     ::= select_core (("UNION"|"INTERSECT"|"EXCEPT") ["ALL"|"DISTINCT"] select_core)*
select_core  ::= "(" query_expr ")" | "VALUES" row ("," row)* | select

select       ::= "SELECT" ["DISTINCT" ["ON" "(" expr ("," expr)* ")"] | "ALL"]
                 ["AS" ("STRUCT"|"VALUE")]
                 select_item ("," select_item)*
                 ["FROM" from_item ("," from_item)*]
                 ["WHERE" expr] ["GROUP" "BY" expr ("," expr)*]
                 ["HAVING" expr] ["QUALIFY" expr]
                 ["WINDOW" name "AS" "(" window_spec ")" ("," name "AS" "(" window_spec ")")*]

from_item    ::= table_factor join_clause*
join_clause  ::= join_words "JOIN" table_factor ["ON" expr | "USING" "(" name ("," name)* ")"]

expr         ::= prefix infix*                      (* Pratt: 见 02 的绑定力表 *)
prefix       ::= literal | name_chain | "(" query_expr ")" | "(" expr ("," expr)* ")"
               | ("-"|"+"|"~") expr | "NOT" expr
               | case_expr | cast | extract | interval | exists
               | array_lit | struct_lit | typed_literal | parameter
```

## 4. 产生式 → 函数 → 规则 id

规则 id 与 `parser.py` 中的 `self.hit("<id>")` 一一对应，`outputs/ast/rules.json` 给出每个
split 的触发次数。下表是 dev（1534 条）上的实测触发数（完整表见
`outputs/reports/parse_report.md`）：

| 规则 id | 产生式 | dev 触发 |
|---|---|---:|
| `stmt.select` | `select` | 1686 |
| `query.query` | `query_expr` | 1683 |
| `query.with` / `query.cte` | `WITH` 子句 / 单个 CTE | 13 / 13 |
| `query.setop` | `set_expr` 的 `UNION/INTERSECT/EXCEPT` | 3 |
| `query.order_by` / `query.limit` / `query.offset` | 后置算子 | 309 / 297 / 0 |
| `select.distinct` / `select.distinct_on` | 投影去重 | 180 / 0 |
| `select.group_by` / `select.having` / `select.qualify` | 分组与后置过滤 | 122 / 21 / 0 |
| `select.star` / `select.qualified_star` / `select.star_except` | 通配符 | 0 / 0 / 0 |
| `from.table` / `from.derived` / `from.unnest` / `from.table_function` | 表因子 | 3077 / 34 / 0 / 0 |
| `from.join.inner` / `from.join.left` / `from.join.using` | 连接 | 1427 / 1 / 0 |
| `from.comma_list` | 逗号连接（隐式 CROSS） | 0 |
| `expr.binary` / `expr.unary` / `expr.bool.and` / `expr.bool.or` / `expr.not` | 运算符 | 4166 / 3 / 807 / 46 / 0 |
| `expr.column` / `expr.qualified_column` | 列引用 | 1292 / 6831 |
| `expr.literal.string` / `.number` / `.null` / `.bool` | 字面量 | 2064 / 1534 / 36 / 2 |
| `expr.agg` / `expr.agg_distinct` / `expr.func` | 函数调用 | 905 / 74 / 407 |
| `expr.cast` / `expr.case` / `expr.extract` / `expr.interval` | 表达式构造 | 196 / 148 / 0 / 0 |
| `expr.is_null` / `expr.between` / `expr.like` / `expr.in_list` / `expr.in_subquery` | 谓词 | 112 / 77 / 53 / 22 / 28 |
| `expr.subquery` / `expr.exists` | 嵌套查询 | 71 / 3 |
| `expr.window` / `window.frame` | 窗口 | 5 / 0 |

**未被触发的规则**同样是信息：`select.star`、`expr.interval`、`from.unnest`、`expr.extract`、
`expr.colon_cast` 在 dev 上全部为 0，但在 Spider2（BigQuery / Snowflake）上是主力——
`query.cte` 619、`from.unnest` 65、`from.comma_list` 125、`expr.window` 130、
`expr.json_path` 118、`expr.extract` 90（lite）/ 24（snow）、`expr.colon_cast` 63/176。
这解释了为什么"只在 BIRD 上调解析器"会做出一个对 Spider2 完全不可用的前端：BIRD 的
AST 语法面只是 Spider2 的子集。

## 5. 方言差异（全部是数据，不是分叉的解析器）

| 差异点 | sqlite | mysql | postgres | bigquery | snowflake |
|---|---|---|---|---|---|
| 标识符引号 | `` ` `` / `"` | `` ` `` | `"` | `` ` `` | `"` |
| 字符串引号 | `'` | `'` `"` | `'` (+`$$`) | `'` `"` | `'` |
| 反斜杠转义 | 否 | 是 | 否 | 是 | 是 |
| `#` 行注释 | 否 | 是 | 否 | 是 | 否 |
| `LIMIT a, b` | 是 | 是 | 否 | 否 | 否 |
| `QUALIFY` | 否 | 否 | 否 | 是 | 是 |
| `::type` 转换 | 否 | 否 | 是 | 否 | 是 |
| `x NOTNULL` / `ISNULL` | 是 | 是 | 否 | 否 | 否 |
| `UNNEST` / `* EXCEPT` | 否 | 否 | 否 | 是 | 否 |
| `col:field` 半结构化 | 否 | 否 | 否 | 否 | 是 |
| 未加引号的 `a-b.c` 表名 | 否 | 否 | 否 | 是 | 否 |

只有三处是真正的**语法**分支（其余都在词法或词表里）：`LIMIT a,b`、
`DISTINCT ON`（postgres）、`QUALIFY`（bigquery/snowflake）。

## 6. 为 gold SQL 的"不严格合法"保留的宽松规则

gold SQL 并不总是严格合法；这些放宽**全部有名有数**，可以单独关闭复现严格模式的失败率：

| 规则 id | 放宽内容 | dev / train / spider2-lite |
|---|---|---:|
| `select.trailing_comma` | `SELECT a, FROM t`（多余逗号） | 0 / 0 / 17 |
| `expr.quoted_ident_string` | 字符串被当作标识符：`T2.'Group'`（BIRD 的 pg 变体把列名写成字符串） | 0 / 2 / 0 |
| 无条件 `JOIN`（无独立 id，按 join 节点统计） | `INNER JOIN t` 无 `ON`，gold 里真实存在 | 0 / 12 / 5（snow 7） |
| `expr.is_compare` | sqlite 允许 `x IS NOT ''` 这类非 NULL 的 `IS` 比较 | 0 / 1 / 0 |
| `expr.notnull` | sqlite/mysql 后缀空值判定 `x NOTNULL` | 0 / 1 / 0 |
| BigQuery 未加引号项目名（无独立 id） | `bigquery-public-data.x.y`，用 `-` 连接且两侧无空格 | — / — / 15 |

这些规则在 `parser.py` 里都有单独 `self.hit(...)`（两项纯词法放宽除外，已在括号里注明），
因此"放宽了多少"永远是量化过的；这也是 [02-parser.md](02-parser.md) §5 里
"覆盖率 + 保真度"双指标的依据。

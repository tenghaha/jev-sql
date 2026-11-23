# 03 · 角色槽位（role slot）

代码：`sqlast/roles.py`。产物：`outputs/roles/role_ast_<split>.{json,jsonl}`、
`outputs/roles/role_schema.json`、`outputs/reports/role_report.md`。
审计工具：`tools/verify_roles.py`（往返）、`tools/verify_template.py`（解耦）。

## 1. 结构：枝干保留，三类叶子换成槽

一条查询的**枝干结构**（算子、嵌套、谓词树的形状、每个子句里"有几个待填的位置"）是可迁移的；
**表名、列名、具体取值**绑定的都是某个库的具体内容，不可迁移。所以这一步只做一件事：

> 遍历 AST，把每个表引用、列引用、字面量取值（以及结构体字段访问）替换成
> `Slot{role, kind, sid}` 节点，其余节点一个不动。

```
原 AST                                           模板（无 schema、无取值）
Query                                            Query  cardinality=TOP_K
└─ Select                                        └─ Select
   ├─ items[0] BinaryOp("/")                        ├─ items[0] BinaryOp("/")
   │   ├─ Column `Free Meal Count (K-12)`  ──►      │   ├─ Slot{MEASURE, column, 1}
   │   └─ Column `Enrollment (K-12)`       ──►      │   └─ Slot{MEASURE, column, 2}
   ├─ from [Table frpm]                    ──►      ├─ from [Table name=Slot{TABLE, table, 3}]
   ├─ where  =                                      ├─ where  =
   │   ├─ Column `County Name`             ──►      │   ├─ Slot{FILTER, column, 4}
   │   └─ Literal 'Alameda'                ──►      │   └─ Slot{FILTER, value, 5}
   ├─ order_by  ...                                 ├─ order_by  ...
   └─ limit Literal 1                      ──►      └─ limit Slot{FILTER, value, 8}
```

每个查询产出两部分：

| 部分 | 内容 | 是否含 schema/取值 |
|---|---|---|
| `template` | 枝干 + `Slot{role, kind, sid}` + 别名 + 关键字 | **不含表名、列名、取值** |
| `slots` | `sid → {role, kind, path, ...}` + gold 里填了什么 | 是（silver label，供训练/评测） |

保留 `sid` 是为了把模板的洞和侧表对齐：模板本身与库无关，标注在侧表，两者不混。

## 2. 六个角色 & 四种 kind

角色说明"这个洞做什么用"，kind 说明"该填什么"。两者正交：`role` 由子句决定，`kind` 由被替换的
节点类型决定。

| role | 来源子句 | 语义 |
|---|---|---|
| `TABLE` | `FROM` / `JOIN` 里的每个表引用 | 这个分支从哪个关系读数据 |
| `JOINKEY` | `JOIN ... ON` / `USING`；`IN` / `=` / `EXISTS` 子查询的投影 | 连接两个关系的列 |
| `MEASURE` | `SELECT` 列表的每个列/值；以及任何子句里落在聚合内部的列 | 答案要报告的列或量 |
| `DIMENSION` | `GROUP BY` 键（及窗口 `PARTITION BY`、`DISTINCT ON (...)`） | 答案的分组粒度 |
| `FILTER` | `WHERE` / `HAVING` / `QUALIFY` 谓词（不在聚合内）；`LIMIT`/`OFFSET` 的计数 | 被条件约束的列，和与它比较的取值 |
| `SORT` | `ORDER BY` 键（含窗口 `ORDER BY`） | 排序依据 |

| kind | 填什么 | 出现在哪 |
|---|---|---|
| `table` | 表名（可多段 `db.schema.table`） | `Table.name` |
| `column` | 列名 | 列引用、`USING (...)`、`* EXCEPT (...)`、`* REPLACE (... AS c)` |
| `value` | 字面量取值（字符串/数字/日期） | 谓词两侧、`SET`/`DECLARE` 的默认值、`LIMIT k`、`ORDER BY 1` 的序数 |
| `field` | 结构体 / JSON 字段访问 | `x.family`（BigQuery struct）、`v:key`（Snowflake 半结构化） |

三条细化规则保持语义准确而不膨胀结构：

1. **聚合优先**：列只要在聚合内部就是 `MEASURE`，不管聚合写在哪（`HAVING MAX(x) > 3`、
   `ORDER BY AVG(x)` 里的 x 也是 `MEASURE`）。
2. **别名不占槽**：`ORDER BY total` 里 `total` 是投影别名，保留为 `AliasRef` 节点——
   要求模型把别名绑到表列上是错的。
3. **关键字不占槽**：`NULL` / `TRUE` / `FALSE` 是语法关键字，不是数据取值，保留为 `Literal`。

## 3. 槽位携带什么

槽节点本身只有三个字段：`{"type":"Slot","role":"MEASURE","kind":"column","sid":1}`。
填写内容在侧表里，按 kind 不同：

| 字段 | 适用 | 用途 |
|---|---|---|
| `sid` | 全部 | 与模板里的 `Slot.sid` 对齐 |
| `role` / `kind` | 全部 | 角色 / 该填什么 |
| `path` | 全部 | 在树里的位置（`stmts[].body.where.l`），可把值写回模板 |
| `table` / `table_parts` | table | 表名（含多段名），即 gold 选的关系 |
| `column` / `qualifier` / `table` | column | 列名、书写前缀、归属表 |
| `binding` | column | `explicit`（带限定符）/ `inferred`（schema 唯一推断）/ `ambiguous` / `unresolved` |
| `data_type` | column | `integer` / `real` / `text` / `date`（有 schema 时） |
| `agg` | column (`MEASURE`) | 它喂给哪个聚合（`SUM`/`COUNT`/…） |
| `key_role` | column (`JOINKEY`) | `pk` / `fk` / `both`（有 schema 时） |
| `value` / `value_class` | value | 取值与类型（`string`/`number`/`date_string`） |
| `format_token` | value | 是否像 `%Y%m%d` 这类格式串（换库时要改） |
| `typed` | value | `DATE '...'` 这类带类型前缀的字面量 |
| `field` / `accessor` | field | 字段名与访问方式（`struct` / `json`） |

`data_type` / `key_role` / `format_token` 是**校验用**标签：不参与模板，也不要求预测，
但让"角色填错"可以被自动发现（见 §6）。

## 4. 一个完整例子（BIRD dev 第 0 条）

```
Q:  What is the highest eligible free rate for K-12 students in Alameda County?
SQL:SELECT `Free Meal Count (K-12)` / `Enrollment (K-12)` FROM frpm
     WHERE `County Name` = 'Alameda'
     ORDER BY (CAST(`Free Meal Count (K-12)` AS REAL) / `Enrollment (K-12)`) DESC LIMIT 1
```

模板（`outputs/roles/role_ast_bird_dev.json` 第 1 条，已去掉 `null` 字段）：

```json
{"type":"Query","cardinality":"TOP_K","body":{"type":"Select",
 "items":[{"type":"SelectItem","expr":{"type":"BinaryOp","op":"/",
   "l":{"type":"Slot","role":"MEASURE","kind":"column","sid":1},
   "r":{"type":"Slot","role":"MEASURE","kind":"column","sid":2}}}],
 "from":[{"type":"Table","name":{"type":"Slot","role":"TABLE","kind":"table","sid":3}}],
 "where":{"type":"BinaryOp","op":"=",
   "l":{"type":"Slot","role":"FILTER","kind":"column","sid":4},
   "r":{"type":"Slot","role":"FILTER","kind":"value","sid":5}}},
 "order_by":[{"type":"OrderItem","expr":{"type":"Paren","e":{"type":"BinaryOp","op":"/",
   "l":{"type":"Cast","e":{"type":"Slot","role":"SORT","kind":"column","sid":6},"to":"REAL"},
   "r":{"type":"Slot","role":"SORT","kind":"column","sid":7}}},"desc":true}],
 "limit":{"type":"Limit","expr":{"type":"Slot","role":"FILTER","kind":"value","sid":8}}}
```

侧表：

| sid | role | kind | gold 填的 | 说明 |
|---:|---|---|---|---|
| 1 | `MEASURE` | column | `Free Meal Count (K-12)` → frpm | 比值分子 |
| 2 | `MEASURE` | column | `Enrollment (K-12)` → frpm | 比值分母 |
| 3 | `TABLE` | table | `frpm` | 唯一关系 |
| 4 | `FILTER` | column | `County Name` → frpm | 过滤列 |
| 5 | `FILTER` | value | `'Alameda'` | 过滤取值 |
| 6 | `SORT` | column | `Free Meal Count (K-12)` | 排序键（与 1 同列） |
| 7 | `SORT` | column | `Enrollment (K-12)` | 排序键（与 2 同列） |
| 8 | `FILTER` | value | `1` | `LIMIT 1` 的 k |

角色序列 `MEASURE>MEASURE>TABLE>FILTER>FILTER>SORT>SORT>FILTER` 就是这条查询的可迁移骨架：
"从一个表里取两个量相除、按某列等值过滤、按该比值降序、取前 1 条"。

## 5. 实测（`outputs/reports/role_report.md`）

| split | 查询数 | 槽位 | table | column | value | field | 槽/查询 |
|---|---:|---:|---:|---:|---:|---:|---:|
| bird_dev | 1534 | 14,796 | 3077 | 8121 | 3598 | 0 | 9.65 |
| bird_train | 9428 | 89,829 | 19,577 | 50,077 | 20,175 | 0 | 9.53 |
| bird_minidev_sqlite / mysql / postgresql | 500×3 | 5,708 / 5,700 / 5,789 | 1086 | 3093 | 1521 | 0 | 11.4–11.6 |
| spider2_lite | 256 | 11,755 | 1531 | 7239 | 2850 | 135 | 45.9 |
| spider2_snow | 120 | 6,822 | 903 | 4244 | 1533 | 142 | 56.9 |

列槽的角色分布（dev）：

| role | 槽位数 | 覆盖查询 |
|---|---:|---:|
| `JOINKEY` | 3012 (37.1%) | 1178 (77%) |
| `MEASURE` | 2484 (30.6%) | 1526 (99%) |
| `FILTER` | 2213 (27.3%) | 1383 (90%) |
| `SORT` | 274 (3.4%) | 233 (15%) |
| `DIMENSION` | 138 (1.7%) | 119 (8%) |

取值槽的角色分布（dev，共 3598：字符串 2064 / 数字 1534）：
`FILTER` 2652 (73.7%) · `MEASURE` 789 (21.9%) · `JOINKEY` 109 (3.0%) · `SORT` 40 (1.1%) ·
`DIMENSION` 8 (0.2%)。**四分之三的取值出现在过滤条件里**，与"问句主要是在限定条件"的直觉一致。

### 5.1 模板归并阶梯

| 归一化层级 | bird_dev 去重 | bird_train 去重 |
|---|---:|---:|
| 名字保留（表名 + 列名），只抹掉取值 | 1464 | 8828 |
| 列 → 角色（表名保留） | 1319 | 7806 |
| 列 + 表 + 取值 → 槽 | **863** | **3512** |

### 5.2 跨 split 迁移（BIRD dev 与 train 的库完全不重叠）

| reference → evaluation | 名字保留 | 列→角色 | 完整模板 |
|---|---:|---:|---:|
| bird_train → bird_dev | 0/1464 (0.0%) | 2/1319 (0.1%) | **274/863 (31.8%)** |
| bird_train → bird_minidev_sqlite | 0/491 (0.0%) | 1/481 (0.2%) | 108/395 (27.3%) |
| spider2_lite → spider2_snow | 1/120 (0.8%) | 1/120 (0.8%) | 1/120 (0.8%) |

只有把**表名和取值一起槽化**，跨库模板复用才从 0.1% 跳到 31.8%——这是这次改进最直接的收益。

## 6. 解耦审计：模板是否真的与 schema/取值无关

`tools/verify_template.py` 做两个独立检查，覆盖全部 12,838 条：

**(1) 残留扫描**：模板里不允许出现
`Column` 节点、`kind` 为 string/number 的 `Literal`、`TypedLiteral`、
非 Slot 的 `Table.name`、以及 `USING (...)` / `* EXCEPT (...)` / `* REPLACE ... AS c` 里的裸字符串。

**(2) 重命名不变性**：把**解析后的 AST** 里每一个表名段、列名、字面量取值、struct/JSON 字段、
`USING`/`EXCEPT` 列名都改写成新造的名字，再跑一遍角色标注，要求得到的模板
**逐字节相同**（签名 + 角色序列都相同）。如果模板还依赖任何真实名字或取值，这一条必然失败。

实测结果：

```
bird_dev        1534 templates  residue=0  rename-invariance-fail=0
bird_train      9428 templates  residue=0  rename-invariance-fail=0
bird_minidev_*  500x3 templates residue=0  rename-invariance-fail=0
spider2_lite     256 templates  residue=0  rename-invariance-fail=0
spider2_snow     120 templates  residue=0  rename-invariance-fail=0
total 12838 templates, 0 problems
```

配合另外两级校验，这一层现在是**三重闭合**的：

| 检查 | 工具 | 结果 |
|---|---|---|
| AST 保真（字面量守恒 + 结构不变式） | `tools/verify_ast.py` | 12,838 条 0 problems |
| 角色往返（`template + slots` 逐节点重建原 AST） | `tools/verify_roles.py` | 12,838 条 0 problems |
| 模板解耦（残留扫描 + 重命名不变性） | `tools/verify_template.py` | 12,838 条 0 problems |

审计过程本身抓到了 4 个真实漏点，都是"AST 里存在但没遍历到"的位置：
`SET var = [...]` 的取值、`LATERAL FLATTEN(...)` 表函数的实参、窗口 frame 的边界值
（`ROWS BETWEEN 2 PRECEDING`）、`DISTINCT ON (...)` 的键。没有这个检查，它们会静静留在模板里。

## 7. 接决策模型：一个槽就是一道选择题

| role | kind | 候选集（可枚举） | 校验 |
|---|---|---|---|
| `TABLE` | table | 目标库的表（含 schema 前缀） | 名字存在；与该分支的列归属一致 |
| `JOINKEY` | column | 键列（PK/FK 优先）+ 与另一侧同类型的列 | 两侧类型相容、join 图连通 |
| `MEASURE` | column | 数值/日期列（`COUNT` 允许任意列）；非聚合时任意列 | 与聚合函数相容 |
| `MEASURE` | value | 问题/evidence 里抽出的数值或字符串 | 与列类型相容 |
| `DIMENSION` | column | 类别/时间列 | 分组基数合理 |
| `FILTER` | column | 任意列（优先问句提及的） | 与取值类型相容 |
| `FILTER` | value | 列值域（值索引）或文本字面量 | 回查该值确实存在于列中 |
| `SORT` | column | 可排序列 | 排序键可与投影不同 |
| `SORT`/`FILTER` | value | `k`（`LIMIT`）、序数、格式串 | `k` 与"top n"措辞一致 |

无依赖的槽可以**同批**提问（JEV 式批式决策）；有依赖的只有"同表约束"——`TABLE` 定下来之后，
其余槽的候选集才能按表过滤。角色把依赖图压得很浅。

## 8. 取舍与限制

- **`LIMIT k` 被槽化**，所以 `TOP_K` 模板不再区分 `k=1` 与 `k=3`（改由 `cardinality` + 侧表的
  `value` 表达）。这是"取值全槽化"的必然代价，换来的是模板跨库可用。
- **`ORDER BY 1` 这类序数引用**变成 `SORT` 取值槽：模板不再表达"按第 1 列排序"，只剩"按某个值排序"。
  语料里很少（dev 只有 40 个 SORT 取值槽），但语义上确实有损失。
- **`NULL`/`TRUE`/`FALSE` 不槽化**：它们是关键字而非数据取值。
- **别名、CTE 名、窗口名保留**：它们是用户自定义标识，不是 schema；`AliasRef` 也让
  "别名 vs 列"的区分留在模板里。所以严格说模板是"与**数据库 schema 与数据取值**解耦"，
  而不是"与所有标识符解耦"。
- **`* EXCEPT (a,b)` 的角色是启发式的**（记为 `DIMENSION`），因为它是"从通配符里去掉的属性"，
  六个角色里没有精确对应。
- **`value` 槽的类型只到 `string`/`number`/`date_string`**：不做更细的值域推断（那属于绑定阶段）。
- **Spider2 无 schema**：`data_type` / `key_role` 缺席，报告里的 sanity 检查只对 BIRD 有效。

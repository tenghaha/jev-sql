# 01 · 第一步消融：LLM 直接产 slot-AST 模板 → JEV 填槽 → SQL

代码：`slotpipe/`（`template.py` 生成与校验、`fill.py` 逐 slot 绑定、`render.py` 反解析、`pipeline.py` 编排）。
配置：`configs/step1_bird_dev.yaml`。入口：`scripts/run_step1.py`。
产物：`outputs/runs/<run>/step1_records.jsonl`（每题一条）、`step1_prompts.jsonl`（每题两份 prompt：simple / complex）。

## 1. 这一步要验证什么

零样本（无示例）下把"生成"拆成两段：

| 段 | 谁做 | 输入 | 输出 |
|---|---|---|---|
| 模板 | LLM，**每类各一次**调用（simple / complex） | 问题 + **evidence** + **DB 表级摘要**（无列清单） | 1 个 schema 无关的 slot-AST 模板 |
| 表槽 | JEV `noul` | 问题 + evidence + **整个 slot-AST** + 全部表卡片 | 每个 (表槽, 表) 一个概率 |
| 列槽 | JEV `noul` | 问题 + evidence + **整个 slot-AST** + 单表列级 schema | 每个 (列槽, 列) 一个概率 |
| SQL | 确定性反解析 | 填满的 AST | SQLite 语句（并做只读执行校验） |

两条硬约束：

- **值槽与模板隔离**：数据写在值槽的 `value` 字段，流程立刻抽出成独立 `values` 映射（`sid → 标量`），
  产物里的模板保持"无表名、无列名、无数据"；校验器拒绝任何残留（见 §4）。JEV 阶段拿不到 `values`
  映射本身，只在槽位说明里看到"compared with the value 'Alameda County'"这类文字。
- **多选**：选表、选列都允许多选——`noul ≥ fill.noul_threshold`（默认 0.5）的候选**全部**留在该槽的
  `positives` 里；最后由绑定规则每槽取一个。

## 2. 模板生成：evidence + 简/繁两个模板

prompt 依次是：表卡片 → **evidence 块** → AST 节点清单 → 硬规则 → 本模板的复杂度约束 → 问题。

**evidence 必读**（`_RULES` 第 6 条，原文）：

> Follow the evidence. When the evidence defines how a value is computed (for example
> "eligible free rate = free meal count / enrollment"), reproduce that computation as the
> same arithmetic over MEASURE slots, with FILTER slots for the conditions it mentions.
> Do not replace a defined computation by some other single column, and do not invent a
> computation the evidence does not support.

两个模板的约束（`_SIMPLE_RULES` / `_COMPLEX_RULES`）：

| | 约束 |
|---|---|
| **simple** | 只有一条语句：一个 `Query` + 一个 `Select`（无集合运算）；函数只允许 `COUNT/SUM/AVG/MIN/MAX/TOTAL` + 算术与比较；过滤只在 WHERE/HAVING；无 `CASE`；无 CTE；无窗口函数（`OVER`）；子查询嵌套 ≤ 1 层 |
| **complex** | 允许子查询、`IN/EXISTS`、`UNION/INTERSECT/EXCEPT`、CTE、`CASE`、窗口函数，**嵌套不限层数**；允许**多轮生成**（`complex_rounds`） |

生成流程（`generate_templates`）：每类各发一次请求，返回 `{"template": {"type":"Script","stmts":[...]}}`；
校验失败把错误带回重试（simple 默认 2 轮、complex 默认 3 轮）；某一类最终失败记为 `template_failed`，
另一类照常使用。simple 的复杂度约束是**生成后校验**（§4），不满足就重试。

## 3. JEV 填槽：每个 slot 一组问题

### 3.1 共享 state

两类请求的 state 都带同一份上下文：

| 字段 | 内容 |
|---|---|
| `context.question` / `context.evidence` | 原问题与 evidence |
| `context.template` | **整个 slot-AST**，序列化成 JSON **字符串**（不是嵌套对象） |
| `slots` | 本次要填的槽及其位置说明（表槽：槽位置；列槽：角色 + 子句 + 聚合 + 是否算术操作数 + 同侧值） |
| `candidates` | 候选（表卡片 / 某表的列） |
| `output_schema` | `{"noul": "probability in [0, 1]"}` |

### 3.2 表槽：一次请求，全部表卡片 × 全部表槽

`qid 0`（`california_schools`，2 个表槽 × 3 张表 = 6 条问题）的实际请求（节选 2 个候选、2 条问题）：

```json
{
  "state": {
    "task": "table_slot_binding",
    "instruction": "Every TABLE slot of the template below must be filled by one database table. Each candidate is a table card (row count, column count, links and a one-line summary; column lists are omitted). Answer, per slot and per candidate, whether that table could fill that slot. Several tables may qualify for the same slot; answer only the provided questions.",
    "context": {
      "database": "california_schools",
      "question": "What is the highest eligible free rate for K-12 students in the schools in Alameda County?",
      "evidence": "Eligible free rate for K-12 = `Free Meal Count (K-12)` / `Enrollment (K-12)`",
      "template": "{\"type\":\"Script\",\"stmts\":[{\"type\":\"Query\",\"body\":{\"type\":\"Select\",\"items\":[{\"type\":\"SelectItem\",\"expr\":{\"type\":\"Func\",\"name\":\"MAX\",\"args\":[{\"type\":\"BinaryOp\",\"op\":\"/\",\"l\":{\"type\":\"Slot\",\"role\":\"MEASURE\",\"kind\":\"column\",\"sid\":1},\"r\":{\"type\":\"Slot\",\"role\":\"MEASURE\",\"kind\":\"column\",\"sid\":2}}]}}],\"from\":[{\"type\":\"Join\",\"kind\":\"INNER\",\"left\":{\"type\":\"Table\",\"name\":{\"type\":\"Slot\",\"role\":\"TABLE\",\"kind\":\"table\",\"sid\":3}},\"right\":{\"type\":\"Table\",\"name\":{\"type\":\"Slot\",\"role\":\"TABLE\",\"kind\":\"table\",\"sid\":4}},\"on\":{\"type\":\"BinaryOp\",\"op\":\"=\",\"l\":{\"type\":\"Slot\",\"role\":\"JOINKEY\",\"kind\":\"column\",\"sid\":5},\"r\":{\"type\":\"Slot\",\"role\":\"JOINKEY\",\"kind\":\"column\",\"sid\":6}}}],\"where\":{\"type\":\"BinaryOp\",\"op\":\"=\",\"l\":{\"type\":\"Slot\",\"role\":\"FILTER\",\"kind\":\"column\",\"sid\":7},\"r\":{\"type\":\"Slot\",\"role\":\"FILTER\",\"kind\":\"value\",\"sid\":8}}}}]}"
    },
    "slots": [
      {"slot_id": "s3", "role": "TABLE", "usage": "one side of a join; the query compares a key across two relations"},
      {"slot_id": "s4", "role": "TABLE", "usage": "one side of a join; the query compares a key across two relations"}
    ],
    "candidates": {
      "t1": {"table_name": "frpm", "rows": 9986, "n_columns": 29, "links_to": ["schools"],
             "summary": "Stores free or reduced-price meal eligibility data for California schools, with one row per school and academic year."},
      "t2": {"table_name": "satscores", "rows": 2269, "n_columns": 11, "links_to": ["schools"],
             "summary": "Stores SAT score and test participation data for California schools, one row per school record."}
    },
    "output_schema": {"noul": "probability in [0, 1]"}
  },
  "questions": {
    "s1_3_t1": {"type": "noul",
                "instructions": "Can table frpm fill slot #3, which is one side of a join; the query compares a key across two relations?"},
    "s1_4_t1": {"type": "noul",
                "instructions": "Can table frpm fill slot #4, which is one side of a join; the query compares a key across two relations?"}
  }
}
```

问题 id 规则：`s1_<槽号>_<候选表号>`。槽位说明三选一（由模板结构算出）：
`the relation the query selects from` / `one side of a join; the query compares a key across two relations` /
`the single relation of a subquery`。

### 3.3 列槽：一表一次请求，本表全部列 × 本表相关列槽

表由 §3.2 的绑定结果决定；本模板绑到 `frpm` 与 `schools`，因此两次请求：
`frpm`（5 个列槽 × 29 列 = 145 条问题）、`schools`（5 × 49 = 245 条）。
`frpm` 那次的实际请求（节选 2 个候选、2 条问题）：

```json
{
  "state": {
    "task": "column_slot_binding",
    "instruction": "The template below still has column slots to fill. Only the columns of the focus table are candidates in this request. Answer, per slot and per column, whether that column could fill that slot. Several columns may qualify for the same slot; answer only the provided questions.",
    "context": {
      "database": "california_schools",
      "question": "What is the highest eligible free rate for K-12 students in the schools in Alameda County?",
      "evidence": "Eligible free rate for K-12 = `Free Meal Count (K-12)` / `Enrollment (K-12)`",
      "template": "{\"type\":\"Script\",\"stmts\":[...]}",
      "table_summary": "Stores free or reduced-price meal eligibility data for California schools, with one row per school and academic year."
    },
    "focus_table": {"table_name": "frpm", "primary_key": ["CDSCode"]},
    "slots": [
      {"slot_id": "s1", "role": "MEASURE",
       "usage": "role MEASURE, in the SELECT list, aggregated with MAX, an operand of an arithmetic expression"},
      {"slot_id": "s7", "role": "FILTER",
       "usage": "role FILTER, in the WHERE clause, compared with the value 'Alameda County'"}
    ],
    "candidates": {
      "frpm_c1": {"table_name": "frpm", "column_name": "CDSCode", "dtype": "text",
                  "sample": "01100170109835", "primary_key": true},
      "frpm_c2": {"table_name": "frpm", "column_name": "Academic Year", "dtype": "text",
                  "sample": "2014-2015", "primary_key": false}
    },
    "output_schema": {"noul": "probability in [0, 1]"}
  },
  "questions": {
    "s2_frpm_1_frpm_c1": {"type": "noul",
      "instructions": "Can column frpm.CDSCode (text, e.g. 01100170109835) fill slot #1, which is role MEASURE, in the SELECT list, aggregated with MAX, an operand of an arithmetic expression?"},
    "s2_frpm_1_frpm_c2": {"type": "noul",
      "instructions": "Can column frpm.Academic Year (text, e.g. 2014-2015) fill slot #1, which is role MEASURE, in the SELECT list, aggregated with MAX, an operand of an arithmetic expression?"}
  }
}
```

问题 id 规则：`s2_<表名>_<槽号>_<候选列号>`。槽位说明的组成（`column_slot_usage`）：
角色 → 子句（SELECT/WHERE/GROUP BY/ORDER BY/HAVING/JOIN）→ 聚合函数 → 是否算术操作数 →
若同侧是值槽则写出**该值的文本**。这一句是 measure 能绑对的关键信号。

### 3.4 回答与多选

JEV 每条问题回一个 `noul` 概率：

```json
{"answers": {"s2_frpm_1_frpm_c1": {"type": "noul", "noul": 0.42},
             "s2_frpm_1_frpm_c3": {"type": "noul", "noul": 0.94}},
 "usage": {"input_tokens": 5120, "output_tokens": 1450}}
```

`noul ≥ threshold` 的**全部**进入该槽 `positives`（记录里保留 `table/column/dtype/noul`）；
同时记录每个槽的 `best`（最高分，即使没过阈值）。

### 3.5 每槽取一个

`_select_tables` / `_select_columns` 按 `(类型是否匹配, 是否已被其它槽占用, -noul)` 排序取第一。
类型匹配只在"聚合要数值 / 算术操作数要数值"时生效（`SUM/AVG/TOTAL/MEDIAN/...` 或 `/ * + -` 的操作数）。
若某槽没有任何阳性候选，取该槽 `best` 兜底并把 `fallback: true` 写进记录（不静默失败）。

### 3.6 绑定、别名与作用域

`fill()` 把槽替换成真实节点：表槽 → `Table.name.parts`，列槽 → `Column.parts = [别名, 列名]`，
值槽 → `Literal`（字符串加引号、数字原样）。别名按 query block 分配：

- 同一 FROM 内重复别名会被换成新的 `t{n}`（否则 SQLite 报 `ambiguous column name`）；
- 列只能用它所在 query block（或外层，做相关子查询）可见的别名，否则记 `unresolved_slots` 而不产出 SQL。

### 3.7 请求数与成本

每个候选模板的 JEV 请求数 = **1（表槽）+ 已绑定表数（列槽）**；问题数 ≈ 表槽数 × 表数 +
列槽数 × 各表列数。`qid 0` 的 simple 候选是 3 次请求（6 + 145 + 245 = 396 条问题）；
24 题实测每候选中位 **306** 条问题。

## 4. 校验（模板必须与 schema 无关）

`slotpipe/template.py` 在绑定之前逐项拒绝：

| 检查 | 拒绝条件 |
|---|---|
| 顶层结构 | 不是 `Script`，或 `stmts` 为空 |
| 节点类型 | 不在允许集合内（少量同义词归一，如 `Aggregate → Func`）；无 `type` 的空对象视为普通容器 |
| 槽合法性 | role 不在 6 种内、kind 不在 4 种内 |
| 表引用 | `Table.name` 不是 `kind=table` 的槽 → 写了真实表名 |
| 列引用 | 出现 `Column` 节点 → 写了真实列名 |
| 内联数据 | `Literal` 的 kind 是 `string`/`number` → 没把值放进值槽 |
| 值完整性 | 值槽没有 `value` 字段 |
| 复杂度（simple） | 语句数 ≠ 1、有集合运算、有 CTE、有 `CASE`、函数不在白名单、有 `OVER`、子查询嵌套 > 1 |

`pipeline.py` 在渲染前还要求所有槽都已解析，否则该候选判 `unresolved_slots`。

## 5. 复现

```powershell
cd D:\JEV\exp_slot_pipeline
python scripts\run_step1.py --config configs\step1_bird_dev.yaml --limit 5
python scripts\run_step1.py --config configs\step1_bird_dev.yaml --limit 24 --offset 24 --resume
```

| 配置项 | 值 / 含义 |
|---|---|
| `db_summary_path` / `schema_path` | 上一阶段两份产物（表卡片 / 列级 schema） |
| `questions_path` / `databases_dir` | BIRD 问题与库文件（`<db>/<db>.sqlite`） |
| `templates.simple_rounds` / `complex_rounds` | 2 / 3，两类模板各自允许的生成轮数 |
| `template_llm.model` | `deepseek-chat`（**必须非推理模型**：`.env` 的 `deepseek-flash` 会把预算全花在 reasoning 上、返回空内容） |
| `template_llm.max_tokens` / `timeout` | 2000 / 180 s |
| `jev.model` / `timeout` / `retries` | `typesafe-ai/jev`；45 s；2 次（端点 `JEV_API_BASE`、密钥 `AI_GATEWAY_API_KEY`） |
| `fill.noul_threshold` / `max_rows` | 0.5（阳性阈值）/ 1000（执行校验最多读 1000 行） |

## 6. 实跑（BIRD dev 前 24 题，全部在 `california_schools`）

| 指标 | 值 |
|---|---|
| 模板生成 | **47 / 48**（`qid 18` 的 simple 两次都返回非 `{"template": ...}` 结构，记 `template_failed`） |
| 填满并渲染出 SQL | **47 / 47**（无 `unresolved_slots`） |
| 能执行 | **47 / 47（100%）** |
| 返回 ≥ 1 行 | 38 / 47；按 best 候选计 **19 / 24** |
| simple / complex 生成成功 | 23 / 24、24 / 24 |
| complex 真的用上高级构造 | **0 / 24**（没有 CTE、没有窗口函数） |
| 每槽阳性候选 | 列槽均值 **4.2**（245 个槽里 24 个无阳性 → 走兜底）；表槽均值 2.3 |
| 每模板列槽数 | 均值 5.2，最多 8 |
| JEV 问题数 | 每候选**中位 306**、均值 320、最多 552 |
| 单题耗时 | 中位 **10.7 s**，均值 14.7 s，最慢 56 s |

加 evidence 后 measure 的绑定直接对上定义式：`qid 0` 的两个候选都产出
`MAX("Free Meal Count (K-12)" / "Enrollment (K-12)")`，join 键为 `frpm.CDSCode = schools.CDSCode`：

```sql
SELECT MAX(("t1"."Free Meal Count (K-12)" / "t1"."Enrollment (K-12)")) AS "max_eligible_free_rate"
FROM "frpm" AS "t1" INNER JOIN "schools" AS "t2" ON ("t1"."CDSCode" = "t2"."CDSCode")
WHERE ("t2"."County" = 'Alameda County');
```

（改写前的同一题是 `MAX("County Name" / "Free Meal Count")`——文本列当分子。）

记录格式（`step1_records.jsonl`，每题一条，节选）：

```json
{"question_id": 0, "db_id": "california_schools", "status": "ok", "best": 1,
 "candidates": [
   {"index": 1, "kind": "simple", "status": "ok", "rounds": 1,
    "template": {"type": "Script", "stmts": ["..."]},
    "values": {"8": "Alameda County"},
    "tables": {"selected": {"3": "frpm", "4": "schools"}, "num_questions": 6,
               "slots": {"3": {"positives": [{"table": "frpm", "noul": 0.94},
                                             {"table": "schools", "noul": 0.82}]}}},
    "columns": {"selected": {"1": {"table": "frpm", "column": "Free Meal Count (K-12)", "noul": 0.94},
                             "7": {"table": "schools", "column": "County", "noul": 0.32, "fallback": true}},
                "slots": {"1": {"positives": ["..."]}}},
    "sql": "SELECT MAX(...) ...;",
    "exec": {"ok": true, "rows": 1, "truncated": false}}]}
```

## 7. 已知问题

1. **列绑定仍有系统性错误**：`qid 11`（"total enrollment over 500"）的两个候选分别填成
   `Charter + Magnet > 500`（simple）与 `Magnet + Magnet > 500`（complex）——两个 MEASURE 槽都在算术
   表达式里，阳性候选里 `Magnet` 分数更高就被选中。说明"这列是否适用于该槽"这种单槽判定缺少
   "与问题里那个数字/名词对齐"的信号。
2. **FROM 里逗号与 JOIN 混用 / 笛卡尔积**：24 题里 3 个候选出现隐式连接——`qid 2` 的 complex 是
   `FROM "schools" AS "t1", "frpm" AS "t2" WHERE ...`（无 join 条件）；`qid 21` 的两个候选是
   `FROM a, b INNER JOIN c ON ...`。语法合法、能执行，但语义上会漏过滤或放大结果集。
3. **complex 没有真的变复杂**：`complex_rounds=3` 只在校验失败时重试，没有"加深"指令，24 个 complex
   模板里 0 个用了 CTE / 窗口函数。要让两类拉开差距，得在 complex 的 prompt 里要求"存在多种写法时
   必须使用至少一个高级构造"，或加复杂度下限校验。
4. **单槽判定的上限**：245 个列槽里 24 个没有阳性候选（阈值 0.5），只能靠 `best` 兜底，说明
   "这列是否适用于该槽"在信息不足时会整体偏低；第 3 步的"候选间对比"应能缓解。
5. **相关子查询的 `ORDER BY` 是潜在失败点**：把外层表列写进子查询的 `ORDER BY` 会被 SQLite 拒绝
   （`no such column`）。本轮 24 题没触发，但它取决于模板形状，不是反解析的问题。
6. **两个已修的反解析 bug**（早期轮次出现、本轮不再出现）：① 同一 FROM 内重复别名
   （`FROM frpm AS t1, schools AS t1` → `ambiguous column name`）；② 链式 join 缺括号
   （`a JOIN b JOIN c ON x ON y` → 语法错误）。现在分别做别名去重与两侧加括号。
7. 未做：结果集正确率（EX）、多候选投票、按意图检索示例（第 2 步）。

## 8. 与下一步的关系

本步回答"零样本下模板能不能生成、槽能不能按 slot 填、SQL 能不能拼出来"。加 evidence 后 measure
的绑定明显改善（`qid 0` 直接产出 `Free Meal Count / Enrollment`）；第 2 步加示例检索，
第 3 步把列绑定从"单槽是否适用"改成"该槽在这些候选里选哪个"的对比式判定。

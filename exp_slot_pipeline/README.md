# exp_slot_pipeline — 消融第一步：LLM 产 slot-AST 模板 → JEV 填槽 → SQL

零样本（无示例）流水线，每题一个 LLM 调用产出 **2 个 schema 无关的 slot-AST 模板**，
再用 JEV 的 `noul` 判定填表槽（表级摘要）与列槽（列级 schema），最后确定性反解析成 SQL。

```
问题 + evidence + 表级摘要（无列清单）
        │
        ├─ LLM（1 次，零样本）────────────► 2 × {template: slot-AST, values: {sid: 值}}
        │                                        └─ 值槽数据被抽出隔离，模板保持无 schema/无数据
        ├─ JEV noul（每题 1 次，表级摘要）───► 表槽：每库每个候选表一个概率 → 取前 n
        ├─ JEV noul（按表，列级 schema）────► 列槽：每列一个概率 → 角色感知贪心绑定
        └─ 确定性反解析 ───────────────────► SQLite 语句 + 只读执行校验（能否跑、几行）
```

## 运行

```powershell
cd D:\JEV\exp_slot_pipeline
python scripts\run_step1.py --config configs\step1_bird_dev.yaml --limit 5
python scripts\run_step1.py --config configs\step1_bird_dev.yaml --limit 12 --offset 3 --resume
```

产物：

| 文件 | 内容 |
|---|---|
| `outputs/runs/<run>/step1_records.jsonl` | 每题一条：2 个候选的模板、表/列 noul 分数、填好的 AST、SQL、执行结果 |
| `outputs/runs/<run>/step1_prompts.jsonl` | 该题模板生成用的完整 prompt（本阶段产物到 prompt 一级也要能查） |

## 依赖的两个上一阶段产物

| 产物 | 由谁生成 | 用途 |
|---|---|---|
| `exp_db_summary/outputs/db_summary/*.json` | `exp_db_summary` 第一步 | 表卡片（LLM 摘要 + 行数 + 外键邻居），给 LLM 与 JEV 表槽 |
| `exp_db_summary/outputs/schema/*.json` | 同上 | 列级 schema（列名/类型/样例/主键），给 JEV 列槽 |

## 目录

```
configs/step1_bird_dev.yaml   一个实验一个配置（数据路径 / 模板数 / 两个模型 / 执行上限）
slotpipe/config.py            配置与 .env 加载
slotpipe/llm.py               OpenAI 兼容 chat + JEV 客户端（都带重试）
slotpipe/template.py          prompt、输出契约、结构校验（拒绝任何真实表名/列名/内联数据）
slotpipe/fill.py              表槽/列槽的 noul 请求、角色过滤、作用域感知的别名绑定
slotpipe/render.py            AST → SQLite（保守加括号，标识符一律加引号）
slotpipe/pipeline.py          单题编排 + 只读执行校验 + 候选选择
scripts/run_step1.py          批量运行（--limit / --offset / --resume）
docs/01-step1.md              设计、契约、校验、复现、已知问题
```

## 本次实跑（BIRD dev 前 24 题）

| 指标 | 值 |
|---|---|
| 模板生成 | 47 / 48（simple 1 次失败） |
| 填满并渲染 | 47 / 47（无 unresolved） |
| 能执行 | **47 / 47** |
| 返回 ≥1 行 | 38 / 47 |
| 每槽阳性候选 | 列槽均值 4.2；表槽均值 2.3 |
| 单题耗时 | 中位 10.7 s |

加 evidence 后 measure 绑定直接对上定义式（`qid 0` 产出 `MAX(Free Meal Count (K-12) / Enrollment (K-12))`）。
complex 模板 0/24 用上 CTE 或窗口函数——`complex_rounds` 只在失败时重试，没有"加深"指令。

## 两个必须知道的坑

1. **模板生成不能用推理模型**：`.env` 默认的 `deepseek-flash` 在这个 prompt 上把 4000 token 预算
   全花在 reasoning 上、`content` 为空（`finish_reason=length`）。配置里显式指定
   `template: deepseek-chat` 即可，3 秒返回 JSON。
2. **JEV 网关偶发连接超时**（单次 20 s 上下），客户端重试 3 次；同题两个模板共享表级判定，
   列级判定按 `(表, 角色集合)` 复用，避免重复付费。

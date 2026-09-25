# ADR 0004: 预授权用独立 hold 表，不进 append-only 账本

状态：Accepted（2026-09-25）

## 背景

`available_credits` 原先只看账本余额，启动中的 workspace 不占额度：余额 60 的用户
可以并发启动两台 GPU，把余额花成负数。需要在 provision 之前把这笔钱"圈住"。

问题不是"要不要预授权"（要），而是**预授权这条事实记在哪里**。本项目的
`CreditLedger` 是 append-only + `idempotency_key` 的账本，只记真实消费；而 hold
天生是"记了又可能撤销"的可变状态。

## 候选方案对比（六维）

参考资料只列本轮**实际读到**的内容：Stripe《Place a hold on a payment method》
文档、TigerBeetle 仓库 README 与 LICENSE、GitHub 检索到的 Formance / blnk 项目简介
（后两者**未读其文档**，License 未在源处核实）。

| 维度 | A 复用外部账本（TigerBeetle / Formance Ledger） | B 账本内写合成 hold 条目 | C workspace 加 `reserved_credits` 列 | D 独立 `credit_holds` 表（选定） |
|---|---|---|---|---|
| 功能匹配度 | 强：TigerBeetle README 显示账户原生带 `debits_pending`/`debits_posted`/`credits_pending`/`credits_posted`，pending↔posted 是数据库一等公民；Formance 简介称提供 double-entry ledger | 中：账本能表达"撤销"但不能表达"未完成"，release 要写反向条目，`balance` 从此分不清"没花"与"退了" | 中：够用但语义错位，workspace 是运行时对象，不是钱的对象 | 强：hold 的四种结局（pending/captured/released/过期）各有一列，Stripe 文档的 auth-capture 形状可逐条对上 |
| License 兼容性 | TigerBeetle LICENSE 已核实为 Apache-2.0；Formance 未核实 | 无第三方，无冲突 | 无第三方，无冲突 | 无第三方，无冲突 |
| 维护活跃度 | 不适用（不引入即无维护面）；引入则需跟对方发布节奏 | 全部自己维护 | 全部自己维护 | 全部自己维护，但面积≈3 个方法 + 1 张表 |
| 安全风险 | 高：新进程/新协议面 + 双写权威源（钱同时存在于两个系统），一致性要自己兜 | 中：反向条目一旦漏写就是静默的余额错，且没有唯一约束可加 | 中：与 workspace 删除/迁移耦合；改列要走 batch migration，SQLite 侧代价高 | 低：与账本同库同事务，`idempotency_key` 唯一约束 + pending 部分唯一索引可直接由 DB 强制 |
| 代码质量 | 与本仓 SQLAlchemy/SQLite 开发档模型不兼容（TigerBeetle 是独立数据库，非库） | 差：把"审计日志"变成"状态表"，破坏账本 append-only 这条最值钱的不变量 | 差：可变计数器漂了没人能对账 | 好：账本保持纯净，可变性被隔离到一张表，且能被 PG 档常驻用例逐条核住 |
| 适配成本 | 最高：替换整个计费子系统 | 低但语义错 | 低但语义错 | 低：一次迁移 + `reserve/capture/release` + worker 周期扫描 |

结论：A 的能力确实比自造强，但它解决的是"跨系统资金流转的分布式账本"，我们的
问题域是单库内的额度门禁——引入它等于为了一个 hold 换掉整个子系统，不适配。
B/C 的省事是以破坏账本不变量或语义错位为代价。**选 D**，并借鉴被核实过的两家
设计里对本问题真正适用的两点：pending/posted 分离（TigerBeetle）、部分 capture
后余量自动释放 + 过期即取消（Stripe）。

## 决策

1. `BillingAccount(subject_type, subject_id)` 唯一：把原先散在 `user_id` /
   `organization_id` 两个 FK 上的"谁付钱"收敛成一行，作为**预授权的串行化根**
   （算可用额前先 `FOR UPDATE` 这一行）。
2. `CreditHold` 独立表，`status ∈ {pending, captured, released}`，
   `idempotency_key = "hold:{workspace_id}"` 唯一，并加部分唯一索引
   `uq_holds_pending_per_workspace`（同一 workspace 至多一个 pending hold）。
3. **hold 不写进 `CreditLedger`**。可用额 = 账本毛余额 − pending hold 合计；
   capture 时把对应 usage 条目的 `idempotency_key` 反向记在
   `hold.ledger_usage_key` 上，形成可核验的关联，而账本侧零改动。
4. capture 是**无条件**的：即使运行段不足 1 秒、没有产生 usage 条目，也要把
   pending 收口成 captured（`captured_amount=0`），否则该 workspace 的后续
   启动会被自己的旧 hold 挡住。
5. 泄漏兜底靠 `expires_at`（`billing_hold_ttl_minutes`，默认 60）+ worker 周期扫描
   `release_expired_holds()`；RUNNING 的 workspace 不扫，它的 hold 由结算收口。

## 后果

- SQLite 下 `FOR UPDATE` 是 no-op（见 ADR 0005），因此第 1 条的锁在开发库上不提供
  任何保证；"一个 workspace 只圈一次"由部分唯一索引兜住，"同一余额不被并发花两次"
  **只在 PostgreSQL 档被验证**（`tests/test_postgres_concurrency.py` 第 7 组）。
- 余额语义变成"毛余额 − pending"，任何新的额度消费方都必须走 `available_credits`，
  不能再自己 `sum(ledger)`。
- 账本条目数不再等于"发生过的事务数"：一次失败启动在账本里没有任何痕迹，只在
  `credit_holds` 留一条 released。审计要查"圈过又退回"必须看 hold 表。
- 过期释放的偏差方向是**少报可用额**（宁可少花），不会多扣钱、不会污染历史。

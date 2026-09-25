# ADR 0006: 生命周期入队冲突返回 409，而不是把伪成功返回给客户端

状态：Accepted（2026-09-25）

## 背景

`POST /api/workspaces/{id}/start`、`POST …/stop` 与 `DELETE /api/workspaces/{id}`
都把 durable operation 入队，但**丢掉了返回值**。当同一 workspace 已有 active
operation 时，部分唯一索引 `uq_ops_active_per_workspace` 会拒绝对方的插入，
`enqueue_operation` 捕获 `IntegrityError` 后返回 `None`
（`app/services/worker.py`）—— 路由却照常返回 200/204。

后果：客户端看到"停止成功"，但库里既没有 operation，workspace 永远停在
RUNNING 并继续计费；重试仍然"成功"。这是一条静默失败路径，且方向是**假绿**。

`start` 比 stop/delete 更糟一步：它在入队**之前**就把状态写成 `QUEUED` 并提交，
所以冲突时不仅谎报受理，还留下一个没有任何队列条目的 QUEUED。缺陷复现读数就是
变异对照跑出来的那条响应：`status_code=200`、`status="queued"`、而 operation
表里只有那个挡路的 PROVISION 行。

## 候选方案（六维简评）

| 维度 | A 路由内阻塞等待前一个 operation 结束 | B 冲突即 409（选定） | C 让 enqueue 永不返回 None（抢占/覆盖旧 operation） |
|---|---|---|---|
| 功能匹配度 | 客户端体验最"顺" | 与"durable operation 同 workspace 串行"的既有设计意图一致 | 需要"取消执行中 operation"的语义，本仓目前不存在 |
| License 兼容性 | 不适用（无新增依赖） | 不适用 | 不适用 |
| 维护活跃度 | 不适用 | 不适用 | 不适用 |
| 安全风险 | 请求线程被 worker 租约量级（`LEASE_SECONDS`）阻塞，SQLite 单写者下等待可能自锁 → 把队列问题升级成可用性问题的入口 | 低：无新状态、无新线程 | 高：覆盖他人 in-flight 操作会破坏 fencing token 的推理（旧 worker 仍能写） |
| 代码质量 | HTTP 层复刻队列语义，重复实现 | 两行判断 + 一条常驻用例 | 需引入"取消"状态机与补偿清理 |
| 适配成本 | 中（要处理超时/取消） | 低 | 高 |

结论：选 **B**。A/C 的代价都是为"看起来更省事"引入新的并发面。

## 决策

1. 三个路由（start / stop / delete）把 `enqueue` 的 `None` 视为冲突：
   `raise HTTPException(409, "…有生命周期操作正在执行，请稍后重试…")`。
2. **先入队、后改状态**：`start` 不再先把 workspace 写成 `QUEUED`。入队失败时
   服务端不应留下任何"已受理"的痕迹（状态、operation 行都不动）。
3. 409 只在"确实有 active operation 挡着"时出现，不用它兜其他错误：入队前
   的 404（workspace 不存在）与鉴权路径不变。
4. 常驻用例 `test_lifecycle_conflict_is_reported_instead_of_faking_success`
   同时钉四件事：start 冲突期 409 **且状态没被翻成 queued**、stop/delete 409、
   冲突期**库里不新增 operation 行**、冲突解除后同一个 DELETE 返回 204 且真正
   tombstone（防"改成 409 就完事"的半修）。
   该用例的两条新断言已按变异对照验过牙：把 `start` 路由改回旧写序即红
   （读数 200 + `status="queued"`），恢复后绿。

## 后果

- API.md「工作区」小节已列出该 409 语义与"入队失败即未受理"的口径；前端按状态
  渲染操作，409 落到既有错误提示路径，无需新交互。
- 调用方从此必须处理"稍后重试"，而不是假设 2xx 即已受理。
- 该规则同样适用于将来新增的生命周期操作类型：任何 `enqueue` 返回 `None`
  都不是成功。

调研说明：这是本仓两处路由的行为修正，影响范围明确（属目标允许的跳过外部检索
情形），故未做生态方案检索；判定依据全部来自本仓代码路径与常驻用例。

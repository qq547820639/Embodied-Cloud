# ARCHITECTURE — EmbodiedCloud

> 版本：0.7.0（2026-09-26）。本文是现行实现架构；历史讨论见 `docs/IMPLEMENTATION_PLAN.md`、`docs/K8S_PRODUCTION.md`、`docs/adr/*`。

## 1. 总体分层

```
Browser (Dashboard / code-server / WebRTC viewer)
        │
        ▼
┌─────────────────────────────────────────────┐
│ Control Plane (FastAPI, app/)               │
│  routers: auth, templates, workspaces,      │
│           gpus, usage(+ledger), streaming,  │
│           courses(+labs/assignments), edge, │
│           deployments                       │
│  services: orchestrator, worker, scheduler, │
│            ledger, billing, warmpool,       │
│            streaming, course, deployment,   │
│            edge, artifact_store, robot      │
│  db: SQLAlchemy 2.0 + Alembic               │
└───────────────┬─────────────────────────────┘
                │ WorkspaceProvider (provision/start/stop/destroy/inspect/logs/
                │   health/reconcile/wait_ready/rotate_credentials/
                │   supports_credential_rotation/pull_artifact)
        ┌───────┼───────────────┬───────────────┐
        ▼       ▼               ▼               ▼
 MockProvider  DockerProvider  K8sProvider    (SimulationProvider 未来)
 (本机/CI)     (单机 GPU)      (生产多机)
```

- `app/deps.py` 是 composition root：所有应用级单例（settings/engine/SessionFactory/认证依赖/服务对象）集中装配，router 不自行构造第二套 DI。
- 组合根注入链：`deps.py → routers（薄壳）→ services（业务）→ providers（runtime 适配）`。

## 2. Workspace 生命周期状态机

```
CREATED → QUEUED → PROVISIONING → RUNNING → STOPPING → STOPPED
           │          │              │                        │
           └──────────┴──────────────┴── FAILED ←── 任何异常/就绪超时
DELETED（soft delete tombstone：destroy 可从 RUNNING/STOPPED/FAILED 直达，
        行保留 deleted_at 供计费/审计/部署历史追溯）
```

约束：
- 状态持久化于 DB；生命周期操作（PROVISION/START/STOP/DESTROY/RECONCILE）落库为
  `workspace_operations`，由 DB-backed worker 执行（lease + fencing token + heartbeat，
  重启后 PENDING/RETRYING 不丢）。
- 启动恢复走 `reconcile_all()`：基于 runtime 事实收敛（DB RUNNING + runtime ALIVE → adopt；
  RUNNING + MISSING → 结算置 FAILED 并归还 GPU；PROVISIONING + MISSING → 重新入队 PROVISION；
  QUEUED/CREATED → 重新入队；mock/UNKNOWN → 保守不动）。
- RUNNING 期间的 stop/destroy 结算 GPU 秒数 → 写入 CreditLedger（USAGE，幂等）。
- provision 补偿式事务：外部副作用失败 → `provider.destroy` 补偿 + `scheduler.release` 归还，
  绝不残留孤儿容器/Pod/GPU。

## 3. GPU Scheduler

- `gpu_allocations` 表（`gpu_id`/`workspace_id` 唯一索引）实现原子分配：
  每轮只 `SELECT ... FOR UPDATE SKIP LOCKED ... LIMIT 1` 锁一张候选（PostgreSQL 生效），
  取不到即「被别人持着」→ 有界重试；唯一约束兜底并发插入（SQLite 单写者）。
  行锁语义只在 `make test-pg` 档验证——SQLite 方言把 `FOR UPDATE` 整个丢弃（ADR 0005）。
- 分配顺序：AVAILABLE（显存满足模板需求，含 16MiB 厂商预留容差）→ ALLOCATED；
  释放：stop/destroy → AVAILABLE。
- UNHEALTHY 不参与调度；DRAINING 不再分配新 workspace。
- **候选排序 = 分配策略，且它是量过、被钉住的**：默认 best-fit（`memory_total asc`，
  先用刚好够用的卡，把大卡留给大任务）。`make policy-bench` 用同一个 `allocate()`、
  同一份合成工作负载（8/16/24/48 GiB 各一张 × 两台机，负载合计恰好等于池子容量 192 GiB）
  换四种排序实测，本轮读数：

  | 策略 | 接得下 | 拒 | 48 GiB 接得下 | 浪费率(占用/需求) |
  |---|---|---|---|---|
  | best_fit（现产） | 8/8 | 0 | 2 | 1.00 |
  | arrival（按入库序） | 6/8 | 2 | 0 | 1.75 |
  | pack_host（先填满一台机） | 6/8 | 2 | 0 | 1.75 |
  | worst_fit（先用最大的卡） | 4/8 | 4 | 0 | 3.00 |

  同一份硬件上，排序换成 worst-fit 就少接 4 个工作区（吞吐 -50%），且两张 48 GiB 的卡
  全部被小任务吃掉。`tests/test_scheduler_policy.py` 把这张表钉成常驻判据：
  按表达式直比生产排序（不经过实测台的认档函数），并要求其余三档**都接不满**——
  改向即红，实测台失去区分力也红。多卡协同放置不在本分配器的讨论范围内：
  一个 workspace 至多绑一张卡（`uq_gpus_workspace`），所以"按 host 打包"这一维今天
  没有可观测的后果（表里 pack_host 只因为它顺手浪费了小卡才落后）。
- 孤儿回收 `recover_stuck_gpu_allocations`：只释放「无 active operation 且非
  PROVISIONING/RUNNING/STOPPING」的 workspace 的绑定（防 PROVISIONING 竞态一卡双跑）。
- 第一阶段一 Workspace 一整块 GPU，不做 MIG。

## 4. Provider 抽象

```python
class WorkspaceProvider(Protocol):
    name: str
    def health(self) -> tuple[bool, str]: ...
    def provision(self, workspace, template, workspace_dir, reservation=None) -> ProvisionResult: ...
    def start(self, workspace) -> None: ...
    def stop(self, workspace) -> None: ...
    def destroy(self, workspace) -> None: ...
    def inspect(self, workspace) -> dict: ...
    def logs(self, workspace, tail: int = 200) -> str: ...
    def reconcile(self, workspace) -> RuntimeState: ...
    def wait_ready(self, workspace, template, timeout_seconds: int = 120) -> bool: ...
    def rotate_credentials(self, workspace, credentials: dict) -> bool: ...
    def pull_artifact(self, workspace, source_path: str) -> Path: ...
    @property
    def supports_credential_rotation(self) -> bool: ...
```

- `ResourceReservation` 是 GpuScheduler 产出的唯一 GPU 决策来源；Provider 只执行绑定，
  禁止自带 allocator（Docker `--gpus device=N` / K8s `nvidia.com/gpu` + nodeSelector）。
- `wait_ready` 是 typed readiness gate：runtime 存在 + 健康 + IDE 可达 +
  TemplateVersion.healthcheck 真实执行；未就绪 → 补偿回滚 → FAILED。
- 业务 API 不直接绑定 Docker；K8s 模式通过 kubernetes python client 实现同一接口
  （offline 测试注入 fake client/model layer，零 SDK 依赖）。

## 5. 数据模型（概览）

见 `app/models.py`。核心表：

- users / organizations / user_sessions
- templates / template_versions（identity 与不可变版本分离，current_version_id 指针）
- workspaces / workspace_operations（durable operations）/ gpu_hosts / gpus / gpu_allocations
- credit_ledger（不可变账本）/ billing_accounts（计费主体行，预授权锁根）/ credit_holds（启动预授权）
- streaming_sessions
- courses / course_members / labs / assignments / submissions
- artifacts / deployments
- edge_agents / telemetry_events

## 6. 计费

- Ledger 不可变 append-only；`idempotency_key` 唯一索引防重复扣款。
- GPU 计费单位：实际运行秒数（RUNNING 起止差，stop/destroy 时结算；幂等键
  `usage:{workspace_id}:{started_at_iso}`，同一运行段只结算一次）。
- balance = SUM(amount)；不维护单一 mutable balance。
- **可用额度 = 个人+组织账本毛余额 − pending hold 合计**；hold 是独立可变表，
  不进账本（ADR 0004）。`reserve_launch` 在 provision 前圈住最低额度（幂等键
  `hold:{workspace_id}`，同一 workspace 至多一个 pending），结算 `capture_hold`
  转正并记 `ledger_usage_key`，失败/销毁 `release_hold` 退回。
- BillingPolicy：launch 前门禁（可用额度 / course 配额），admin 与 instructor 豁免；
  运行中由 worker 周期任务 `monitor_runtime_quotas` 透支优雅停止，另有
  `release_expired_holds` 回收超时 pending（控制面崩溃残留，RUNNING 段不回收）。

## 7. 安全边界

见 `docs/SECURITY.md`。要点：Workspace 容器无 docker.sock、无 host root、resource limits、
owner/org 隔离（越权一律 404）、secret 不入日志、生产 fail-closed 启动校验、
TLS 网关。

## 8. 可观测性

- `/metrics` Prometheus 格式（workspace_launch_total/failed/seconds、workspace_running、
  gpu_allocated、gpu_seconds、template_launch_total/failure_total、stream_session_total/
  failure_total、warm_pool_ready/claim_total/claim_failed）
- JSON 结构化日志 + request_id 中间件（X-Request-Id 响应头）
- 前端 `/api/health` 版本 pill + provider 状态。

## 9. Streaming 架构决策（ADR 0001）

- 不重新发明视频协议；优先 Isaac Sim 官方 WebRTC 路径。
- 控制面只管理 StreamingSession 状态机（starting→ready→connected→disconnected→ready / failed）；
  媒体面由 workspace 内 simulator 提供。
- 固定端口策略（49100/TCP + 47998/UDP，单实例）为保守 MVP；多实例需 gateway（未来）。

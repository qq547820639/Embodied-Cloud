# ARCHITECTURE — EmbodiedCloud

> 版本：0.2.0（2026-08-12）。本文是现行实现架构；历史讨论见 `docs/IMPLEMENTATION_PLAN.md`、`docs/K8S_PRODUCTION.md`。

## 1. 总体分层

```
Browser (Dashboard / code-server / WebRTC viewer)
        │
        ▼
┌─────────────────────────────────────────────┐
│ Control Plane (FastAPI)                     │
│  routers: auth, users, orgs, templates,     │
│           workspaces, gpus, usage, ledger,  │
│           streaming, courses, deployments,  │
│           metrics                           │
│  services: orchestrator, scheduler,         │
│            ledger, streaming, warmpool,     │
│            course, edge                     │
│  db: SQLAlchemy 2.0 + Alembic               │
└───────────────┬─────────────────────────────┘
                │ WorkspaceProvider (create/start/stop/delete/inspect/logs/health)
        ┌───────┼───────────────┬───────────────┐
        ▼       ▼               ▼               ▼
 MockProvider  DockerProvider  K8sProvider    (SimulationProvider 未来)
 (本机/CI)     (单机 GPU)      (生产多机)
```

## 2. Workspace 生命周期状态机

```
CREATED → QUEUED → PROVISIONING → RUNNING → STOPPING → STOPPED
           │          │              │                        │
           │          │              └── FAILED               │
           │          └── FAILED ←─── 任何 provisioning 异常  │
           └────────── FAILED                                 │
DELETED（仅从终态 STOPPED/FAILED 删除）
```

约束：
- 状态持久化于 DB；crash recovery 在启动时把悬置 `PROVISIONING/QUEUED` 标记为 FAILED 并归还 GPU。
- RUNNING 期间的 stop 结算 GPU 秒数 → 写入 CreditLedger（USAGE，幂等）。

## 3. GPU Scheduler

- `gpu_allocations` 表（workspace_id 唯一索引）实现原子分配：`SELECT ... FOR UPDATE` + 唯一约束兜底。
- 分配顺序：AVAILABLE → ALLOCATED；释放：STOPPED/DELETED → AVAILABLE。
- UNHEALTHY 不参与调度；DRAINING 不再分配新 workspace。
- 第一阶段一 Workspace 一整块 GPU，不做 MIG。

## 4. Provider 抽象

```python
class WorkspaceProvider(Protocol):
    name: str
    def health(self) -> tuple[bool, str]: ...
    def provision(self, workspace, template, workspace_dir) -> ProvisionResult: ...
    def stop(self, workspace) -> None: ...
    def destroy(self, workspace) -> None: ...
    def inspect(self, workspace) -> dict: ...
    def logs(self, workspace, tail: int = 200) -> str: ...
```

业务 API 不直接绑定 Docker；K8s 模式通过 kubernetes python client 实现同一接口。

## 5. 数据模型（概览）

见 `app/models.py`。核心表：
- users / organizations / roles / user_sessions
- templates（registry 字段）
- workspaces / gpu_hosts / gpus / gpu_allocations
- credit_ledger（不可变账本）
- streaming_sessions
- courses / course_members / labs / assignments / submissions
- deployments / artifacts
- edge_agents / robot_devices / telemetry_events

## 6. 计费

- Ledger 不可变 append-only；`idempotency_key` 唯一索引防重复扣款。
- GPU 计费单位：实际运行秒数（RUNNING 起止差，stop/delete 时结算）。
- balance = SUM(amount)；不维护单一 mutable balance。

## 7. 安全边界

见 `docs/SECURITY.md`。要点：Workspace 容器无 docker.sock、无 host root、resource limits、owner/org 隔离、secret 不入日志、TLS 网关。

## 8. 可观测性

- `/metrics` Prometheus 格式（workspace_launch_total/failed/seconds、workspace_running、gpu_allocated/gpu_seconds、template_launch_total/failure_total、stream_session_total/failure_total）
- JSON 结构化日志 + request_id 中间件（user_id/workspace_id/template_id/host_id/gpu_id）

## 9. Streaming 架构决策（ADR 0001）

- 不重新发明视频协议；优先 Isaac Sim 官方 WebRTC 路径。
- 控制面只管理 StreamingSession 状态机；媒体面由 workspace 内 simulator 提供。
- 固定端口策略（49100/TCP + 47998/UDP，单实例）为保守 MVP；多实例需 gateway（未来）。

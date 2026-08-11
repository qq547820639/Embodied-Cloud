# EmbodiedCloud v0.1 架构

```text
Browser
  │
  ├── Dashboard / REST API
  │       │
  │       ▼
  │   FastAPI Control Plane
  │     ├── Template Catalog
  │     ├── Workspace State Machine
  │     ├── IDE Port Allocator
  │     ├── Usage Accounting
  │     └── Provider Adapter
  │             │
  │      ┌──────┴─────────┐
  │      │                │
  │    Mock           Docker GPU Host
  │                         │
  │                         ▼
  │            Isaac Lab Workspace Container
  │            ├── code-server (browser IDE)
  │            ├── Isaac Lab 3.0 Beta 2
  │            ├── Isaac Sim 6.0.1
  │            └── Isaac Lab process -> optional WebRTC
  │
  └────────────────────────────────────
```

## 状态机

`queued -> provisioning -> running -> stopping -> stopped`

任何 provision/stop 异常进入 `failed`。重新 start 会从 `queued` 再尝试。

## v0.1 的关键工程约束

### 一工作区一 GPU

Streaming/可视化不仅占 CUDA，还依赖 NVENC；先保证复现性和故障隔离，不做 GPU 超售。后续再评估 MIG / time-slicing。

### Streaming 不由 entrypoint 预启动第二个 Isaac Sim

Workspace 只常驻 code-server。用户在 IDE 终端运行模板命令时，**该 Isaac Lab 进程**读取 `LIVESTREAM=1` 和 `PUBLIC_IP` 并启动 WebRTC。这样避免“后台一个 Isaac Sim + 用户命令再起一个 Isaac Sim”的双实例资源冲突。

### v0.1 每宿主只允许 1 个 Streaming Workspace

Isaac Lab 3.0 Beta 2 的 public livestream AppLauncher 当前使用 49100/TCP 与 47998/UDP。Docker Provider 因此对 streaming 做单槽位限制；多实例 streaming 是 K8s/session gateway 阶段再解决的独立任务。

## 数据边界

- SQLite：只用于单实例控制面 MVP。
- `/workspace/project`：用户项目持久目录。
- workspace 容器：可丢弃；用户项目目录不随容器销毁。
- `password`：当前仅用于可信单团队 v0.1；生产迁移到短期 token/OIDC。

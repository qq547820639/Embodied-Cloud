# Autonomous Production Hardening Spec

## Why
EmbodiedCloud 需要一个可部署、可测试、可维护、可继续迭代的真实软件产品。当前仓库是功能完整但**缺失核心生产能力的 MVP**：无 Auth/隔离、无 GPU Scheduler、无不可变 Ledger、无 Template Registry、无 Observability、无迁移、无完整 CI、无 K8s Provider、无 Edge Agent。本 spec 将按用户给定的 P0→P2 优先级自主推进到"当前环境可完成的最大工程范围"。

## What Changes
- 建立完整治理文档（PRODUCT_SPEC/ARCHITECTURE/MASTER_PLAN/ACCEPTANCE_GATES/SECURITY/RUNBOOK/OPERATIONS/RELEASE_PROCESS/ADR/CURRENT_STATE）
- 扩展数据模型：User/Organization/Role/Session、GPU inventory、CreditLedger Transaction、Template 完整字段、StreamingSession、Course/Lab、Deployment/Artifact
- 引入 Alembic 迁移（替代 create_all），保留 SQLite 本地开发、PostgreSQL 生产
- Auth（API key + session，OIDC-ready 接口）+ 资源隔离（owner/org 校验）
- GPU Scheduler：原子分配、防重复、crash recovery、健康状态
- Provider 抽象完备化（inspect/logs/start + K8sWorkspaceProvider）
- 不可变 Credit Ledger + 幂等 Usage 记账
- Template Registry（slug/version/entrypoints/healthcheck/gpu_requirement，禁 latest）
- Streaming 状态机（starting/ready/connected/disconnected/failed）+ 模拟链路测试
- Warm Pool（pool manager + metrics + benchmark harness）
- Course/Assignment 模块
- Edge Agent 模块（register/heartbeat/device_info/download/verify/start/stop/telemetry）+ RobotDriver interface + MockRobotDriver
- Observability：/metrics（Prometheus 格式）+ 结构化日志 + request_id 中间件
- CI 扩展（lint/type/unit/integration/build/artifact）+ release 脚本（checksum/migration notes/分级验证矩阵）
- Makefile 完备（install/dev/lint/typecheck/build/smoke/clean/gpu-preflight/gpu-test/compose-up/compose-down）
- G1–G4 GPU 验收脚本（preflight/gpu_acceptance/isaac_sim_smoke/isaac_lab_cartpole_smoke/franka_smoke），本机标记 PHYSICAL_GPU_VALIDATION_PENDING
- 无物理 GPU/机器人：如实输出 BLOCKED_EXTERNAL_DEPENDENCY，不伪造 PASS

## Impact
- Affected specs: Workspace 生命周期、Usage、Template、Provider、Streaming、Deployment
- Affected code: app/（main/config/models/schemas/seed/db + services 全部）、tests/、Makefile、.github/workflows、deploy/、scripts/、runtime/
- **BREAKING**: Workspace/Template schema 扩展需要迁移；workspace_id 归属校验后，匿名 API 行为改变（需 auth）；`GET /api/usage` 语义改为 ledger-based

## ADDED Requirements

### Requirement: Auth + 资源隔离
系统 SHALL 提供 User/Organization/Role/Session 认证；任何 Workspace 读写 SHALL 校验 owner/org 归属。
#### Scenario: 越权读取被拒
- **WHEN** User A GET /api/workspaces/{UserB的id}
- **THEN** 返回 403/404，且 User B 数据不泄漏

### Requirement: GPU Scheduler
系统 SHALL 维护 GPU inventory 并提供原子分配/释放，支持 crash recovery，不重复分配。
#### Scenario: 并发分配不重复
- **WHEN** 两个 workspace 并发请求分配同一 AVAILABLE GPU
- **THEN** 恰好一个成功，另一个获得不同 GPU 或排队

### Requirement: Immutable Credit Ledger
计费 SHALL 基于不可变 Transaction 记录（RECHARGE/USAGE/PROMOTION/REFUND/ADJUSTMENT），restart-safe、idempotent、auditable。
#### Scenario: 同一 usage 不重复扣款
- **WHEN** 进程在扣款写入后崩溃重启并重放
- **THEN** 该 usage 仅产生一条 USAGE transaction

### Requirement: Template Registry
Template SHALL 包含 slug/version/image/gpu_requirement/entrypoints/outputs/streaming/healthcheck/metadata，禁止引用 mutable latest。

### Requirement: Streaming 状态机
StreamingSession SHALL 维护 starting/ready/connected/disconnected/failed 状态并支持 reconnect 与端口清理。

### Requirement: Observability
应用 SHALL 暴露 Prometheus /metrics（含 workspace_launch_total 等指定指标）与结构化日志（request_id/user_id/workspace_id/template_id/host_id/gpu_id），禁止将 secret 写入日志。

### Requirement: Warm Pool
系统 SHALL 提供基于 Template 的 warm pool（pool manager + metrics + benchmark harness），P50<15s、P95<30s 目标。

### Requirement: Edge Agent
提供 embodied-edge-agent 模块（register/heartbeat/device_info/download_deployment/verify_artifact/start_policy/stop_policy/telemetry）与 RobotDriver interface + MockRobotDriver；无真实机器人时不得宣称 Sim2Real 成功。

### Requirement: Kubernetes Provider
KubernetesWorkspaceProvider SHALL 实现与 WorkspaceProvider 相同的接口（create/start/stop/delete/inspect/logs/health），不可直接绑定 Docker。

## MODIFIED Requirements

### Requirement: Workspace 生命周期
原 QUEUED/PROVISIONING/RUNNING/STOPPING/STOPPED/FAILED 扩展为含 CREATED 与 DELETED，并保证状态持久化与 crash recovery（重启后恢复悬置 PROVISIONING）。
### Requirement: Usage API
原"实时估算"改为基于 ledger 的累计；同时保留展示口径。
### Requirement: CI/CD
原仅 pytest 的 CI 扩展为 lint/type/unit/integration/build/artifact；production 发布依赖 tests green + artifact built + migration validated + smoke passed。

## REMOVED Requirements
无移除；仅标注 **BREAKING** 变更。

## BLOCKED 声明
物理 GPU/Docker/NGC/机器人相关验证一律标记 PHYSICAL_GPU_VALIDATION_PENDING 或 BLOCKED_EXTERNAL_DEPENDENCY，绝不标 PASS。

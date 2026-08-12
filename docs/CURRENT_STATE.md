# CURRENT_STATE — EmbodiedCloud

> 版本：0.2.0（基线）→ 正在推进 v0.2.1 / v0.3.0。
> 更新：2026-08-12（本次为真实 baseline audit 结果，非复制旧文档结论）。
> 环境：macOS（Python 3.12.13 / venv / 无 Docker daemon / 无 NVIDIA GPU / 无 NGC 凭据 / 无真实机器人）。

## 1. 实际执行记录（本次验证）

| 审计项 | 命令 | 结果 |
|---|---|---|
| 仓库状态 | `git status` | CLEAN（main @ e69f67b，与 origin/main 一致，无 tag） |
| 版本 | `pyproject.toml` / CHANGELOG | v0.2.0 |
| 测试 | `.venv/bin/python -m pytest -q` | PASS（**84 passed**） |
| Lint | `ruff check app tests` | PASS（All checks passed） |
| Type | `mypy app` | PASS（36 source files） |
| 迁移 | `tests/test_migrations.py`（含于 84 用例） | PASS |
| 前端 | `app/static/app.js` | 存在非 2xx 无 typed error 问题（见 §2） |
| OpenAPI | `docs/openapi.json` 与真实 app 比对 | 待验证（本次重新生成） |

## 2. 审计发现（VERIFIED 缺陷，本次代码阅读确认）

| # | 缺陷 | 位置 | 影响 | 优先级 |
|---|---|---|---|---|
| B1 | **usage balance bug**：standalone user（无 org、非 admin）`credits_balance` 恒为 0，即使有 CreditLedger 记录 | `app/routers/usage.py:43` | 计费展示错误 | P0 (v0.2.1) |
| B2 | **GPU 双重调度**：`GpuScheduler.allocate()` 已分配 GPU，`DockerProvider.provision()` 又调用 `_choose_gpu()` 独立选 GPU | `app/services/providers/docker.py:84,139` | 控制面分配 A、实际容器跑 B，违反 ONE RESOURCE = ONE SOURCE OF TRUTH | P0 (v0.3.0) |
| B3 | **镜像决策错误**：DockerProvider 使用 `settings.workspace_image` 而非 `template.image` | `docker.py:166` | 模板 A/B 可能跑同一镜像 | P0 (v0.3.0) |
| B4 | **threading.Thread 异步启动**：进程重启丢任务、无 lease/heartbeat、无串行保证 | `app/services/orchestrator.py:60` | 恢复语义脆弱 | P0 |
| B5 | **crash_recovery 停掉所有 RUNNING**：无 runtime inspect/reconcile | `orchestrator.py:181-198` | 控制面重启即杀真实容器 | P0 |
| B6 | **destroy 硬删除**：`db.delete(workspace)`，`WorkspaceStatus.DELETED` 成死代码 | `orchestrator.py:177` | 无 billing/audit/安全追溯 | P1 |
| B7 | **Streaming 生命周期未耦合**：stop/destroy 不调用 `StreamingSessionService.stop` | `orchestrator.py:118-178` | 会话悬挂、端口不释放 | P1 |
| B8 | **Provider Protocol 不完整**：仅 health/provision/stop/destroy；docker/mock 缺 start/inspect/logs/reconcile | `app/services/providers/base.py` | 无法 reconcile | P0 |
| B9 | **K8s 无 GPU 资源声明**：无 `nvidia.com/gpu` limit、无 nodeSelector/affinity | `k8s.py:153-199` | Pod 不保证绑定 GPU | P1 |
| B10 | **前端非 2xx 不抛 typed error**：body 解析后不 throw，调用方拿不到 FastAPI detail | `app/static/app.js:10` | 错误展示不一致 | P1 (v0.2.1) |
| B11 | `Workspace.password` 明文持久化 | `models.py:201`、`orchestrator.py:99` | 凭据长期落库 | P1 |
| B12 | Provision 无补偿回滚：GPU allocate 成功后 provider 失败，仅 `_fail` 释放 GPU，但端口/容器/volume 不清理 | `orchestrator.py:104-107` | 孤儿资源 | P0 |

## 3. 分项状态（按验证结果分类）

### VERIFIED（本环境实际验证通过）
- 控制面全 API 面：auth/templates/workspaces/gpus/usage/ledger/streaming/courses/edge/deployments/metrics/health
- Auth + owner/org 隔离（tests/test_isolation.py）
- GPU Scheduler 原子分配/释放/并发防重（tests/test_scheduler.py）
- 不可变 CreditLedger + 幂等 usage 结算（tests/test_ledger.py）
- Alembic 迁移 up/down（tests/test_migrations.py）
- Template Registry（5 SKU，version locked 声明）
- Streaming 状态机（模拟链路，tests/test_streaming.py）
- Warm Pool manager + benchmark harness（tests/test_warmpool.py）
- Course/Edge/Deployment/Robot(Mock) 流程
- K8s Provider 离线单测（fake client，tests/test_k8s_provider.py）
- Observability：/metrics + 结构化日志 + request_id + 脱敏
- CLI + 静态 UI + G1–G4 验收脚本 + release.sh

### PARTIAL
- DockerProvider：单测完整，**真实 Docker daemon/GPU 未执行**；且存在 B2/B3 双重调度缺陷（v0.3.0 修复）
- Streaming：控制面状态机完整，真实 WebRTC 媒体链路未验证
- OpenAPI：docs/openapi.json 存在，freshness 检查本次加入

### BROKEN
- `GET /api/usage` standalone user balance（B1）→ v0.2.1 修复
- Provider Protocol 契约不完整（B8）→ v0.2.1 修复

### MOCK_ONLY
- GPU inventory（mock-gpu-0001/0002）、浏览器 IDE/Streaming demo 页
- RobotDriver（MockRobotDriver 无物理硬件）

### BLOCKED_EXTERNAL_DEPENDENCY（本环境无法完成，不标 PASS）
- Docker daemon / NVIDIA Container Toolkit → G1；NVIDIA GPU → G2–G4
- NGC 凭据 → workspace 镜像构建
- 真实机器人 → Sim2Real
- PostgreSQL / 真实 K8s 集群 → 生产模式验证

## 4. 当前 Roadmap 状态

| 版本 | 内容 | 状态 |
|---|---|---|
| v0.2.1 | Correctness Hotfix：usage balance / 前端 error handling / Provider Protocol / OpenAPI | 🔄 进行中 |
| v0.3.0 | Single GPU Authority：ResourceReservation、删 `_choose_gpu`、template.image 真实化、一致性测试 | ⏳ 下一步 |
| v0.3.1 | Kubernetes Runtime：GPU inventory adapter、nvidia.com/gpu、RBAC/PVC/Service/NetworkPolicy | ⏳ |
| v0.4.0+ | Cloud Beta / Production / Sim2Real / v1.0.0-rc1 | ⏳ |

## 5. 结论

v0.2.0 基线全部自动验证通过（84 tests / lint / mypy / build / smoke）。但代码审计确认
**P0 正确性缺陷**：GPU 双重调度（B2）、usage balance（B1）、无补偿回滚（B12）、threading 异步
（B4）、无 reconcile（B5/B8）。v0.2.1 + v0.3.0 的目标是消灭这些缺陷并全部用自动测试证明。
硬件验收项（G1–G4、Streaming 媒体面、Sim2Real）持续标记 BLOCKED_EXTERNAL_DEPENDENCY，不伪造 PASS。

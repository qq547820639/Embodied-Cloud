# CURRENT_STATE — EmbodiedCloud

> 版本：0.2.0 → **0.3.0（v0.2.1 + v0.3.0 软件面已完成）**。
> 更新：2026-08-12（第二次审计：全部为实际验证结果）。
> 环境：macOS（Python 3.12.13 / venv / 无 Docker daemon / 无 NVIDIA GPU / 无 NGC 凭据 / 无真实机器人）。

## 1. 本次执行记录（实际验证）

| 审计项 | 命令 | 结果 |
|---|---|---|
| 测试 | `.venv/bin/python -m pytest` | **PASS（132 passed + 2 skipped[k8s_integration]）** |
| Lint | `ruff check app tests` | PASS |
| Type | `mypy app` | PASS（38 source files） |
| 迁移 | `alembic upgrade head`（empty→head 逐级） | PASS（4 个 revision 链） |
| OpenAPI | `make api-docs` + CI freshness | PASS（已重新生成） |
| git | 12 个功能 commit（见 §4） | 工作树 CLEAN |

## 2. 本次修复/新增（VERIFIED）

| # | 内容 | 测试证明 |
|---|---|---|
| F1 | usage balance：standalone user 返回真实 ledger balance（B1） | test_standalone_user_usage_balance_matches_ledger |
| F2 | 前端统一 API wrapper：非 2xx 解析一次 body + typed ApiError（B10） | （静态页，无单测） |
| F3 | Provider Protocol 统一：health/provision/start/stop/destroy/inspect/logs/reconcile + RuntimeState + ResourceReservation（B8） | test_docker_provider / test_k8s_provider |
| F4 | **GPU 单一权威**：GpuScheduler 唯一决策入口，Docker 删除 `_choose_gpu()`，`--gpus device=N` 与 DB GpuAllocation 完全一致（B2） | test_gpu_single_authority.py（5 用例） |
| F5 | **Template image 真实化**：TemplateVersion/workspace.image 快照决定 runtime 镜像（B3） | test_docker_run_uses_template_image / test_template_versions |
| F6 | **Provision 补偿回滚**：任意步骤失败清理全部下游资源（B12） | test_provision_rollback.py（6 用例） |
| F7 | **Durable WorkspaceOperation**：DB-backed worker（lease/heartbeat/过期 reclaim/串行/重试），替换 threading.Thread（B4） | test_worker.py（10 用例） |
| F8 | **Reconciliation**：基于 provider.inspect/reconcile 状态收敛，不再无条件停 RUNNING（B5） | test_reconcile_* |
| F9 | **Streaming 生命周期耦合**：stop/destroy 先终结流会话（B7） | test_streaming_lifecycle.py（4 用例） |
| F10 | **Soft delete tombstone**：destroy 保留行（DELETED+deleted_at），API 默认不返回（§11） | test_workspace_delete_is_soft_tombstone |
| F11 | **Immutable TemplateVersion**：UNIQUE(template_id,version)、seed 不覆盖 released、Workspace 绑定版本（§15） | test_template_versions.py（6 用例） |
| F12 | **BillingPolicy**：launch 前额度/配额门禁（负余额 402、course quota 真实执行）（§17） | test_billing_policy.py（5 用例） |
| F13 | **凭据加密**：Workspace.password Fernet 加密落库，access 解密返回（§20） | test_workspace_credential.py（3 用例） |
| F14 | K8s 部署清单：workspace RBAC + NetworkPolicy（§13）；k8s_integration marker（无集群 skip，不假 PASS）（§14） | pytest -m k8s_integration → 2 skipped |

## 3. 分项状态

### VERIFIED（自动测试证明）
- 132 个测试全绿：identity/isolation、GPU scheduler 并发、ledger 幂等、migration 链、
  provider 契约、GPU 一致性、provision 回滚、operation worker、reconcile、streaming
  生命周期、soft delete、immutable template versions、billing policy、credential 加密

### IMPLEMENTED / PHYSICAL VALIDATION PENDING（软件完成，需真实硬件）
- Docker GPU runtime（G1–G4 验收脚本就绪；无 GPU 主机）→ `GPU_PHYSICAL_VALIDATION_PENDING`
- K8s provider（fake client 单测 + RBAC/NetworkPolicy 清单；无集群）→ `K8S_PHYSICAL_VALIDATION_PENDING`
- Streaming 媒体面（状态机完整；真实 WebRTC 未验证）→ `STREAMING_PHYSICAL_VALIDATION_PENDING`
- Sim2Real / RobotDriver（Mock 实现；无机器人）→ `ROBOT_PHYSICAL_VALIDATION_PENDING`
- Warm pool（manager + benchmark harness；无真实 GPU 预热，不声称 SLA）→ PENDING
- Edge Agent / Deployment checksum 流程（mock 级实现）

### BLOCKED_EXTERNAL_DEPENDENCY
- NGC 凭据 → workspace 镜像构建 / image_digest 回填
- PostgreSQL → 生产模式验证（SQLite 本地已验证）
- 对象存储凭据 → S3-compatible ArtifactStore

## 4. Commit 记录（本次 12 个功能 commit）

```
57e9bdd chore: establish verified baseline and generated API artifacts
6a20e8a fix: return correct credit balance for standalone users
3349b4c fix: unified frontend API wrapper with typed errors
f934a07 refactor: unify WorkspaceProvider runtime contract
fc75dbd fix: make scheduler the single GPU reservation authority
2f34d80 feat: implement idempotent provision rollback
12f3692 feat: add durable workspace operations and reconciliation
3d5e1e7 fix: couple streaming lifecycle to workspace lifecycle
628d460 fix: workspace delete uses soft delete tombstone semantics
b995dd5 feat: immutable template versions
f424d0b feat: billing policy and quota enforcement before workspace launch
094d754 feat: encrypt workspace credentials at rest
```

## 5. 遗留风险 / TECHINAL DEBT
- Python lockfile 未提交（§16 P2）
- workspace 镜像 digest / code-server SHA256 校验待构建流水线（BLOCKED_EXTERNAL）
- Streaming 单实例固定端口仍为保守 MVP（ADR 0001）
- Warm pool claim 模型（PREWARMING/CLAIMING/CLAIMED）为 §18 下一步
- Edge Agent 独立包（packages/embodied-edge-agent）未创建（P2）
- ArtifactStore（Local/S3）未实现（P2）

## 6. 结论

v0.2.1（Correctness Hotfix）与 v0.3.0（Runtime Correctness）软件面完成：
GPU 双重调度已消灭并有 5 项一致性测试证明；provision 补偿回滚、durable
operations、reconciliation、streaming 生命周期、soft delete、immutable
template versions、billing 门禁、凭据加密全部落地并被自动测试覆盖。
132 passed + 2 skipped（k8s 物理验证正确标记 PENDING，不伪造 PASS）。
下一步（P1）：Kubernetes 真实集群验证、warm pool claim 模型、object storage。

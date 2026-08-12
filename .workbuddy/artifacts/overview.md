# EmbodiedCloud v0.2.1 + v0.3.0 — 交付总结

> 日期：2026-08-12 · 分支：main · 版本：0.2.0 → 0.3.0（软件面）
> 仓库：github.com/qq547820639/Embodied-Cloud（工作树 CLEAN，全部提交已落）

## 交付概览

- **TL;DR**：消灭 GPU 双重调度（GpuScheduler 唯一权威，测试证明 DB↔docker 一致），
  并完成 v0.2.1 Correctness Hotfix + v0.3.0 Runtime Correctness 全部软件面。
- **测试**：152 passed + 2 skipped（k8s_integration 无集群正确标记 PENDING）
- **Lint / Typecheck / Migration / OpenAPI**：全绿；5 级迁移链 empty→head 通过
- **提交**：本轮 22 个 commit（功能 10 + 文档 3 + 修复/样式 9），工作树 CLEAN

## 已落地（VERIFIED PASS，全部有测试证明）

| 模块 | 关键变更 |
|---|---|
| GPU 单一权威 | `_choose_gpu()` 删除；typed ResourceReservation；`--gpus device=N` 与 DB GpuAllocation 完全一致（test_gpu_single_authority 5 用例） |
| Provider 契约 | health/provision/start/stop/destroy/inspect/logs/reconcile + RuntimeState 三实现统一 |
| Provision 回滚 | 补偿事务：任意步骤失败清理全部下游资源（6 用例） |
| Durable ops | WorkspaceOperation + DB worker（lease/过期 reclaim/串行/重试），替换 threading.Thread（10 用例） |
| Reconcile | 基于 runtime 事实收敛，不再无条件停 RUNNING；幂等（7 用例） |
| Streaming 耦合 | stop/destroy 先终结流会话 + 释放端口（4 用例） |
| Soft delete | tombstone（DELETED+deleted_at），API 默认不可见，admin 审计 |
| Immutable versions | TemplateVersion：UNIQUE(template_id,version)，seed 不覆盖 released（6 用例） |
| Billing policy | 负余额/课程配额 launch 前门禁（402），结算幂等（5 用例） |
| 凭据加密 | Workspace.password Fernet 落库，access 解密；日志脱敏（3 用例） |
| Warm pool | 真实 claim 状态机 PREWARMING→READY→CLAIMING→CLAIMED，原子抢占并发测试（5 用例） |
| Deployment 校验 | verify 必须真实 download 后执行（防绕过），tampered/mismatch/幂等（5 用例） |
| Object storage | ArtifactStore Protocol + Local 实现 + S3 明确 BLOCKED；path traversal 防护（9 用例） |
| Observability | launch/failed/seconds/gpu_seconds 接入真实业务路径 |
| K8s | GPU limit/nodeSelector 来自 reservation；RBAC/NetworkPolicy 清单；integration marker |
| 前端 | 统一 API wrapper（typed error）；workspace 日志查看 |

## IMPLEMENTED / PHYSICAL VALIDATION PENDING

- `GPU_PHYSICAL_VALIDATION_PENDING`：Docker GPU runtime（G1–G4 脚本就绪，无 GPU 主机）
- `K8S_PHYSICAL_VALIDATION_PENDING`：真实集群验证（pytest -m k8s_integration 正确 skip）
- `STREAMING_PHYSICAL_VALIDATION_PENDING`：真实 WebRTC 媒体面
- `ROBOT_PHYSICAL_VALIDATION_PENDING`：Sim2Real / RobotDriver 真机
- Warm pool SLA（P50<15s/P95<30s）需真实 GPU 实测

## BLOCKED_EXTERNAL_DEPENDENCY

- NGC 凭据 → workspace 镜像构建 + image_digest 回填
- PostgreSQL / 真实 K8s 集群 / S3 凭据 → 生产模式验证
- Python lockfile / SBOM（P2 供应链项）

## 下一步

1. GPU 主机上跑 `make gpu-preflight && make gpu-test`（G1–G4）
2. K8s 集群上 `EMBODIEDCLOUD_K8S_TEST=1 pytest -m k8s_integration`
3. v0.3.1 剩余：真实集群 GPU inventory adapter 验证

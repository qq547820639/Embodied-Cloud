# MASTER_PLAN — EmbodiedCloud

> 版本：0.6.0（2026-09-26）。滚动更新；每完成一个 milestone 同步 `docs/CURRENT_STATE.md` 与 `docs/ACCEPTANCE_GATES.md`。

## 目标

在"无物理 GPU/机器人/凭据"的当前环境内，完成全部可完成的工程工作，产出可部署、可测试、可维护的真实产品，并对硬件项如实标记 PHYSICAL_GPU_VALIDATION_PENDING / BLOCKED_EXTERNAL_DEPENDENCY。

## Milestones

### M0 审计与基线（DONE）
- 审计、CURRENT_STATE、构建基线（ruff/mypy/pytest/build/CI）、治理文档。

### M1 数据与生命周期（DONE）
- Alembic 迁移、扩展模型（User/Org/Gpu/Ledger/Template/Streaming/Course/Deployment）、
- WorkspaceStatus CREATED/DELETED、crash recovery。

### M2 控制面核心（DONE）
- Auth + 隔离、GPU Scheduler + Provider（Mock/Docker/K8s）、Credit Ledger + Usage。

### M3 产品能力（DONE）
- Template Registry（5 SKU）、Streaming 状态机、Warm Pool、Observability。

### M4 场景扩展（DONE）
- Course/Lab/Assignment、Edge Agent + RobotDriver + Deployment 流程。

### M5 验收与交付（DONE）
- G1–G4 脚本、Release 流程、最终 acceptance matrix、提交推送。

## 当前状态

见 `docs/CURRENT_STATE.md`。当前 v0.6.0：对象存储真后端（VersityGW）与 K8s 控制面真集群（kind）已进常驻门禁；上一轮 v0.5.0 做的是 PostgreSQL 真并发、Docker provider 真容器、真实浏览器 DOM 三档已进常驻门禁（docker daemon 已可用，不再是遗留项）。
遗留 BLOCKED：NVIDIA GPU/容器运行时 / NGC 凭据 / S3 凭据 / 真实 K8s 集群 / 真机机器人。

## 商业 Gate（预留）

- G1：10 users / 5 experiments → 优化 activation、templates、reliability
- G2：30 devs / 15 experiments → 引入付费
- G3：5 paying users 或 2 paying labs → 商业化

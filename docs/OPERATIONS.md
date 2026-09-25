# OPERATIONS — EmbodiedCloud

> 版本：0.5.0（2026-09-26）。环境拓扑、监控、容量与运维约定。

## 1. 部署拓扑

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│ Control Plane (1+ replicas) │        │ GPU Host（单机模式）          │
│  FastAPI + SQLAlchemy       │───────▶│  Docker + NVIDIA runtime      │
│  /metrics, /api/*           │        │  每 Workspace 独占 GPU        │
└──────────┬──────────────────┘        └──────────────────────────────┘
           │                          ┌──────────────────────────────┐
           │                          │ K8s Cluster（生产多机）        │
           ├─────────────────────────▶│  NVIDIA device plugin         │
           │                          │  PVC + fsGroup                │
           ▼                          │  Ingress / TLS                │
  PostgreSQL（生产）                  └──────────────────────────────┘
  Redis（未来 streaming/queue）
```

## 2. 监控

- Prometheus 采集 `/metrics`（详见 Observability 章节）。
- 关键指标告警建议（未来接入 Alertmanager）：

| 指标 | 告警建议 |
|---|---|
| workspace_launch_failed_total 增量 | >0 持续 10min |
| workspace_running / gpu_allocated 失衡 | allocated > running + 5 |
| gpu UNHEALTHY 数量 | >0 |
| API 5xx 率 | >1% 持续 10min |

## 3. 容量模型

- 单 GPU = 1 Workspace（第一阶段）。
- GPU 秒计费 = RUNNING 起止差（stop/delete 结算）。
- Warm Pool 目标：P50 < 15s，P95 < 30s（需真实硬件实测调优）。

## 4. 数据生命周期

- 数据库：Alembic 迁移，SQLite 本地 / PostgreSQL 生产（**生产仅支持 PostgreSQL**：
  SQLite 方言丢弃 `FOR UPDATE`，行锁类并发保证拿不到；见 ADR 0005）。
- 预授权（credit_holds）：`billing_hold_ttl_minutes`（默认 60）界定崩溃残留的泄漏窗口，
  worker 每 60s 扫一次超时 pending 并退回；RUNNING 的 workspace 不回收，等结算转正。
- 迁移新增列/约束忘了写：`tests/test_migrations.py` 的模型↔迁移对账（`compare_metadata`）
  会在默认档直接红，不需要等生产库暴露。
- Workspace 数据：`/workspace/project|datasets|outputs|checkpoints` 持久化，重建不丢。
- checkpoint → artifact（checksum）→ deployment record → edge 下载验证。

## 5. 运维例行

- 每周：alembic current、备份验证、审计 ledger 抽样对账。
- 每次发布：G0 全部 Gate + 迁移 up/down + smoke（见 ACCEPTANCE_GATES.md）。
- 每季度：威胁模型复核、依赖扫描、镜像重扫。

## 6. 环境变量（.env）

见 `.env.example`；新增配置必须在 `app/config.py` 有默认值且文档化 —— 该约定由
`tests/test_config_docs.py` 双向核（字段未进文档、或文档留着 Settings 已删的键，都红）。

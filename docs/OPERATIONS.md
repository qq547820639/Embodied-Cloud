# OPERATIONS — EmbodiedCloud

> 版本：0.6.0（2026-09-26）。环境拓扑、监控、容量与运维约定。

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
- **`POST /deployments/{id}/verify` 返回 503 时不要动数据**：那表示对象存储不可用
  （鉴权失败 / 桶不存在 / 网络故障），部署记录仍停在 `downloading`，恢复后直接重试
  即可。只有 200 + `status=failed` 才是"关于产物的判决"（对象不存在或 checksum 不符），
  而那是终态、不会自动重验（ADR 0008）。

## 6. 环境变量（.env）

见 `.env.example`；新增配置必须在 `app/config.py` 有默认值且文档化 —— 该约定由
`tests/test_config_docs.py` 双向核（字段未进文档、或文档留着 Settings 已删的键，都红）。

两条运维含义容易踩：

- **对象存储**：`EMBODIEDCLOUD_ARTIFACT_BACKEND=s3` 必须先装 SDK
  （`pip install ".[s3]"`）。凭据或 endpoint 不全时**启动即失败**
  （`BLOCKED_EXTERNAL_DEPENDENCY`），不会退回本地目录 —— 退回会让产物落在控制面
  文件系统，而 `Artifact.store_name` 仍记 `s3`，校验时找不到对象。
- **`EMBODIEDCLOUD_K8S_KUBECONFIG`**：留空时走 SDK 默认解析，但 kubernetes Python SDK
  在**模块 import 时**就把 `KUBECONFIG` 固化为常量（实测：进程启动后再 export 该变量，
  加载直接报 `Invalid kube-config file`）。要在进程生命周期内切换集群/挂载专用
  kubeconfig，请用本项而不是改环境变量。

## 7. 验证档位与前置条件

`make test` 一次跑全部档位；单档可点名单跑。缺前置条件的档位是**整档 skip +
在 `docs/VALIDATION.json` 记 PENDING(原因)**，CI 对需要 docker 的档位要求 PASS。

| 档位 | 命令 | 前置（本机实测） |
|---|---|---|
| PostgreSQL 真并发 | `make test-pg` | docker daemon + `postgres:16-alpine` 缓存 + `.[postgres]` |
| Docker provider 真容器 | `make test-docker` | docker daemon + 与 daemon 同架构的已缓存测试镜像 |
| 浏览器真 DOM | `make test-browser` | Playwright + 系统 Chrome/Chromium |
| 对象存储真后端 | `make test-s3` | docker daemon + `versity/versitygw:v1.8.0` 缓存 + `.[s3]` |
| K8s 控制面真集群 | `make test-k8s-control-plane` | docker daemon + `kindest/node:v1.37.0` 缓存 + kind 二进制（`EMBODIEDCLOUD_KIND_BIN` 或 PATH）；**不需要 GPU** |
| K8s GPU 全流程 | `EMBODIEDCLOUD_K8S_TEST=1 pytest -m k8s_integration` | 真实集群 + NVIDIA Device Plugin（物理待验） |

测试自建的外部资源一律自持有、用完即删：PG/S3 容器带 `--rm` 与 atexit 兜底，
kind 集群写进临时 KUBECONFIG（不合并 `~/.kube/config`）并在退出时 `kind delete cluster`。
档位内部**只起一个 kind 集群**：本机曾出现两个集群并跑时 API server 连接被掐
（`SSLEOFError`，一次观测，未做对照复现），删掉多余集群后同一条用例链稳定通过。

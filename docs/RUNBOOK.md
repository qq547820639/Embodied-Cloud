# RUNBOOK — EmbodiedCloud

> 版本：0.6.0（2026-09-26）。面向 SRE/值班工程师；先看 `docs/ACCEPTANCE_GATES.md` 了解验证分级。

## 1. 本地开发（Mock）

```bash
make install        # python3.12 venv + pip install -e '.[dev]'
make dev            # uvicorn --reload :8000
make test           # pytest
make lint && make typecheck
```

打开 http://127.0.0.1:8000（Dashboard）。

## 2. 数据库

```bash
make migrate-up     # alembic upgrade head（SQLite 默认：./embodiedcloud.db）
make migrate-downgrade
EMBODIEDCLOUD_DATABASE_URL=postgresql+psycopg://... make migrate-up   # 生产
```

首次启动控制面时若表不存在会自动 `Base.metadata.create_all`（开发便利，
默认 `EMBODIEDCLOUD_AUTO_CREATE_TABLES=true`）；正式环境请显式执行迁移并设为
`EMBODIEDCLOUD_AUTO_CREATE_TABLES=false`（生产多实例必须走 alembic）。

## 3. 启动/停止

| 操作 | 命令 |
|---|---|
| 启动控制面 | `EMBODIEDCLOUD_PROVIDER=mock make dev` 或 `scripts/run_demo.sh` |
| GPU 单机模式 | `EMBODIEDCLOUD_PROVIDER=docker EMBODIEDCLOUD_EULA_ACCEPTED=true make dev` |
| Compose | `make compose-up` / `make compose-down` |
| K8s | `kubectl apply -f deploy/kubernetes/`（配置 kubeconfig 后） |

## 4. 健康检查

```bash
curl -s http://127.0.0.1:8000/api/health
# {"status":"ok","provider":"mock","provider_ready":true,...}
curl -s http://127.0.0.1:8000/metrics | head
```

- 控制面 down → 检查 uvicorn 日志；DB 连接 → `alembic current`。
- Provider 异常 → `/api/health` 的 `provider_ready=false` + `provider_detail` 原因。

## 5. GPU 主机上线（BLOCKED_EXTERNAL_DEPENDENCY：需要真实 NVIDIA 主机）

```bash
./scripts/preflight_gpu_host.sh        # G1.1
docker login nvcr.io                   # NGC 凭据
./scripts/build_workspace_image.sh     # 构建 workspace 镜像
./scripts/gpu_acceptance.sh            # G1.2 兼容检查
EMBODIEDCLOUD_HOST_PUBLIC_IP=<ip> EMBODIEDCLOUD_PROVIDER=docker make dev
```

GPU 验收 Gate 见 `docs/GPU_HOST.md` 与 `docs/ACCEPTANCE_GATES.md`。

## 6. 故障排查

| 症状 | 检查 |
|---|---|
| Workspace 卡 QUEUED/PROVISIONING | 查 DB `workspaces.status`；启动时 reconcile 会按 runtime 事实收敛（PROVISIONING 缺失 → 重新入队；RUNNING 缺失 → 结算置 FAILED 并归还 GPU） |
| FAILED 无错误 | `GET /api/workspaces/{id}` 的 `error_message` |
| 端口冲突 | `ss -ltn` / 查 `ide_port`；streaming 固定 49100/47998 |
| GPU 未释放 | `gpu_allocations` 表 + `gpus.workspace_id`；正常由 stop/destroy/reconcile 自动释放（`gpus.status → available` 且 `workspace_id=NULL`），勿只删 allocation 行 |
| 日志无 request_id | 确认经过 RequestIDMiddleware（生产开启 `EMBODIEDCLOUD_LOG_JSON=true`） |

## 7. 备份

- SQLite：`cp embodiedcloud.db embodiedcloud.db.bak`
- 生产 PostgreSQL：常规 pg_dump；workspace 数据在 PVC/持久卷，单独备份策略。

## 8. 回滚

- 代码：`git revert` 上一个 release tag；镜像回退到上一版本 tag。
- DB：`alembic downgrade -1`（迁移必须可逆）。

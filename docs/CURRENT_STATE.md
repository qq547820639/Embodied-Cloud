# CURRENT_STATE — EmbodiedCloud

> 版本：0.2.0（2026-08-12）。结论依据：本机实际执行结果。
> 环境：macOS（Python 3.12.13 / uv / 无 Docker daemon / 无 NVIDIA GPU / 无 NGC 凭据 / 无真实机器人）

## 1. 执行记录（v0.2.0 交付）

| 审计项 | 命令 | 结果 |
|---|---|---|
| 安装 | `make install`（python3.12 venv） | PASS |
| Lint | `make lint`（ruff + shell syntax） | PASS（0 errors） |
| Type | `make typecheck`（mypy） | PASS（36 files） |
| 测试 | `make test`（pytest） | PASS（84 passed） |
| 迁移 | `alembic upgrade head / downgrade base` 循环 | PASS（tests/test_migrations.py） |
| 构建 | `make build` | PASS（wheel + sdist） |
| Smoke | `make smoke`（auth 全流程 + metrics） | PASS |
| Release | `scripts/release.sh` | PASS（checksums + VALIDATION_STATUS.md） |
| CLI | `python -m app.cli bootstrap-admin/list-gpus/show-usage` | PASS |
| API 端到端 | 注册→模板→建工作区→running→access→usage→stop→delete | PASS |
| 隔离 | User A 读/改/删 User B workspace | 404（tests/test_isolation.py 3 用例） |
| 调度并发 | 8 线程抢 2 GPU | 无重复分配（tests/test_scheduler.py） |
| Ledger 幂等 | 同运行段重复结算 | 不重复扣款（tests/test_ledger.py） |
| Secret 扫描 | grep 全库 | PASS（无 hard-coded secret） |

## 2. 分项状态

### Working（本环境已验证）
- 控制面：FastAPI 全 API 面（auth/templates/workspaces/gpus/usage/ledger/streaming/courses/edge/deployments/metrics/health）
- Auth + 隔离：注册/登录/登出/me；owner/org 校验；PBKDF2 哈希会话
- GPU Scheduler：原子分配/释放/并发防重/unhealthy/draining/crash recovery
- 不可变 CreditLedger + 幂等 usage 结算
- Alembic 迁移体系（SQLite up/down 验证；PostgreSQL 配置就绪未实测）
- Template Registry（5 SKU，version locked）
- Streaming 状态机（starting/ready/connected/disconnected/failed，模拟链路测试）
- Warm Pool manager + benchmark harness
- Course/Lab/Assignment/Submission 流程
- Edge Agent + RobotDriver(Mock) + Deployment 流程（checksum/verify）
- K8sWorkspaceProvider（离线单测 8 用例；集群连接 BLOCKED）
- Observability：/metrics + JSON 日志 + request_id + 脱敏
- CLI 运维工具 + 静态 UI（登录注册 + 工作区管理）
- G1–G4 验收脚本 + release.sh

### Partially Working
- Docker GPU Provider：代码与单测完整，未对真实 Docker daemon/GPU 执行
- K8s Provider：单测通过，未对真实集群执行
- Streaming：控制面状态机完整，真实 WebRTC 媒体链路未验证

### Broken
- 无（本环境可复现范围内 0 失败）

### Mock Only
- GPU 分配（mock-gpu-0001/0002 虚拟 inventory）
- 浏览器 IDE / Streaming 视图（demo 页）

### Blocked External（本环境无法完成，不标 PASS）
- Docker daemon / NVIDIA Container Toolkit → G1
- NVIDIA GPU → G2/G3/G4
- NGC 凭据 → workspace 镜像构建
- 真实机器人 → Sim2Real（G5.2）
- PostgreSQL / 真实 K8s 集群 → 生产模式验证

## 3. 结论

v0.2.0 交付了 P0–P2 全部软件工程能力：身份与隔离、GPU 调度、不可变计费、Template Registry、
Streaming 状态机、Warm Pool、高校课程、Edge/Sim2Real 模块、K8s Provider、可观测性、CI/CD、release 流程。
84 个测试覆盖核心模块与主路径；lint/type/build/smoke 全绿。全部真实硬件验收项
（G1–G4、Streaming 媒体面、Sim2Real 真机）如实标记 BLOCKED_EXTERNAL_DEPENDENCY，未伪造 PASS。

## 4. 遗留风险
- 计费汇率为固定 1s=1 credit 展示口径；真实定价费率需产品决策后入 ledger service。
- Streaming 单实例固定端口策略仍为保守 MVP 决策（ADR 0001）。
- Warm Pool 性能目标（P50<15s/P95<30s）需真实 GPU 实测调优。
- PostgreSQL/Redis/对象存储的生产拓扑（docs/K8S_PRODUCTION.md）未在本环境实测。

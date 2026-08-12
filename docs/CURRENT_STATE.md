# CURRENT_STATE — EmbodiedCloud

> 生成时间：2026-08-12（autonomous audit 首次执行）
> 结论依据：本机实际执行结果，而非代码存在与否。
> 环境：macOS（Python 3.12.13 / uv / 无 Docker daemon / 无 NVIDIA GPU / 无 NGC 凭据 / 无真实机器人）

## 1. 执行记录

| 审计项 | 命令 | 结果 |
|---|---|---|
| Git 状态 | `git status` | 干净，分支 main，跟踪 origin/main |
| 安装 | `python3.12 -m venv .venv && pip install -e '.[dev]'` | PASS |
| 单元/集成测试 | `pytest` | PASS（4 passed） |
| 编译 | `python -m compileall -q app` | PASS |
| 类型检查 | `mypy app` | PASS（12 files, no issues） |
| Lint | `ruff check app tests` | FAIL（4× BLE001，均为 provider 边界的**有意**盲捕获，需配置豁免） |
| 启动 | `uvicorn app.main:app --port 8000` | PASS |
| API health | `GET /api/health` | PASS（mock provider ready） |
| API templates | `GET /api/templates` | PASS（3 模板） |
| API create workspace | `POST /api/workspaces` | PASS（queued） |
| API 生命周期 | create→running→access→usage→stop | PASS（mock） |
| Secret 扫描 | grep password/token/secret/private key | PASS（无 hard-coded secret） |
| TODO/FIXME 扫描 | grep TODO/FIXME/HACK/pass/stub | 仅 DELIVERY.md 文案提及（非代码） |

## 2. 分项状态

### Working（本环境已验证）
- Python 控制面（FastAPI + SQLAlchemy + SQLite）可启动、可调 API
- Template seed（3 个内置模板，幂等）
- Workspace 生命周期 API：create / start / stop / delete / access / usage
- MockWorkspaceProvider 完整产品主路径（含 access secret、demo 页）
- 端口分配（TCP/UDP allocator + 测试）
- 静态 Web UI（index.html / app.js / styles.css）
- DockerProvider 的 fake-test 覆盖（EULA gate、streaming 端口策略、env 注入）

### Partially Working
- **Docker GPU Provider**：代码完整、有单测，但从未对真实 Docker daemon/GPU 执行（本机无 docker/nvidia-smi）
- **Usage 计量**：基于 `accumulated_seconds` + 实时补算的估算，非不可变 ledger，无幂等、无审计
- **CI**：仅 pytest + shell syntax，缺 lint/typecheck/build/smoke 阶段
- **Makefile**：只有 demo/test/check/control-image/workspace-image，缺 install/dev/lint/typecheck/build/smoke/clean 等

### Broken
- ruff 4× BLE001（**有意**盲捕获，通过 pyproject 配置豁免即可，非逻辑缺陷）
- 无其他已复现的 Broken 项

### Mock Only
- GPU 分配（Mock GPU / no real compute）
- Streaming（demo 页 + stream_hint，无真实 WebRTC 状态机）
- 浏览器 IDE（demo 页，非 code-server）

### Missing（对照 P0–P2 目标）
- Auth / User / Organization / Role / Session —— **任何 Workspace 查询无 owner/org 校验**
- GPU Scheduler（原子分配 / 不重复分配 / crash recovery / inventory）
- Immutable Credit Ledger（Transaction: RECHARGE/USAGE/PROMOTION/REFUND/ADJUSTMENT）
- Template Registry 完整模型（slug/version/image/gpu_requirement/entrypoints/outputs/streaming/healthcheck/metadata）
- Warm Pool（metrics / pool manager / benchmark harness）
- Streaming 状态机（starting/ready/connected/disconnected/failed）
- Course / Lab / Assignment / Submission
- Edge Agent（register/heartbeat/device_info/download/verify/start/stop/telemetry）+ RobotDriver
- KubernetesWorkspaceProvider
- Observability（metrics 端点、结构化日志、request_id/user_id/workspace_id 上下文）
- 数据库 migrations（当前仅 create_all）
- Async provisioning 的 crash recovery（重启后 QUEUED/PROVISIONING 的恢复）
- WorkspaceStatus.CREATED 状态
- G3/G4 验收脚本（isaac_sim_smoke / isaac_lab_cartpole_smoke / franka_smoke）
- Release 流程（checksum / migration notes / 分级验证矩阵）
- Threat model / audit log

### Blocked External（本环境无法完成）
- Docker daemon / NVIDIA Container Toolkit —— G1 验收
- NVIDIA GPU —— G2 渲染 / G3 Cartpole / G4 Franka
- NGC 凭据（nvcr.io 登录）—— 构建 isaac-sim 基础镜像
- 真实机器人 —— Sim2Real 物理验收

## 3. 结论

仓库是一个**真实可运行、测试覆盖基础完整**的 MVP（Mock 模式）。所有真实硬件能力均为"部署就绪代码/脚本"而非"已验证"。当前可立即推进：治理文件、P0 代码正确性（ruff 豁免配置）、Workspace 生命周期完备化、GPU Scheduler、Auth/Isolation、Ledger、Template Registry、Observability、CI/CD、G3/G4 脚本、K8s Provider、Edge Agent，均不依赖外部物理资源。

## 4. 已知风险
- Python 3.9 系统默认解释器无法满足 `requires-python >=3.12`，必须使用 `python3.12`/`.venv`（本机已验证可用）
- Streaming 单实例固定端口策略（49100/TCP + 47998/UDP）是保守 MVP 决策，多实例需 gateway
- Docker 卷权限 0o777 为单机可信模式，K8s/PVC 需 fsGroup 策略替换

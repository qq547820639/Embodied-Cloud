# ACCEPTANCE_GATES — EmbodiedCloud

> 版本：0.5.0（2026-09-26，v0.2.1 + v0.3.0 + v0.4.0 软件面 + v0.5.0 验证纵深/预授权）。验收分级严格区分：**VERIFIED PASS** / **IMPLEMENTED BUT NOT PHYSICALLY VERIFIED** / **FAILED** / **BLOCKED_EXTERNAL_DEPENDENCY**。

## G0 软件验收（本机/CI 自动执行）

| Gate | 命令 | 期望 | 状态 |
|---|---|---|---|
| G0.1 Lint | `make lint` | ruff 0 错误 + shell syntax OK | VERIFIED PASS |
| G0.2 Type | `make typecheck` | mypy no issues | VERIFIED PASS |
| G0.3 Unit | `make test` | pytest 全绿（精确计数见 `docs/VALIDATION.json`，`make validate` 自动生成） | VERIFIED PASS |
| G0.4 Build | `make build` | wheel 产出 | VERIFIED PASS |
| G0.5 Smoke | `make smoke` | API 生命周期冒烟 | VERIFIED PASS |
| G0.6 Migrations | `alembic upgrade head` / `downgrade base` | empty→head 14 文件迁移链 + 关键表落地 + downgrade 循环 + **模型↔迁移对账**（`compare_metadata` 非空即红） | VERIFIED PASS |
| G0.7 OpenAPI freshness | `make api-docs && git diff --exit-code` | 重新生成无 diff | VERIFIED PASS |
| G0.8 GPU single authority | `pytest tests/test_gpu_single_authority.py` | DB↔docker --gpus 一致 | VERIFIED PASS |
| G0.9 Provision rollback | `pytest tests/test_provision_rollback.py` | 失败无孤儿资源 | VERIFIED PASS |
| G0.10 Durable ops/reconcile | `pytest tests/test_worker.py` | lease/串行/幂等 | VERIFIED PASS |
| G0.11 Immutable versions | `pytest tests/test_template_versions.py` | released 不可覆盖 | VERIFIED PASS |
| G0.12 Billing policy | `pytest tests/test_billing_policy.py` | 负余额/配额拒绝 | VERIFIED PASS |
| G0.13 Credential at rest | `pytest tests/test_workspace_credential.py` | DB 无明文密码 | VERIFIED PASS |
| G0.14 Auth session | `pytest tests/test_auth.py` | login/logout/me + verify_password 边界 + token 仅存哈希 | VERIFIED PASS |
| G0.15 GPU admin & release | `pytest tests/test_gpu_admin.py` | admin inventory + 分配→释放真实断言 | VERIFIED PASS |
| G0.16 Frontend 安全/课程 onboarding | `pytest tests/test_demo_workspace.py tests/test_course_onboarding.py tests/test_demo_checkpoint.py` | demo 页转义/鉴权、slug 加入、member 可见、mock checkpoint | VERIFIED PASS |
| G0.17 K8s integration | `pytest -m k8s_integration` | 无集群 → 整档 PENDING 并在 VALIDATION 登记原因（哨兵，不用 skip 冒充） | K8S_PHYSICAL_VALIDATION_PENDING（不假装 PASS） |
| G0.18 PostgreSQL 真并发 | `make test-pg` | 自建一次性 PG 容器；`FOR UPDATE`/`SKIP LOCKED`/CAS/部分唯一索引在真行锁下成对验证 | VERIFIED PASS（18/18） |
| G0.19 Docker provider 真容器 | `make test-docker` | 真守护进程跑 health/start/logs/wait_ready/reconcile/流式占用；会话级容器泄漏守卫 | VERIFIED PASS（17/17） |
| G0.20 浏览器真 DOM | `make test-browser` | Playwright + 系统 Chrome：XSS 载荷不执行、轮询真的停、控制台零错误 | VERIFIED PASS（11/11） |
| G0.21 K8s 线格式合规 | `pytest tests/test_k8s_model_conformance.py` | provider 生成的对象过真实 SDK 的 `sanitize_for_serialization` | VERIFIED PASS（6/6） |
| G0.22 预授权语义 | `pytest tests/test_credit_holds.py` | 圈住/转正/退回/过期回收 + 3 条走 worker 真实执行链 | VERIFIED PASS（14/14） |
| G0.23 供应链可复现 | `make verify-lock && make sbom && make audit` | universal uv.lock 一致、SBOM 生成、审计无阻断项；CI 要求 docker 档必须真 PASS | VERIFIED PASS |
| G0.24 约定入门禁 | `pytest tests/test_config_docs.py` + `test_migrations.py` 对账用例 | Settings↔.env.example 双向、模型声明↔迁移产物（`compare_metadata`） | VERIFIED PASS |

## G1 物理 GPU 主机预检（BLOCKED_EXTERNAL_DEPENDENCY：本机无 NVIDIA 设备/容器运行时；docker daemon 本身可用，见 G0.19）

| Gate | 脚本 | 期望 PASS 条件 |
|---|---|---|
| G1.1 预检 | `scripts/preflight_gpu_host.sh` | docker + nvidia-smi + NVENC 存在；端口可绑定 |
| G1.2 兼容检查 | `scripts/gpu_acceptance.sh` | isaac-sim compatibility_check 退出码 0 |

## G2 Isaac Sim GPU 渲染 / headless（BLOCKED）

| Gate | 脚本 | 期望 PASS 条件 |
|---|---|---|
| G2.1 | `scripts/isaac_sim_smoke.sh` | 容器内 headless 渲染 10s 无异常 |

## G3 Isaac Lab Cartpole（BLOCKED）

| Gate | 脚本 | 期望 PASS 条件 |
|---|---|---|
| G3.1 | `scripts/isaac_lab_cartpole_smoke.sh` | 5 iteration 训练完成，loss/reward 非 NaN（模板 `cartpole`） |

## G4 Franka manipulation / WebRTC（BLOCKED）

| Gate | 脚本 | 期望 PASS 条件 |
|---|---|---|
| G4.1 | `scripts/franka_smoke.sh` | Franka Lift 训练或推理 smoke 完成（模板 `franka-lift`） |
| G4.2 | WebRTC 流 | 模板 `franka-pick-place`：49100/TCP + 47998/UDP 监听，客户端可见仿真画面 |

## G5 真机 Sim2Real（BLOCKED：无真实机器人）

| Gate | 脚本 | 期望 PASS 条件 |
|---|---|---|
| G5.1 | edge agent + RobotDriver（Mock） | mock 驱动回环通过（控制台「部署/边缘设备」页可走通全链路） |
| G5.2 | 物理真机 | 需要真实机器人；本环境不可执行 |

## 当前总状态

- VERIFIED PASS：G0 全部（精确计数见 `docs/VALIDATION.json`，`make validate` 自动生成）
- IMPLEMENTED BUT NOT PHYSICALLY VERIFIED：Streaming 媒体面、G1–G4 脚本、K8s provider 真实集群路径（其请求体形状已由 G0.21 覆盖）
- 已由真后端覆盖（v0.5.0 起不再属于上一条）：Docker provider 非 GPU 路径（G0.19）、PostgreSQL 并发语义（G0.18）、前端真实 DOM（G0.20）
- FAILED：无
- BLOCKED_EXTERNAL_DEPENDENCY：G1.1–G4（Docker/NVIDIA/NGC）、G5.2（真机）

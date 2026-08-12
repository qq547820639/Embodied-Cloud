# ACCEPTANCE_GATES — EmbodiedCloud

> 版本：0.3.0（2026-08-12，v0.2.1+v0.3.0 软件面）。验收分级严格区分：**VERIFIED PASS** / **IMPLEMENTED BUT NOT PHYSICALLY VERIFIED** / **FAILED** / **BLOCKED_EXTERNAL_DEPENDENCY**。

## G0 软件验收（本机/CI 自动执行）— 2026-08-12 实测

| Gate | 命令 | 期望 | 状态 |
|---|---|---|---|
| G0.1 Lint | `make lint` | ruff 0 错误 + shell syntax OK | VERIFIED PASS |
| G0.2 Type | `make typecheck` | mypy no issues（39 files） | VERIFIED PASS |
| G0.3 Unit | `make test` | pytest 全绿（**152 passed + 2 skipped**） | VERIFIED PASS |
| G0.4 Build | `make build` | wheel 产出 | VERIFIED PASS |
| G0.5 Smoke | `make smoke` | API 生命周期冒烟 | VERIFIED PASS |
| G0.6 Migrations | `alembic upgrade head` | empty→head 5 级迁移链 | VERIFIED PASS |
| G0.7 OpenAPI freshness | `make api-docs && git diff --exit-code` | 重新生成无 diff | VERIFIED PASS |
| G0.8 GPU single authority | `pytest tests/test_gpu_single_authority.py` | DB↔docker --gpus 一致 | VERIFIED PASS（5 用例） |
| G0.9 Provision rollback | `pytest tests/test_provision_rollback.py` | 失败无孤儿资源 | VERIFIED PASS（6 用例） |
| G0.10 Durable ops/reconcile | `pytest tests/test_worker.py` | lease/串行/幂等 | VERIFIED PASS（10 用例） |
| G0.11 Immutable versions | `pytest tests/test_template_versions.py` | released 不可覆盖 | VERIFIED PASS（6 用例） |
| G0.12 Billing policy | `pytest tests/test_billing_policy.py` | 负余额/配额拒绝 | VERIFIED PASS（5 用例） |
| G0.13 Credential at rest | `pytest tests/test_workspace_credential.py` | DB 无明文密码 | VERIFIED PASS（3 用例） |
| G0.14 K8s integration | `pytest -m k8s_integration` | 无集群 → skip | K8S_PHYSICAL_VALIDATION_PENDING（不假装 PASS） |

## G0 软件验收（本机/CI 自动执行）

| Gate | 命令 | 期望 | 状态 |
|---|---|---|---|
| G0.1 Lint | `make lint` | ruff 0 错误 + shell syntax OK | VERIFIED PASS |
| G0.2 Type | `make typecheck` | mypy no issues | VERIFIED PASS |
| G0.3 Unit | `make test` | pytest 全绿 | VERIFIED PASS |
| G0.4 Build | `make build` | wheel 产出 | VERIFIED PASS |
| G0.5 Smoke | `make smoke` | API 生命周期冒烟 | VERIFIED PASS |
| G0.6 Migrations | `alembic upgrade head` | up/down 可执行 | VERIFIED PASS |

## G1 物理 GPU 主机预检（BLOCKED_EXTERNAL_DEPENDENCY：无 Docker/NVIDIA）

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
| G3.1 | `scripts/isaac_lab_cartpole_smoke.sh` | 5 iteration 训练完成，loss/reward 非 NaN |

## G4 Franka manipulation（BLOCKED）

| Gate | 脚本 | 期望 PASS 条件 |
|---|---|---|
| G4.1 | `scripts/franka_smoke.sh` | Franka Lift 训练或推理 smoke 完成 |

## G5 真机 Sim2Real（BLOCKED：无真实机器人）

| Gate | 脚本 | 期望 PASS 条件 |
|---|---|---|
| G5.1 | edge agent + RobotDriver（Mock） | mock 驱动回环通过 |
| G5.2 | 物理真机 | 需要真实机器人；本环境不可执行 |

## 当前总状态

- VERIFIED PASS：G0 全部（见 CURRENT_STATE 执行记录）
- IMPLEMENTED BUT NOT PHYSICALLY VERIFIED：Docker/K8s Provider、Streaming、G1–G4 脚本
- FAILED：无
- BLOCKED_EXTERNAL_DEPENDENCY：G1.1–G4（Docker/NVIDIA/NGC）、G5.2（真机）

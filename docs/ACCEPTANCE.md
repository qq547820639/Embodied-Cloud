# 交付验收记录

> 早期验收记录（v0.1）。最新验收状态与分级矩阵见 `docs/ACCEPTANCE_GATES.md`、
> `docs/CURRENT_STATE.md` 与 `docs/VALIDATION.md`（`make validate` 自动生成）。

## 本环境自动验收

| 项 | 命令 | 当前结果 |
|---|---|---|
| Python 编译 | `python -m compileall -q app` | PASS |
| Shell 语法 | `bash -n scripts/*.sh runtime/workspace-entrypoint.sh` | PASS |
| API/Provider 单测 | `pytest` | PASS（v0.1 基线 4 tests；当前版本见 VALIDATION.json） |
| Mock 实例启动 | `python -m uvicorn app.main:app ...` | PASS |
| API smoke | `scripts/smoke_api.sh` | PASS |
| Python editable package | `pip install -e '.[dev]' --no-build-isolation` | PASS |

## 无法在当前执行环境完成的外部验收

原因不是代码权限，而是当前运行环境没有 Docker daemon、NVIDIA GPU、NGC 登录态。

必须迁移到目标 GPU 主机执行（Gate 编号以 `docs/ACCEPTANCE_GATES.md` 为准）：

1. `scripts/gpu_acceptance.sh`（G1.2）
2. G2 Isaac Sim headless 渲染
3. G3 Isaac Lab Cartpole 训练（模板 `cartpole`）
4. G4 Franka Lift Cube（模板 `franka-lift`）与 WebRTC 流（模板 `franka-pick-place`）

任何一项未通过都不能把“真实 GPU SaaS”标成已上线。

## 发布判断

- **可立即发布：** 本地/内网 Mock 产品 Demo、API/UI、研发联调版。
- **可进入 GPU 验收：** 单机 Docker GPU 版。
- **不可直接发布：** 公网多租户生产 SaaS（身份、网关、PostgreSQL、K8s Provider 尚属于 Beta Gate）。

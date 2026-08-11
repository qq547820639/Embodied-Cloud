# 交付验收记录

## 本环境自动验收

| 项 | 命令 | 当前结果 |
|---|---|---|
| Python 编译 | `python -m compileall -q app` | PASS |
| Shell 语法 | `bash -n scripts/*.sh runtime/workspace-entrypoint.sh` | PASS |
| API/Provider 单测 | `pytest` | PASS（4 tests） |
| Mock 实例启动 | `python -m uvicorn app.main:app ...` | PASS |
| API smoke | `scripts/smoke_api.sh` | PASS |
| Python editable package | `pip install -e '.[dev]' --no-build-isolation` | PASS |

## 无法在当前执行环境完成的外部验收

原因不是代码权限，而是当前运行环境没有 Docker daemon、NVIDIA GPU、NGC 登录态。

必须迁移到目标 GPU 主机执行：

1. `scripts/gpu_acceptance.sh`
2. G1 Newton smoke
3. G2 Franka Lift Cube
4. G3 WebRTC smoke

任何一项未通过都不能把“真实 GPU SaaS”标成已上线。

## 发布判断

- **可立即发布：** 本地/内网 Mock 产品 Demo、API/UI、研发联调版。
- **可进入 GPU 验收：** 单机 Docker GPU 版。
- **不可直接发布：** 公网多租户生产 SaaS（身份、网关、PostgreSQL、K8s Provider 尚属于 Beta Gate）。

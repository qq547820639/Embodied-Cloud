# EmbodiedCloud 本次交付

## 交付结论

已把 BP 收敛成一个可以开始卖的产品本体：**Isaac Lab Cloud Workspace**。

本仓库不是原 BP 全部愿景的“假完成”。已经完成当前环境能真实验证的全部开发闭环；所有依赖实体 NVIDIA GPU 与大容量构建机的验收都被固化为脚本和 Gate，而不是口头 TODO（NGC 那一侧本轮实测**不是**凭据问题：匿名 pull 令牌即可取回基础镜像的清单与层字节）。

## 已交付项目本体

- FastAPI 控制面
- Template Catalog
- Workspace lifecycle
- Mock Provider
- Docker GPU Provider
- GPU 分配策略
- Browser Dashboard
- code-server access
- Usage/cost estimation
- Isaac Sim 6.0.1 + Isaac Lab 3.0 Beta 2 workspace image recipe
- WebRTC 单槽位安全实现
- NVIDIA host preflight/acceptance scripts
- Control-plane Dockerfile / Compose / K8s skeleton / Nginx config
- 自动测试 / CI / Makefile
- 0–12 周商业 Beta 执行计划

## 立即执行顺序

```text
A. 本地演示
   pip install -e '.[dev]'
   ./scripts/run_demo.sh
   -> http://127.0.0.1:8000

B. GPU 主机
   ./scripts/preflight_gpu_host.sh
   # 无需 docker login nvcr.io：依据与实测读数见 docs/GPU_HOST.md §2
   ./scripts/build_workspace_image.sh
   ./scripts/gpu_acceptance.sh

C. 启动 GPU 控制面
   EMBODIEDCLOUD_HOST_PUBLIC_IP=<GPU_HOST_IP> ./scripts/run_gpu_controlplane.sh

D. 产品 Gate
   G1 Newton -> G2 Franka -> G3 WebRTC

E. 真实用户 Gate
   10 人内测 -> 30 Workspace -> 15 成功实验 -> 5 个付费/强意向
```

## 决策冻结

- 现在不做国产仿真引擎。
- 现在不做通用硬件。
- 现在不做资产商城。
- 先把“点一个模板，能在云 GPU 里成功跑一个具身实验”做到稳定并收费。

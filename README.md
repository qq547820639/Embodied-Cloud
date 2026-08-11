# EmbodiedCloud MVP 0.1

**定位：Isaac Lab Cloud Workspace。** 用户不先买 GPU、不配驱动，不从空白 Ubuntu 开始，而是从“可运行实验模板”开始。

这个仓库是原 EmbodiedCloud BP 的第一版**项目本体**，不是 PPT Demo：控制面、工作区状态机、模板、成本计量、浏览器 Dashboard、Mock Provider、单机 NVIDIA GPU Docker Provider、Isaac Lab Workspace 镜像 recipe、测试和上线手册都在这里。

## 两种运行模式

- **Mock 模式：现在就能跑。** 无 Docker、无 NVIDIA GPU 也能完整演示产品流程和 API。
- **Docker GPU 模式：面向一台真实 NVIDIA GPU 主机。** 每个 Workspace 独占一张物理 GPU；浏览器 IDE 与 Isaac Lab 位于同一容器。

Streaming 的 v0.1 实现有意保守：用户运行 Isaac Lab 命令后由该进程自身提供 WebRTC；同一宿主只允许 1 个 Streaming Workspace，固定 49100/TCP + 47998/UDP，避免宣称未经真机验证的动态多实例端口能力。

> 重要：仓库不包含 NVIDIA 软件本体。GPU 模式构建时从 NVIDIA NGC 获取 Isaac Sim 基础镜像，并受 NVIDIA 对应许可约束。

## 30 秒运行产品 Demo

要求：Python 3.12+。

```bash
cd embodiedcloud-mvp
python -m pip install -e '.[dev]'
./scripts/run_demo.sh
```

打开：`http://127.0.0.1:8000`

页面上点任一模板 -> “一键启动” -> “打开 IDE”，可完整走完产品主路径（Mock，不消耗 GPU）。

## 自动测试

```bash
pytest
```

当前交付测试覆盖：
- API 健康检查；
- Template seed；
- Workspace create/start/access/stop/delete；
- 端口分配。

## 真实 GPU 模式

先看 `docs/GPU_HOST.md`。核心命令：

```bash
./scripts/preflight_gpu_host.sh
docker login nvcr.io
./scripts/build_workspace_image.sh
EMBODIEDCLOUD_HOST_PUBLIC_IP=<你的GPU主机IP> ./scripts/run_gpu_controlplane.sh
```

## 当前技术基线

- Isaac Lab：`v3.0.0-beta2.patch1`
- Isaac Sim：`6.0.1`
- Python：3.12（GPU runtime）
- code-server：4.130.0
- FastAPI + SQLAlchemy

## 第一批 Golden Template

1. `newton-cartpole-smoke` — 5 iteration 最小训练验收
2. `franka-lift-cube` — 机械臂抓取训练
3. `webrtc-streaming-smoke` — 0.5m 立方体 + WebRTC 链路验收

模板不是“课程目录”，而是产品 SKU：固定运行目标、固定镜像基线、固定验收条件。

## 当前验证状态

### 已在本次交付环境实际验证

- Python 控制面可启动
- API 生命周期自动测试
- Mock 产品主路径
- Workspace access secret
- 端口分配测试
- 静态 Web UI / API smoke

### 本次环境无法替代真实硬件验收

当前执行环境没有 Docker daemon、NVIDIA GPU 和 NGC 凭据，因此下列项是**部署就绪代码/脚本，但不能诚实标记为“已真机通过”**：

- `runtime/Dockerfile.isaaclab-workspace`
- Docker GPU Provider
- Isaac Sim/Isaac Lab 真 GPU 运行
- WebRTC 视频链路

真实 GPU 验收已经固化成 `docs/GPU_HOST.md` 的 G1/G2/G3 Gate，不需要再设计。

## 文档导航

- `docs/PRODUCT_SCOPE.md` — 产品边界/停止条件
- `docs/ARCHITECTURE.md` — 架构、状态机、Streaming 决策
- `docs/IMPLEMENTATION_PLAN.md` — 0–12 周详细执行计划 + Gate
- `docs/GPU_HOST.md` — 单 GPU 主机上线和真机验收
- `docs/K8S_PRODUCTION.md` — 多机生产迁移
- `docs/SECURITY.md` — 安全边界
- `docs/API.md` — API 快速参考
- `docs/BP_GAP_ANALYSIS.md` — 原 BP 到执行版本映射

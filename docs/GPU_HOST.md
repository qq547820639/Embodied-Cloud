# 单机 GPU Host 上线手册

## 推荐起点

- Ubuntu 24.04
- NVIDIA GPU，至少 16GB VRAM；需要 Streaming 时必须支持 NVENC
- Docker + NVIDIA Container Toolkit
- NGC 可拉取 `nvcr.io/nvidia/isaac-sim:6.0.1@sha256:783444c706538aa76cf5126e911ddc5e618779e6105305ad4af4260362a30aa9`
  （与 `runtime/Dockerfile.isaaclab-workspace` 的 `FROM` 同一字符串，常驻判据 G0.30 逐字核对；
  解析 digest 本身不需要凭据，**接受 NGC 条款后**才需要 `docker login nvcr.io` 拉层字节）

## 0. 先做 NVIDIA 官方兼容性检查

```bash
docker run --entrypoint bash -it --gpus all --rm --network=host \
  nvcr.io/nvidia/isaac-sim:6.0.1@sha256:783444c706538aa76cf5126e911ddc5e618779e6105305ad4af4260362a30aa9 \
  ./isaac-sim.compatibility_check.sh --/app/quitAfter=10 --no-window
```

必须看到系统兼容检查通过，再继续。

## 1. EmbodiedCloud 主机预检

```bash
./scripts/preflight_gpu_host.sh
```

## 2. 构建 Workspace 镜像

```bash
docker login nvcr.io
./scripts/build_workspace_image.sh
# 构建成功后脚本会打印镜像摘要与回填命令；不回填则该版本仍按可变 tag 启动
```

该镜像基于 Isaac Sim 6.0.1，固定 Isaac Lab `v3.0.0-beta2.patch1`，并加入 code-server。

## 3. 启动控制面

```bash
cp .env.example .env
# 编辑：
# EMBODIEDCLOUD_PROVIDER=docker
# EMBODIEDCLOUD_HOST_PUBLIC_IP=<GPU主机IP>
# EMBODIEDCLOUD_EULA_ACCEPTED=true

sudo mkdir -p /var/lib/embodiedcloud/workspaces
sudo chown -R "$USER":"$USER" /var/lib/embodiedcloud

EMBODIEDCLOUD_HOST_PUBLIC_IP=<GPU主机IP> ./scripts/run_gpu_controlplane.sh
```

打开 `http://<GPU主机IP>:8000`。

## 4. 必须按顺序验收三个模板

### Gate G1：Newton smoke

启动 `cartpole`，打开 IDE，运行 README 中命令。验收：5 iterations 正常结束，没有 CUDA/runtime import error。

### Gate G2：Franka headless

启动 `franka-lift`。验收：环境成功创建并开始训练；记录显存峰值和第一次 asset/cache 时间。

### Gate G3：WebRTC smoke

先停止其它 streaming workspace，再启动 `franka-pick-place（WebRTC 模板）`，在 IDE 内运行模板命令。验收：49100/TCP 与 47998/UDP 监听，客户端能看到 0.5m 立方体。

## 防火墙

- 控制面：8000/TCP（生产必须经 HTTPS 反向代理）。
- IDE：18000–18999/TCP（生产不应直接对公网开放，建议统一 gateway）。
- Isaac Lab WebRTC v0.1：49100/TCP + 47998/UDP。
- 浏览器 Web Viewer 如果采用 NVIDIA 官方 Docker Compose：还会使用 8210/TCP。

Streaming 端点本身不提供应用层认证/加密，**不要裸露公网**。最小方案是只允许固定客户端 IP，产品化方案是放到受认证的访问层之后。

## 生产前必须补的安全项

当前 Docker provider 直接调用宿主 Docker CLI，适合单机 MVP，不适合多租户生产。生产版本改为 Kubernetes provider，控制面只持有 namespace-scoped ServiceAccount，不挂 Docker socket。

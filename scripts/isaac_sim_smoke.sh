#!/usr/bin/env bash
set -euo pipefail
# =============================================================================
# Gate G2 — Isaac Sim GPU headless 渲染冒烟（scripts/isaac_sim_smoke.sh）
#
# 用法：./scripts/isaac_sim_smoke.sh
#
# 验收级别说明（重要，严禁混为一谈）：
#   本脚本是"部署就绪、真实 GPU 验收待执行"的验收工具，只应在装有
#   NVIDIA GPU + Docker + NVIDIA Container Toolkit 的宿主机上运行。
#   脚本如实报告 PASS/FAIL；若宿主机缺依赖则输出 BLOCKED_EXTERNAL_DEPENDENCY
#   并以非零码退出。PHYSICAL_GPU_VALIDATION_PENDING 状态由执行者维护：
#   只要尚未在真实 NVIDIA GPU 上跑通 G1–G4，release 产物
#   （dist/VALIDATION_STATUS.md）中的 "GPU Verified" 必须保持 PENDING，
#   绝不允许以本脚本在无 GPU 环境下的运行结果冒充 PASS。
# =============================================================================

cd "$(dirname "$0")/.."

ISAAC_SIM_IMAGE="${ISAAC_SIM_IMAGE:-nvcr.io/nvidia/isaac-sim:6.0.1}"
LOG_DIR="${LOG_DIR:-/tmp/embodiedcloud-gpu-smoke}"
LOG="$LOG_DIR/isaac_sim_smoke_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$LOG_DIR"

say(){ printf '%s\n' "$*"; }

# ---------- 前置检查：docker + nvidia-smi ----------
missing=()
command -v docker >/dev/null 2>&1 || missing+=(docker)
command -v nvidia-smi >/dev/null 2>&1 || missing+=(nvidia-smi)
if (( ${#missing[@]} > 0 )); then
  say "BLOCKED_EXTERNAL_DEPENDENCY: 缺少宿主机前置依赖: ${missing[*]}"
  say "本脚本必须在装有 NVIDIA GPU + Docker + NVIDIA Container Toolkit 的宿主机上执行。"
  say "GPU Verified 保持 PHYSICAL_GPU_VALIDATION_PENDING，不得在本机冒充 PASS。"
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  say "BLOCKED_EXTERNAL_DEPENDENCY: docker daemon 不可用（docker info 失败）"
  exit 2
fi

# ---------- Gate G2：headless 渲染冒烟 ----------
# 覆盖 entrypoint 并指定镜像内官方 headless 入口（兼容 Isaac Sim 6.0.1）。
# --/app/quitAfter=10：渲染 10 帧后自动退出（官方 headless 冒烟参数）。
# ACCEPT_EULA=Y 是 NGC Isaac Sim 镜像的官方运行前提。
say "== Gate G2: Isaac Sim GPU headless 渲染冒烟 =="
say "镜像: $ISAAC_SIM_IMAGE"
say "日志: $LOG"

set +e
docker run --gpus all --rm --network=host \
  -e ACCEPT_EULA=Y \
  "$ISAAC_SIM_IMAGE" \
  ./isaac-sim.headless.sh --/app/quitAfter=10 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
set -e

if (( rc == 0 )); then
  say "G2 PASS: Isaac Sim GPU headless render（退出码 0）"
  say "日志: $LOG"
else
  say "G2 FAIL: Isaac Sim headless render 退出码 $rc"
  say "日志: $LOG"
  exit "$rc"
fi

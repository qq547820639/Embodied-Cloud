#!/usr/bin/env bash
set -euo pipefail
# =============================================================================
# Gate G3 — Isaac Lab Cartpole 训练冒烟（scripts/isaac_lab_cartpole_smoke.sh）
#
# 用法：./scripts/isaac_lab_cartpole_smoke.sh
#
# 使用 workspace 镜像（embodiedcloud/isaaclab-workspace:0.1.0），覆盖 entrypoint
# 为 isaaclab CLI，并把临时工作目录挂载到 /workspace/project，执行：
#   isaaclab train --rl_library rsl_rl --task Isaac-Cartpole-Direct \
#     --num_envs 16 presets=newton_mjwarp --max_iterations 5
# 验收条件：退出码 0，且训练日志 grep -i nan 命中数 0（无 NaN loss/NaN reward）。
#
# 验收级别说明同 scripts/isaac_sim_smoke.sh：真实 GPU 验收待执行，
# PHYSICAL_GPU_VALIDATION_PENDING 状态由执行者维护，严禁冒充 PASS。
# =============================================================================

cd "$(dirname "$0")/.."

WORKSPACE_IMAGE="${WORKSPACE_IMAGE:-embodiedcloud/isaaclab-workspace:0.1.0}"
LOG_DIR="${LOG_DIR:-/tmp/embodiedcloud-gpu-smoke}"
LOG="$LOG_DIR/isaac_lab_cartpole_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$LOG_DIR"

WORKDIR="$(mktemp -d)"
chmod 777 "$WORKDIR"
trap 'rm -rf "$WORKDIR"' EXIT

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

# ---------- Gate G3：Cartpole 训练冒烟 ----------
say "== Gate G3: Isaac Lab Cartpole 训练冒烟 =="
say "镜像: $WORKSPACE_IMAGE"
say "日志: $LOG"

set +e
docker run --gpus all --rm --network=host \
  -e ACCEPT_EULA=Y \
  --entrypoint isaaclab \
  -v "$WORKDIR:/workspace/project" \
  -w /workspace/project \
  "$WORKSPACE_IMAGE" \
  train --rl_library rsl_rl --task Isaac-Cartpole-Direct --num_envs 16 presets=newton_mjwarp --max_iterations 5 \
  2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
set -e

nan_count=0
if [[ -s "$LOG" ]]; then
  nan_count=$(grep -ic nan "$LOG" || true)
fi

if (( rc != 0 )); then
  say "G3 FAIL: isaaclab train 退出码 $rc"
  say "日志: $LOG"
  exit "$rc"
fi
if (( nan_count > 0 )); then
  say "G3 FAIL: 训练日志中发现 $nan_count 处 NaN 命中（loss/reward 异常）"
  say "日志: $LOG"
  exit 1
fi
say "G3 PASS: Isaac Lab Cartpole smoke（退出码 0，训练日志无 NaN）"
say "日志: $LOG"

#!/usr/bin/env bash
set -euo pipefail
# This script performs the host-side checks we can automate before the three
# product-template gates are exercised from the browser IDE.

cd "$(dirname "$0")/.."
./scripts/preflight_gpu_host.sh

echo
printf '%s\n' '== NVIDIA Isaac Sim 6.0.1 compatibility checker =='
# 同一个钉死引用（与 runtime/Dockerfile.isaaclab-workspace 的 FROM 逐字相等）
docker run --entrypoint bash --gpus all --rm --network=host \
  "${ISAAC_SIM_IMAGE:-nvcr.io/nvidia/isaac-sim:6.0.1@sha256:783444c706538aa76cf5126e911ddc5e618779e6105305ad4af4260362a30aa9}" \
  ./isaac-sim.compatibility_check.sh --/app/quitAfter=10 --no-window

echo
printf '%s\n' '== EmbodiedCloud workspace image presence =='
if docker image inspect embodiedcloud/isaaclab-workspace:0.1.0 >/dev/null 2>&1; then
  docker image inspect embodiedcloud/isaaclab-workspace:0.1.0 \
    --format 'image={{.Id}} created={{.Created}} size={{.Size}}'
else
  echo 'Workspace image not built. Run: ./scripts/build_workspace_image.sh' >&2
  exit 2
fi

echo
printf '%s\n' 'Host acceptance preflight passed. Continue with G1/G2/G3 in docs/GPU_HOST.md.'

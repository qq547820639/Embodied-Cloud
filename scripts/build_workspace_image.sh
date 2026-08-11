#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker build -f runtime/Dockerfile.isaaclab-workspace -t embodiedcloud/isaaclab-workspace:0.1.0 .

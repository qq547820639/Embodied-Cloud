#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${EMBODIEDCLOUD_HOST_PUBLIC_IP:?Set EMBODIEDCLOUD_HOST_PUBLIC_IP to the GPU host LAN/public IP}"
export EMBODIEDCLOUD_PROVIDER=docker
export EMBODIEDCLOUD_EULA_ACCEPTED=true
export EMBODIEDCLOUD_WORKSPACE_ROOT="${EMBODIEDCLOUD_WORKSPACE_ROOT:-/var/lib/embodiedcloud/workspaces}"
python -m app.main

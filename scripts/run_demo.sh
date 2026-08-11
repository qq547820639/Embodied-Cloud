#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export EMBODIEDCLOUD_PROVIDER=mock
export EMBODIEDCLOUD_DATABASE_URL="sqlite:///./embodiedcloud-demo.db"
export EMBODIEDCLOUD_WORKSPACE_ROOT="${EMBODIEDCLOUD_WORKSPACE_ROOT:-/tmp/embodiedcloud-demo-workspaces}"
python -m app.main

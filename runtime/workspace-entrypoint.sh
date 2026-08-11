#!/usr/bin/env bash
set -euo pipefail

if [[ "${ACCEPT_EULA:-}" != "Y" ]]; then
  echo "ACCEPT_EULA=Y is required to run the NVIDIA Isaac Sim based workspace." >&2
  exit 2
fi

export PASSWORD="${WORKSPACE_PASSWORD:-change-me}"
IDE_PORT="${IDE_PORT:-18000}"
mkdir -p /workspace/project /workspace/home

cat > /workspace/project/EMBODIEDCLOUD.txt <<EOF
EmbodiedCloud workspace is ready.

Isaac Lab checkout: /workspace/IsaacLab
Streaming mode: ${LIVESTREAM:-0}
Public host: ${PUBLIC_IP:-127.0.0.1}

Open README.md for the selected template command.
EOF

exec code-server \
  --bind-addr "0.0.0.0:${IDE_PORT}" \
  --auth password \
  --disable-telemetry \
  --app-name "EmbodiedCloud" \
  /workspace/project

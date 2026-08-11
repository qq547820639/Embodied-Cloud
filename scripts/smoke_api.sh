#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
curl -fsS "$BASE/api/health" | python -m json.tool
curl -fsS "$BASE/api/templates" | python -m json.tool | head -80
ID=$(curl -fsS -X POST "$BASE/api/workspaces" -H 'Content-Type: application/json' -d '{"template_id":"newton-cartpole-smoke","auto_start":true}' | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')
sleep 1
curl -fsS "$BASE/api/workspaces/$ID" | python -m json.tool

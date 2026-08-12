#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
PYTHON_BIN="${PYTHON:-python3}"
if [[ -x .venv/bin/python ]]; then PYTHON_BIN=".venv/bin/python"; fi

# 若服务未运行则自启动（CI/无人环境可用），结束后清理。
SERVER_PID=""
if ! curl -fsS "$BASE/api/health" >/dev/null 2>&1; then
  EMBODIEDCLOUD_PROVIDER=mock EMBODIEDCLOUD_DATABASE_URL=sqlite:///./smoke-embodiedcloud.db \
    "$PYTHON_BIN" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >/tmp/embodiedcloud-smoke.log 2>&1 &
  SERVER_PID=$!
  trap 'kill "$SERVER_PID" 2>/dev/null || true; rm -f smoke-embodiedcloud.db' EXIT
  for _ in $(seq 1 40); do
    curl -fsS "$BASE/api/health" >/dev/null 2>&1 && break
    sleep 0.25
  done
fi

curl -fsS "$BASE/api/health" | "$PYTHON_BIN" -m json.tool
curl -fsS "$BASE/api/templates" | "$PYTHON_BIN" -m json.tool | head -80
ID=$(curl -fsS -X POST "$BASE/api/workspaces" -H 'Content-Type: application/json' -d '{"template_id":"newton-cartpole-smoke","auto_start":true}' | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["id"])')
sleep 1
curl -fsS "$BASE/api/workspaces/$ID" | "$PYTHON_BIN" -m json.tool
echo "SMOKE_OK workspace=$ID"

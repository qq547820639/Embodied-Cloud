#!/usr/bin/env bash
set -u
fail=0
say(){ printf '%-38s %s\n' "$1" "$2"; }
check(){ if command -v "$1" >/dev/null 2>&1; then say "$1" "OK"; else say "$1" "MISSING"; fail=1; fi; }

check docker
check nvidia-smi
check curl
check ss

if command -v docker >/dev/null 2>&1; then
  docker info >/dev/null 2>&1 && say "docker daemon" "OK" || { say "docker daemon" "UNAVAILABLE"; fail=1; }
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv,noheader || fail=1
fi

if ldconfig -p 2>/dev/null | grep -q libnvidia-encode; then
  say "NVENC library" "OK"
else
  say "NVENC library" "NOT FOUND (WebRTC streaming unavailable)"
fi

if command -v ss >/dev/null 2>&1; then
  for p in 8000 18000 49100; do
    if ss -ltn 2>/dev/null | grep -Eq ":${p}([[:space:]]|$)"; then say "TCP $p" "IN USE"; else say "TCP $p" "FREE"; fi
  done
  if ss -lun 2>/dev/null | grep -Eq ":47998([[:space:]]|$)"; then say "UDP 47998" "IN USE"; else say "UDP 47998" "FREE"; fi
fi

if [[ $fail -ne 0 ]]; then echo "Preflight failed." >&2; exit 1; fi
echo "Preflight passed. Next: run the NVIDIA Isaac Sim compatibility checker from docs/GPU_HOST.md."

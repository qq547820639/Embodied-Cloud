#!/usr/bin/env bash
# 在**真 amd64 运行时**里复算控制面镜像的依赖层（生产主机是 x86_64，本机是 arm64）。
# 人工/CI 档，不进每轮 validate：它要在模拟环境里下载并安装整组 wheel。
# 为什么不用 `docker build --platform`（本机实测，详见 docs/OPERATIONS.md 同一条）：这台机器的
# docker 29 没有 buildx 插件，退回 legacy builder，而 legacy builder 不把 --platform 传进中间容器
# （日志里的原话是 "…and no specific platform was requested"），于是 builder 阶段其实按 arm64 跑完，
# 到 COPY --from 时被判"不提供 linux/amd64"。那条路要 BuildKit 才成立，不在本轮范围。
set -uo pipefail
cd "$(dirname "$0")/.."

BASE="python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"
UV="ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424"
SCRATCH="/Volumes/Extra/qoder-scratch/amd64probe"
NAME="amd64-probe-$$"

mkdir -p "$SCRATCH"
rm -f "$SCRATCH/uv"

echo "== 1) 取出 uv 二进制（从钉死的那份工具镜像里，只读，不装到宿主）"
CID=$(docker create --platform linux/amd64 "$UV" true) || { echo "docker create uv 失败"; exit 2; }
docker cp "$CID:/uv" "$SCRATCH/uv" || { echo "取 /uv 失败"; docker rm -f "$CID" >/dev/null; exit 2; }
docker rm -f "$CID" >/dev/null
file "$SCRATCH/uv" | sed 's/^/   /'

echo "== 2) 起一个 amd64 的 BASE 容器（qemu 模拟，只为本轮验证）"
docker run -d --name "$NAME" --platform linux/amd64 --entrypoint sleep "$BASE" 3600 >/dev/null || { echo "起容器失败"; exit 2; }
echo "   容器内架构 = $(docker exec "$NAME" uname -m)  python = $(docker exec "$NAME" python -V 2>&1)"

echo "== 3) 送进配方要送的两份文件（pyproject.toml + uv.lock）与 uv 二进制"
docker exec "$NAME" mkdir -p /app
docker cp pyproject.toml "$NAME":/app/pyproject.toml
docker cp uv.lock "$NAME":/app/uv.lock
docker cp "$SCRATCH/uv" "$NAME":/usr/local/bin/uv
docker exec "$NAME" chmod +x /usr/local/bin/uv

echo "== 4) 跑与 Dockerfile 里同一行命令"
docker exec -w /app \
  -e UV_LINK_MODE=copy -e UV_PYTHON_DOWNLOADS=0 \
  "$NAME" uv sync --frozen --no-dev --extra postgres --no-install-project --no-editable 2>&1 | tail -8
echo "sync_rc=${PIPESTATUS[0]}"

echo "== 5) 验收：装的是 x86_64 轮子吗，两个 C 扩展能 import 吗"
docker exec -w /app -e PATH="/app/.venv/bin:/usr/local/bin:/usr/bin:/bin" "$NAME" sh -c '
  python -c "import platform, sqlalchemy, psycopg; print(\"machine=\", platform.machine(), \"sqlalchemy=\", sqlalchemy.__version__, \"psycopg=\", psycopg.__version__)"
  ls /app/.venv/lib/python3.12/site-packages | grep -c . | sed "s/^/site-packages 条目= /"
  ls /app/.venv/lib/python3.12/site-packages | grep -oE "x86_64|aarch64|arm64" | sort | uniq -c
  alembic --version
' 2>&1 | tail -12

echo "== 6) 收尾"
docker rm -f "$NAME" >/dev/null && echo "容器已清（$NAME）"
rm -f "$SCRATCH/uv"

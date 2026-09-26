#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${WORKSPACE_IMAGE:-embodiedcloud/isaaclab-workspace:0.1.0}"
docker build -f runtime/Dockerfile.isaaclab-workspace -t "$IMAGE" .

# 构建产物 → TemplateVersion.image_digest 的回填入口（SUPPLY_CHAIN §8 第 1 条）。
# 摘要取自 docker 的镜像 ID：本机实测 `docker image inspect --format {{.Id}}` 与
# .RepoDigests[0] 给出同一个 sha256（本地构建未推送的 scratch 镜像、以及拉取来的
# postgres:16-alpine 两例皆如此），也就是这份 manifest 的内容摘要。
image_id=$(docker image inspect "$IMAGE" --format '{{.Id}}')
if [ -z "$image_id" ]; then
  # 宁可让这一列留 NULL，也不写一个"看起来像 digest"的字符串：NULL 会让 workspace
  # 快照退回可变 tag（如实的不保证），坏字符串会让 app.services.image_ref 直接拒绝启动。
  echo "拿不到镜像摘要，拒绝回填" >&2
  exit 2
fi
printf 'built %s\n  digest=%s\n' "$IMAGE" "$image_id"
# 回填是**显式**一步，不在这里静默改已发布版本（§15：released 版本不可变；
# 同一 tag 指向新内容应当发布新版本，而不是就地改写这一行）。
printf '  回填：python -m app.cli record-image-digest --template-id <id> --version <ver> --digest %s\n' "$image_id"

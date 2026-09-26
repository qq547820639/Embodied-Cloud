#!/usr/bin/env bash
# 控制面镜像的**镜像层漏洞扫描**（SUPPLY_CHAIN §8 第 5 项）。
# 与 `make audit`（uv）的分工：那一条只看 Python 依赖，看不见基础镜像里 Debian 层的东西；
# 本机实测这一层今天有 156 条命中，而 wheel 层只有 6 条——不跑这一步，OS 层就是盲区。
#
# 这一步**只出报告、不当门禁**：分诊已做完，量出的结论是阈值不可用——44 条 HIGH 塌成
# 17 个二进制包 / 8 个 CVE，其中 43 条 `affected`（上游没发修复版本）、1 条 Debian 标
# `fix_deferred`；`FixedVersion` 这个键在 156 条 OS 层命中里一条都没有。完整读数见 SUPPLY_CHAIN §8 第 5 项。
set -euo pipefail
cd "$(dirname "$0")/.."

# 工具镜像与漏洞库通道都只写一份（含三通道实测读数），在这里 source 进来。
. "$(dirname "$0")/trivy_tool_ref.sh"
IMAGE="${CONTROL_IMAGE:-embodiedcloud/control-plane:0.7.0}"
OUT="${CVE_OUT:-dist/vulns.image.json}"
LOG="${CVE_LOG:-dist/vulns.image.log}"
CACHE="${TRIVY_CACHE_DIR:-$PWD/.trivy-cache}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[image-cve] 找不到被审镜像 $IMAGE；先跑 make control-image（预检失败退 2，不交空报告）" >&2
  exit 2
fi

mkdir -p "$(dirname "$OUT")" "$CACHE"
trap 'rm -f "$OUT.tmp" "$LOG.tmp"' EXIT
# 与 image_sbom.sh 同理：结果走 stdout，不依赖容器内挂载路径的可见性。
# 漏洞库**不能钉 digest**——钉住就等于每天拿一份过期的库去说"没有漏洞"；
# 所以这里显式指定一条到得了的库通道，并把 trivy 自己关于库的日志留在 $LOG 里做溯源。
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$CACHE:/root/.cache/" \
  "$TRIVY_IMAGE" image --scanners vuln --skip-version-check \
  --db-repository "$TRIVY_DB_REPOSITORY" --format json "$IMAGE" > "$OUT.tmp" 2> "$LOG.tmp"
mv "$OUT.tmp" "$OUT"
mv "$LOG.tmp" "$LOG"
trap - EXIT

# 报告必须说得出它扫的是哪个镜像的字节：与 inspect 的 .Id 逐字比。
.venv/bin/python - "$OUT" "$(docker image inspect --format '{{.Id}}' "$IMAGE")" <<'PY'
import collections
import json
import sys

path, image_id = sys.argv[1], sys.argv[2]
doc = json.load(open(path, encoding="utf-8"))
declared = (doc.get("Metadata") or {}).get("ImageID", "")
if declared != image_id:
    sys.exit(f"[image-cve] ✗ 报告自报的 ImageID={declared!r} 与 inspect 的 {image_id!r} 不符——扫的不是这个镜像")

sev: collections.Counter[str] = collections.Counter()
per_class: dict[str, int] = {}
for res in doc.get("Results") or []:
    vulns = res.get("Vulnerabilities") or []
    per_class[res.get("Class") or res.get("Type") or "?"] = len(vulns)
    sev.update(v.get("Severity") or "UNKNOWN" for v in vulns)
print(f"[image-cve] {doc.get('ArtifactName')} ImageID={declared[:19]}…")
print(f"[image-cve] 分来源 {per_class}；按等级 {dict(sev)}；合计 {sum(sev.values())} 条")
print("[image-cve] 这一档只出报告：分诊已完成，44 条 HIGH 里 43 affected＋1 fix_deferred、无一条带 FixedVersion，接阈值只会得到每次必红却无从修的红；漏洞库按设计不钉 digest")
PY

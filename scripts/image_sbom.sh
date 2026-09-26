#!/usr/bin/env bash
# 控制面镜像的**镜像层** SBOM（SUPPLY_CHAIN §8 第 3 条）。
# 与 `make sbom` 的区别：那份是 uv 从锁文件导出的 wheel 清单，看不见基础镜像里的 Debian 包，
# 也看不见"实际装进镜像的那一份"是什么——这一步两样都出，并且末了拿 uv.lock 逐个核对本步骤
# 自己量出的漂移（N-22）。
set -euo pipefail
cd "$(dirname "$0")/.."

# 工具镜像的钉法只有一份（与 make image-cve 共用），三通道实测读数与理由记在那里。
. "$(dirname "$0")/trivy_tool_ref.sh"
IMAGE="${CONTROL_IMAGE:-embodiedcloud/control-plane:0.7.0}"
OUT="${IMAGE_SBOM_OUT:-dist/sbom.image.cdx.json}"
PYTHON="${PYTHON:-.venv/bin/python}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[image-sbom] 找不到被审镜像 $IMAGE；先跑 make control-image（预检失败退 2，不当成产物缺失）" >&2
  exit 2
fi

mkdir -p "$(dirname "$OUT")"
# 失败也要扫掉半成品：读侧不该看见上一次没做完的那份 JSON。
trap 'rm -f "$OUT.tmp"' EXIT
# 两个刻意的选择，都记在这里免得下一个人重新踩：
# 1) 结果走 stdout 重定向，不用 trivy 的 --output 指到挂载路径：这台机器（colima）的 /tmp
#    **不是共享进虚拟机的挂载点**，实测容器内写 /tmp 挂载点里的文件宿主看不见（同一分钟内
#    把挂载点换成工程目录下的 dist/ 子目录就立刻可见）。与其依赖一条随时会变的共享目录约定，
#    不如让产物由宿主自己写。
# 2) 先写 .tmp 再 mv：读侧永远看不到半份 JSON。
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  "$TRIVY_IMAGE" image --format cyclonedx "$IMAGE" > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
trap - EXIT

# 落盘之后必须过形状判据（scripts/check_image_sbom.py --self-test 证它每条都能开火）。
# --image-id 是"扫错对象"的真防线：清单自报的摘要必须等于 inspect 这个镜像拿到的 Id。
# 本机实测三处同值——`.Id`＝trivy 的 ImageID 属性＝purl 里的摘要＝sha256:cd371b31…；
# 只比名字的话比的是 trivy 回声它自己收到的命令行参数，比不出字节。
# --lock 是 N-22 那把闸：镜像里每个 wheel 都必须由 uv.lock 那一行决定。旧配方
# `pip install ".[postgres]"` 现解析时，这条一跑就红（镜像 sqlalchemy 2.1.1 vs 锁 2.1.0）；
# 改配方之后实测 47 个 pypi 组件里 46 个逐名逐版本命中锁，剩下的一个是基础镜像自带的 pip
# （判据里那张白名单只认它，改名即红）。基准路径由这里显式传，判据不猜。
"$PYTHON" scripts/check_image_sbom.py "$OUT" "$IMAGE" \
  --image-id "$(docker image inspect --format '{{.Id}}' "$IMAGE")" \
  --lock uv.lock
echo "[image-sbom] 产物 $OUT —— 这份是**清单**，不是「没有漏洞」的结论：--format cyclonedx 按 trivy 自己的日志会关掉安全扫描"

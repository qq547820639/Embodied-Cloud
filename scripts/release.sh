#!/usr/bin/env bash
set -euo pipefail
# =============================================================================
# semver release（scripts/release.sh）
#
# 用法：./scripts/release.sh [VERSION]
#   - VERSION 缺省时读取 pyproject.toml 的 [project].version（优先用
#     .venv/bin/python 解析，其次 python3 / python；老版本 Python 无 tomllib
#     时回退正则解析）。
#   - 若传入 VERSION 与 pyproject.toml 不一致 → 中止。版本号修改只允许由
#     开发者在 pyproject.toml 完成，本脚本不自动改版本。
#
# 流程：
#   1. make lint && make typecheck && make test（任一失败中止并输出原因）
#   2. make build 生成 dist/*.whl 与 dist/*.tar.gz
#   2.4 make verify-lock / sbom / audit（uv.lock 一致性 + CycloneDX SBOM + 漏洞审计）
#   2.44 make validate（重生成 docs/VALIDATION.json，分级矩阵的唯一事实源）
#   3. 生成 dist/checksums.txt（对 wheel / sdist / sbom.cdx.json 计算 sha256sum）
#   4. 生成 dist/VALIDATION_STATUS.md（分级验证矩阵 + BLOCKED_EXTERNAL_DEPENDENCY 明细）
#   5. 输出 release 产物清单，并提示 git tag（不自动打 tag、不 push）
# =============================================================================

cd "$(dirname "$0")/.."

step(){ printf '\n========== %s ==========\n' "$*"; }
say(){ printf '%s\n' "$*"; }

# ---------- 选择 Python 解释器（优先 .venv/bin/python） ----------
PYTHON=""
for cand in .venv/bin/python python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PYTHON="$cand"; break; fi
done
if [[ -z "$PYTHON" ]]; then
  say "release FAILED: 未找到 Python 解释器（尝试过 .venv/bin/python、python3、python）" >&2
  exit 1
fi
say "使用 Python: $PYTHON"

# ---------- 读取 pyproject.toml 的 version ----------
read_pyproject_version() {
  "$PYTHON" - <<'PY'
import re
import sys

try:
    import tomllib  # Python 3.11+

    with open("pyproject.toml", "rb") as f:
        print(tomllib.load(f)["project"]["version"])
except Exception:
    for line in open("pyproject.toml", encoding="utf-8"):
        m = re.match(r'^\s*version\s*=\s*"([^"]+)"', line)
        if m:
            print(m.group(1))
            sys.exit(0)
    sys.exit(1)
PY
}

PYPROJECT_VERSION="$(read_pyproject_version)"
say "pyproject.toml 版本: $PYPROJECT_VERSION"

VERSION="${1:-$PYPROJECT_VERSION}"
if [[ "$VERSION" != "$PYPROJECT_VERSION" ]]; then
  say "release FAILED: 传入版本 '$VERSION' 与 pyproject.toml 版本 '$PYPROJECT_VERSION' 不一致" >&2
  say "版本号修改由开发者在 pyproject.toml 完成（脚本不允许改版本）。请先更新 pyproject.toml 再发布。" >&2
  exit 1
fi
say "发布版本: $VERSION"

trap 'say "release ABORTED: 前置 Gate 或构建失败，原因见上方输出" >&2' ERR

# ---------- 1. G0 软件 Gate（lint / typecheck / test） ----------
step "1/5 make lint"
make lint
step "2/5 make typecheck"
make typecheck
step "3/5 make test"
make test

# ---------- 2. build（清空 dist，确保产物只属于本次版本） ----------
step "4/5 make build"
rm -rf dist
make build

# ---------- 2.4 供应链：锁一致性 / SBOM / 漏洞审计 ----------
step "4.1 make verify-lock"
make verify-lock
step "4.2 make sbom + make audit"
make sbom
make audit

# ---------- 2.44 分级验证矩阵的唯一事实源：docs/VALIDATION.json ----------
step "4.3 make validate（重生成 docs/VALIDATION.json / .md）"
# 无条件重跑，不按"版本一致就复用"跳过：本轮实测过——发布链跑完后又加了一条判据用例，
# 版本号没变，于是"版本一致"的判断让发布链复用了比工作树少一条用例的旧报告，
# 而这正是 CI 的 `make validate && git diff --exit-code` 会红的形状。
make validate
VALIDATION_ROWS="$("$PYTHON" - <<'PY'
import json

checks = json.load(open("docs/VALIDATION.json", encoding="utf-8"))["checks"]
rows = []
for key in sorted(checks):
    if not key.startswith("integration_"):
        continue
    cell = checks[key]
    note = str(cell.get("note", cell.get("count", ""))).replace("|", "/")
    rows.append(f"| {key} | {cell['status']} | {note} |")
assert rows, "docs/VALIDATION.json 里没有任何 integration_* 档位行（判据会恒空）"
run = checks["test_run"]
print(f"| test_run | {run['status']} | passed {run['passed']} / skipped {run['skipped']} / failed {run['failed']} |")
print("\n".join(rows))
PY
)"
say "已从 docs/VALIDATION.json 取到 $(printf '%s' "$VALIDATION_ROWS" | grep -c '^|') 行档位读数"

# ---------- 2.5 release archive 清洁验证（§16） ----------
step "2.5 校验 release archive 清洁度"
# source archive 不得包含：__pycache__ / *.pyc / pytest/mypy/ruff cache /
# test-*.db / .env / .venv / .workbuddy
BAD_PATTERNS=(__pycache__ '*.pyc' .pytest_cache .mypy_cache .ruff_cache 'test-*.db' .env .venv .workbuddy)
tar_gz=""
for f in dist/*.tar.gz; do [[ -f "$f" ]] && tar_gz="$f"; done
if [[ -n "$tar_gz" ]]; then
  unclean=""
  for pat in "${BAD_PATTERNS[@]}"; do
    hits=$(tar -tzf "$tar_gz" 2>/dev/null | grep -c "$pat" || true)
    if (( hits > 0 )); then
      unclean="$unclean $pat($hits)"
    fi
  done
  if [[ -n "$unclean" ]]; then
    say "release FAILED: source archive 包含不应发布的条目:$unclean" >&2
    exit 1
  fi
  say "source archive 清洁度 OK（无 __pycache__/pyc/cache/test-db/.env/.venv/.workbuddy）"
else
  say "警告: 未找到 *.tar.gz，跳过 archive 清洁度检查" >&2
fi

# ---------- 3. dist/checksums.txt ----------
step "生成 dist/checksums.txt"
hash_cmd="sha256sum"
if ! command -v sha256sum >/dev/null 2>&1; then
  hash_cmd="shasum -a 256"   # macOS 兼容
fi
: > dist/checksums.txt
count=0
for f in dist/*.whl dist/*.tar.gz dist/sbom.cdx.json; do
  [[ -f "$f" ]] || continue
  $hash_cmd "$f" >> dist/checksums.txt
  count=$((count + 1))
done
if (( count == 0 )); then
  say "release FAILED: dist/ 下没有 *.whl / *.tar.gz，make build 未产出工件" >&2
  exit 1
fi
say "已为 $count 个构建工件生成 SHA-256:"
cat dist/checksums.txt

# ---------- 4. dist/VALIDATION_STATUS.md（分级验证矩阵） ----------
step "生成 dist/VALIDATION_STATUS.md"

# 预构建产物清单。注意：必须用 ${f} 花括号形式，避免 bash 把 $f 与紧随的
# 全角字符（（ ）误解析为变量名（set -u 下会报 unbound variable）。
ARTIFACTS="$(for f in dist/*.whl dist/*.tar.gz dist/sbom.cdx.json; do [[ -f "$f" ]] && echo "- ${f}（SHA-256 见 dist/checksums.txt）"; done)"

cat > dist/VALIDATION_STATUS.md <<EOF
# VALIDATION_STATUS — embodiedcloud v${VERSION}

- 生成时间（UTC）：$(date -u +%Y-%m-%dT%H:%M:%SZ)
- 生成脚本：scripts/release.sh
- 版本来源：pyproject.toml（本脚本不修改版本号）

## 分级验证矩阵（严禁混为一谈）

| 级别 | 状态 | 说明 |
|---|---|---|
| Software Verified | ✅ VERIFIED | 本环境/CI 实际执行 lint / typecheck / test / build 全部通过（明细见下） |
| GPU Verified | ⏳ PENDING | 需真实 NVIDIA GPU 运行 G1–G4（scripts/preflight_gpu_host.sh、scripts/gpu_acceptance.sh、scripts/isaac_sim_smoke.sh、scripts/isaac_lab_cartpole_smoke.sh、scripts/franka_smoke.sh）；当前 PHYSICAL_GPU_VALIDATION_PENDING |
| Streaming Verified | ⏳ PENDING | 需真实 Isaac Sim WebRTC 链路验证（49100/TCP + 47998/UDP） |
| Physical Robot Verified | ⏳ PENDING | 需真实机器人 Sim2Real 验证（Gate G5） |

> 本版本为"部署就绪、物理验证待执行"状态。GPU / Streaming / Physical Robot
> 三项在真实硬件验证完成前必须保持 PENDING，绝不允许冒充 PASS。

## Software Verified 明细（本次实际执行）

| Gate | 命令 | 结果 |
|---|---|---|
| lint | make lint | PASS（退出码 0） |
| typecheck | make typecheck | PASS（退出码 0） |
| test | make test | PASS（退出码 0） |
| 锁文件一致性 | make verify-lock | PASS（uv.lock 与 pyproject 一致） |
| SBOM | make sbom | dist/sbom.cdx.json（CycloneDX 1.5） |
| 依赖漏洞审计 | make audit | PASS（uv audit --locked，0 命中） |
| PostgreSQL 真并发 | make test-pg | 见下方"集成档读数"（由 docs/VALIDATION.json 生成） |
| build | make build | PASS（退出码 0） |

### 集成档读数（逐行取自 docs/VALIDATION.json，不在本脚本里手抄）

| 档位 | 状态 | 读数 |
|---|---|---|
${VALIDATION_ROWS}

## 构建产物（dist/）

${ARTIFACTS}

## BLOCKED_EXTERNAL_DEPENDENCY 明细

| 依赖 | 状态 | 影响 |
|---|---|---|
| Docker daemon | 本环境可用（colima） | 容器档集成测试可跑（make test-pg / docker 档） |
| NVIDIA GPU + NVIDIA Container Toolkit | 本机为 Apple Silicon，无 CUDA | G1–G4 无法执行（非软件缺陷）；容器参数层已由 docker 档在真守护进程上验收（回读 HostConfig.DeviceRequests），设备可见性仍待真机 |
| NGC（nvcr.io/nvidia/isaac-sim:6.0.1） | 需 NGC 凭据 + x86 GPU 主机 | G2–G4 无法执行；构建配方内的下载/克隆已钉死并机检 |
| 云对象存储真实账号 | 本机无凭据 | S3 协议语义已由自起的真服务端（VersityGW）覆盖，并用 MinIO 交叉核对；缺的只是云厂商那份实现 |
| Kubernetes 集群 + Device Plugin | 控制面已由 kind 自起真集群覆盖（make test-k8s-control-plane）；节点带 nvidia.com/gpu 容量仍需 Device Plugin | G0.17 / G1 K8s GPU 全流程保持 PENDING |
| 真实机器人硬件 | 本环境无真机 | G5 Sim2Real 无法执行 |

## 待办（开发者人工执行）

- [ ] 在真实 GPU 主机完成 G1–G4 并更新本文件为 VERIFIED
- [ ] 更新 docs/CURRENT_STATE.md、docs/ACCEPTANCE_GATES.md
- [ ] 手工 git tag v${VERSION} 并 push（本脚本不自动打 tag、不 push）
EOF
# 反引号在这个未加引号的 heredoc 里会被 bash 当命令替换**执行掉**：实测有人写了
# `make test-k8s-control-plane` 之后，发布脚本真的又跑了一遍那个档位并把 pytest 的
# 收尾行（含 "7 passed, 451 deselected ... in 64.66s"）抄进了发布工件，而脚本本身
# 一声不响。写完之后强制检查产物，不再靠人眼。
if grep -q '`' dist/VALIDATION_STATUS.md || grep -qE 'deselected|warnings summary' dist/VALIDATION_STATUS.md; then
  say "release FAILED: dist/VALIDATION_STATUS.md 含反引号或用例运行输出 ⇒ heredoc 里有片段被 bash 执行了" >&2
  exit 1
fi
say "已生成 dist/VALIDATION_STATUS.md"

# ---------- 5. 产物清单 + git tag 提示 ----------
step "release v${VERSION} 产物清单"
ls -lh dist/
say ""
say "release v${VERSION} 完成。"
say "下一步（人工执行，本脚本不自动打 tag / push）："
say "  git add CHANGELOG.md docs/ dist/ && git commit -m \"release v${VERSION}\""
say "  git tag v${VERSION}"
say "  git push origin v${VERSION}"

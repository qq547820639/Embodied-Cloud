"""发布产物可复算性的探针：每类产物连建 N 次，sha 全等才算 `recomputable=yes`。

为什么要它（N-34）：`dist/checksums.txt` 记 wheel / sdist 的 SHA-256。第三方拿到那行 sha，
能不能自己重建出一模一样的字节？2026-09-27 一手实测（同一棵树立两次，`SOURCE_DATE_EPOCH` 固定）：
setuptools 84.0.0 的 wheel 相同（`2992a47a…`）、sdist 不同（`f0dad9e2…` vs `90e84be2…`）；
`uv build --no-build-isolation`（同一后端）wheel 相同、sdist 仍不同；hatchling 同样 sdist 不同；
flit_core 两者都相同——但它只认"与 project.name 同名的单个模块/包"，本仓发行的是
`app*` + `edge_agent*` 两个顶层包，不是一处改名能迁的模型。

所以**不换后端**，改成让主张可被推翻：`recomputable=` 的值由本脚本现测写进
`dist/checksums.manifest`，`check()` 再把"清单声明"与"本轮实测"双向对账。
两个方向的偏离都算红：把不可复算的说成可复算＝假承诺；上游修好后清单还写着 no＝过期悲观。
本脚本不把"sdist 不可复算"钉成基线（那等于把缺陷焊死）。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from build_env import epoch_env

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = {"wheel": "*-py3-none-any.whl", "sdist": "*.tar.gz"}


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifacts_in(outdir: Path) -> dict[str, Path]:
    """一次构建的产物：每类必须恰好一个，多一个少一个都是读数无意义。"""
    found: dict[str, Path] = {}
    for kind, pattern in PATTERNS.items():
        hits = sorted(outdir.glob(pattern))
        if len(hits) != 1:
            raise RuntimeError(f"{pattern} 在 {outdir} 下命中 {len(hits)} 个（要求恰好 1 个）")
        found[kind] = hits[0]
    return found


def build_once(outdir: Path) -> dict[str, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    env = epoch_env(dict(__import__("os").environ))
    res = subprocess.run(  # noqa: S603 受控常量参数：本机 venv 的 build 模块
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(outdir)],
        cwd=ROOT, text=True, capture_output=True, env=env, timeout=900,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stdout[-500:] + res.stderr[-500:])
    return artifacts_in(outdir)


def probe(
    runs: int = 2,
    outdir: Path | None = None,
    kinds: tuple[str, ...] = ("wheel", "sdist"),
) -> dict[str, list[str]]:
    """每类产物各建 `runs` 次（每次一份新目录，避免读到上一次的残留）。"""
    if runs < 1:
        raise ValueError("runs 至少 1：零次构建给不出任何主张")
    root = Path(outdir) if outdir else Path(tempfile.mkdtemp(prefix="artifact-probe-"))
    samples: dict[str, list[str]] = {kind: [] for kind in sorted(kinds)}
    for run in range(runs):
        built = build_once(root / f"run{run}")
        for kind in samples:
            samples[kind].append(sha256_of(built[kind]))
    return samples


def recomputable(samples: list[str]) -> str:
    """同类产物的多次 sha 全等 ⇒ yes。空样本一律拒判（分母为 0 与恒真同形）。"""
    if not samples:
        raise ValueError("空样本：无法判可复算，按红处理而不是按绿放行")
    return "yes" if len(set(samples)) == 1 else "no"


def by_filename(verdicts: dict[str, str], names: dict[str, str]) -> dict[str, str]:
    """把「按产物类别」的实测结果换成「按文件名」的键空间。

    清单与 `sha256sum` 那一类文件都按文件名索引，而探针按类别（wheel/sdist）聚合。
    上一版 main() 直接把两个键空间喂给同一个 `check()`，于是每一轮都报"主张缺席 +
    清单里有、本轮没测"——第一次真跑就抓到，说明这一层翻译本身必须有判据（见
    tests/test_artifact_reproducibility.py::test_by_filename_translates_the_key_space）。
    """
    out: dict[str, str] = {}
    for kind, verdict in verdicts.items():
        name = names.get(kind)
        if not name:
            raise ValueError(f"{kind} 没有对应的产物文件名：主张落不进清单")
        out[name] = verdict
    return out


def render_manifest(verdicts: dict[str, str], names: dict[str, str], epoch: str) -> str:
    lines = [
        "# 每个产物的 sha256 能否被第三方重建复算 —— 由 scripts/artifact_reproducibility.py 现测，不手抄。",
        f"# SOURCE_DATE_EPOCH={epoch}",
    ]
    for kind in sorted(verdicts):
        name = names.get(kind)
        if not name:
            raise ValueError(f"{kind} 没有对应的产物文件名，写进清单就是空头主张")
        lines.append(f"{name}\trecomputable={verdicts[kind]}")
    return "\n".join(lines) + "\n"


def parse_manifest(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        for field in parts[1:]:
            if field.startswith("recomputable="):
                out[parts[0]] = field.split("=", 1)[1]
    return out


def check(declared: dict[str, str], measured: dict[str, str]) -> list[str]:
    """主张 ↔ 实测 双向对账（纯函数，可喂夹具）。"""
    out: list[str] = []
    for kind, verdict in sorted(measured.items()):
        got = declared.get(kind)
        if got is None:
            out.append(f"{kind}: 清单里没有这一项（主张缺席，等于没人承诺）")
        elif got != verdict:
            out.append(f"{kind}: 清单声明 recomputable={got}，本轮现测为 {verdict}")
    for kind in sorted(set(declared) - set(measured)):
        out.append(f"{kind}: 清单里有、本轮没测（判据覆盖面在缩小）")
    if not measured:
        out.append("一个产物都没测：这条判据无事可做")
    return out


def main() -> int:
    import os

    runs = 2
    outdir = ROOT / "dist" / "artifact-probe"
    samples = probe(runs=runs, outdir=outdir)
    verdicts = {kind: recomputable(digests) for kind, digests in samples.items()}
    names = {kind: path.name for kind, path in artifacts_in(outdir / f"run{runs - 1}").items()}
    epoch = epoch_env({})["SOURCE_DATE_EPOCH"]
    out = ROOT / "dist" / "checksums.manifest"
    out.parent.mkdir(exist_ok=True)
    out.write_text(render_manifest(verdicts, names, epoch), encoding="utf-8")
    offenders = check(parse_manifest(out.read_text(encoding="utf-8")), by_filename(verdicts, names))
    summary = " ".join(f"{k}={v}" for k, v in sorted(verdicts.items()))
    print(f"[artifacts] {summary}（SOURCE_DATE_EPOCH={os.environ.get('SOURCE_DATE_EPOCH') or epoch}）")
    for kind, digests in sorted(samples.items()):
        print(f"[artifacts]   {kind}: {' '.join(d[:10] for d in digests)}")
    shutil.rmtree(outdir, ignore_errors=True)
    if offenders:
        print("[artifacts] FAIL:\n  " + "\n  ".join(offenders))
        return 1
    print(f"[artifacts] 主张与实测一致 → {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

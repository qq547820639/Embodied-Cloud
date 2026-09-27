"""`make hang-probe`：真造一次"外部依赖卡住"，量每个 docker 档付多少秒、说了什么。

为什么需要它（N-49）：N-48 的挂起实验只覆盖到"版本探测阶段就挂"（它先短路），
"版本通、后续探测挂"那一支我只在登记行里写了个估算（20+30+30=80s/档）却没量过——
估算写进文档就成了没人核的主张。这台子把两种挂起都做成可复跑的取证件：

- `blackhole`：`DOCKER_HOST=tcp://192.0.2.1:2375`（RFC 5737 TEST-NET-1，黑洞：丢包不是拒连）。
- `hang-later`：在 PATH 最前面放一个假 `docker`——`version`/`info` 立刻正常返回，
  其余子命令 `sleep` 到天荒地老，于是每一层探测都被**自己的超时**掐掉。

两种都是真子进程、真超时（不是 monkeypatch）；唯一的替身是超时上限：子进程里把
`subprocess.run(timeout=…)` 的 timeout 夹到 `HANG_PROBE_TIMEOUT_CAP`，这样常驻判据可以按
2 秒跑完，而人手动跑 `make hang-probe` 用真实的 20/30 秒。

每个档位还顺带过一遍 N-48 的可行动性判据（`pending_reason_offenders`）：
挂起时给出的原因必须是"能指着补"的句子，否则这台子自己就是红的。
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

BLACKHOLE_HOST = "tcp://192.0.2.1:2375"  # TEST-NET-1：路由不到，表现为丢包（不是 connection refused）

# 档位 → (提供 gate_reason() 的模块, 它在 skip 闭集里的哨兵)
TIERS = {
    "docker": ("tests.test_docker_provider_integration", "DOCKER_VALIDATION_PENDING"),
    "postgres": ("tests.pg_server", "POSTGRES_VALIDATION_PENDING"),
    "object-store": ("tests.s3_server", "S3_VALIDATION_PENDING"),
    "k8s-control-plane": ("tests.k8s_server", "K8S_CONTROL_PLANE_PENDING"),
}

CHILD = """
import json, subprocess, sys
cap = float({cap!r})
_real = subprocess.run
def run(*args, **kwargs):
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = cap
    else:
        kwargs["timeout"] = min(float(kwargs["timeout"]), cap)
    return _real(*args, **kwargs)
subprocess.run = run
sys.path.insert(0, ".")
import importlib
mod = importlib.import_module({module!r})
print(json.dumps({{"reason": mod.gate_reason()}}))
"""

# 三种剧本，代价差别正是想知道的事：
#   hang-later —— 除 version/info 外全挂（极端：整层探测都在等）
#   half-hang  —— `image inspect` 立刻答"没有"、`image ls` 挂住（真实半挂：只有兜底那条路会等）
FAKE_HANG_LATER = """#!/bin/sh
case "$1 $2" in
  "version "*) echo "Client: Docker Engine (hang-probe fake)"; exit 0 ;;
  "info "*) echo "aarch64"; exit 0 ;;
  *) sleep 3600 ;;
esac
"""
FAKE_HALF_HANG = """#!/bin/sh
case "$1 $2" in
  "version "*) echo "Client: Docker Engine (hang-probe fake)"; exit 0 ;;
  "info "*) echo "aarch64"; exit 0 ;;
  "image ls"*) sleep 3600 ;;
  "image inspect"*) echo "no such image" >&2; exit 1 ;;
  *) exit 1 ;;
esac
"""
FAKES = {"hang-later": FAKE_HANG_LATER, "half-hang": FAKE_HALF_HANG}
HANGING = {
    "hang-later": ["image inspect", "image ls", "pull", "build"],
    "half-hang": ["image ls"],
}


def _fake_docker_dir(tmp: Path, mode: str) -> tuple[Path, list[str]]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "docker"
    script.write_text(FAKES[mode], encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir, ["image", "pull", "build", "inspect", "ls"]


def measure(mode: str, cap_seconds: float) -> dict:
    """逐档在子进程里跑一次 gate_reason()，记录原因与耗时（互不污染环境与进程）。"""
    rows: dict[str, dict] = {}
    injected: dict[str, object] = {"mode": mode, "timeout_cap_seconds": cap_seconds, "hang_subcommands": []}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = dict(os.environ)
        env["HANG_PROBE"] = mode
        env.pop("DOCKER_HOST", None)
        env.pop("EMBODIEDCLOUD_DOCKER_TEST_IMAGE", None)
        if mode == "blackhole":
            env["DOCKER_HOST"] = BLACKHOLE_HOST
        elif mode in FAKES:
            bin_dir, hanging = _fake_docker_dir(tmp, mode)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
            injected["hang_subcommands"] = hanging
        else:
            raise ValueError(f"未知模式：{mode}（可选 blackhole / {' / '.join(FAKES)}）")
        for tier, (module, sentinel) in TIERS.items():
            code = CHILD.format(cap=cap_seconds, module=module)
            started = time.monotonic()
            proc = subprocess.run(  # noqa: S603 受控常量参数（本机解释器 + 模板代码）
                [sys.executable, "-c", code], cwd=ROOT, env=env, text=True, capture_output=True,
                timeout=int(cap_seconds * 12 + 60),
            )
            elapsed = round(time.monotonic() - started, 2)
            reason = None
            if proc.returncode == 0:
                try:
                    start = proc.stdout.index("{")
                    reason = json.loads(proc.stdout[start: proc.stdout.rindex("}") + 1]).get("reason")
                except ValueError:
                    reason = None
            rows[tier] = {
                "offenders": [],
                "reason": reason or "",
                "stderr": proc.stderr[-200:] if reason is None else "",
                "elapsed_s": elapsed,
                "sentinel": sentinel,
            }
    return {"injected": injected, "tiers": annotate(rows)}


def tier_offenders(rows: dict[str, dict]) -> list[str]:
    """台子自己的主张：每档都要**给得出**原因，且原因可行动（复用 N-48 的判据）。"""
    from validate_release import pending_reason_offenders

    if not rows:
        return ["一个档都没测：这台子无事可做（与恒真同形）"]
    statuses = {
        tier: {
            "status": "PENDING",
            "note": f"整档 ? 用例未执行，原因：{row.get('reason', '')}（哨兵 {row.get('sentinel', '?')}）",
        }
        for tier, row in rows.items()
    }
    return pending_reason_offenders(statuses)


def annotate(rows: dict[str, dict]) -> dict[str, dict]:
    """给每档补上它自己的偏离列表（判据与读数同源，读 JSON 的人不用自己再推一遍）。"""
    from validate_release import pending_reason_offenders

    statuses = {
        tier: {
            "status": "PENDING",
            "note": f"整档 ? 用例未执行，原因：{row.get('reason', '')}（哨兵 {row.get('sentinel', '?')}）",
        }
        for tier, row in rows.items()
    }
    offenders = pending_reason_offenders(statuses)
    per_tier: dict[str, list[str]] = {tier: [] for tier in rows}
    for text in offenders:  # pending_reason_offenders 的每条都以 "档名: " 开头
        tier = text.split(":", 1)[0].strip()
        per_tier.setdefault(tier, []).append(text)
    for tier, row in rows.items():
        if not row.get("reason"):
            per_tier[tier].append(f"挂起时没给出原因（{str(row.get('stderr', ''))[-120:]}）")
        row["offenders"] = per_tier[tier]
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("blackhole", "hang-later", "half-hang", "both", "all"), default="both")
    ap.add_argument("--timeout", type=float, default=20.0, help="单次探测的超时上限（秒）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.mode == "both":
        modes = ("blackhole", "hang-later")
    elif args.mode == "all":
        modes = ("blackhole", "hang-later", "half-hang")
    else:
        modes = (args.mode,)
    reports = {mode: measure(mode, args.timeout) for mode in modes}
    offenders = {mode: tier_offenders(report["tiers"]) for mode, report in reports.items()}
    if args.json:
        print(json.dumps({"reports": reports, "offenders": offenders}, ensure_ascii=False, indent=2))
    else:
        for mode, report in reports.items():
            print(f"== {mode}（单次探测超时上限 {args.timeout:g}s）")
            for tier, row in report["tiers"].items():
                print(f"   {tier:18s} {row['elapsed_s']:6.2f}s  {row['reason'] or '(没有原因)'}")
            if offenders[mode]:
                print("   判据未过：", *offenders[mode], sep="\n     - ")
    total = [o for lst in offenders.values() for o in lst]
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""`make hang-probe`：把"外部依赖卡住"做成可复跑的取证件，并量出真实代价上界。

上一轮的读数只覆盖到"版本探测阶段就挂"（它先短路），
"版本通、后续探测挂"那一支我只给了估算（20+30+30=80s/档）却没量过——
估算写在登记行里就是主张。这台子用两种真实方式造挂起：
- `blackhole`：`DOCKER_HOST=tcp://192.0.2.1:2375`（TEST-NET-1 黑洞，丢包≠拒连）；
- `hang-later`：在 PATH 前面放一个假 `docker`——`version` 立刻返回 0，其余子命令 `sleep` 到被超时掐掉。
两者都是**真子进程真超时**，不是 monkeypatch。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = "scripts/hang_probe.py"
TIERS = ("docker", "postgres", "object-store", "k8s-control-plane")


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=600)  # noqa: S603


def _probe(mode: str) -> dict:
    proc = _run([sys.executable, "-m", "scripts.hang_probe", "--mode", mode, "--timeout", "2", "--json"])
    assert proc.returncode == 0, proc.stdout[-600:] + proc.stderr[-600:]
    start = proc.stdout.index("{")
    data = json.loads(proc.stdout[start: proc.stdout.rindex("}") + 1])
    return data["reports"][mode]


def test_hang_later_mode_measures_a_finite_cost_per_tier() -> None:
    """版本探测通、后续探测挂：每档都必须给出原因，且代价有限（不是 3600s 的挂死）。"""
    report = _probe("hang-later")
    assert tuple(report["tiers"].keys()) == TIERS or set(report["tiers"]) == set(TIERS), sorted(report["tiers"])
    for tier, row in report["tiers"].items():
        assert row["reason"], f"{tier} 挂住却没给出原因：{row}"
        assert row["offenders"] == [], f"{tier} 的原因不可行动：{row}"
        assert 0 < row["elapsed_s"] < 60, f"{tier} 的代价不像被超时封顶：{row['elapsed_s']}s"
    # 这一支的"卡住"必须是真卡住：假 CLI 里除 version 外全是 sleep
    assert report["injected"]["hang_subcommands"], report["injected"]


def test_blackhole_mode_is_the_simpler_short_circuit_path() -> None:
    report = _probe("blackhole")
    for tier, row in report["tiers"].items():
        assert "超时" in row["reason"] or "不可达" in row["reason"], (tier, row)
        assert row["offenders"] == [], (tier, row)
        # 黑洞下版本探测先挂 ⇒ 每档只付一次超时
        assert row["elapsed_s"] < 12, f"{tier} 在黑洞模式下付了不止一次超时：{row['elapsed_s']}s"


def test_probe_exit_code_is_load_bearing(tmp_path) -> None:
    """台子的 rc 必须承载它自己的主张：把"原因可读"这条判据摘掉，它就该红。

    做法是最小改动地喂一个"永远给不出原因"的档位表——直接调纯函数，不起子进程。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib

    probe = importlib.import_module("hang_probe")
    assert probe.tier_offenders({"docker": {"reason": "", "offenders": [], "elapsed_s": 1.0}})
    assert probe.tier_offenders(
        {"docker": {"reason": "docker daemon 不可达（命令超时（2s））", "offenders": [], "elapsed_s": 1.0}}
    ) == []
    # 空表不是"通过"
    assert probe.tier_offenders({})


def test_makefile_wires_the_target() -> None:
    make = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "hang-probe:" in make, "没有 hang-probe 目标"
    block, collecting = [], False
    for line in make.splitlines():
        if line.startswith(".PHONY:"):
            collecting = True
        elif collecting and not line.rstrip().endswith("\\"):
            break
        if collecting:
            block.append(line)
    assert block and "hang-probe" in " ".join(block), "新目标没写进 .PHONY，`make -n` 会把它当文件目标"
    assert "-m scripts.hang_probe" in make

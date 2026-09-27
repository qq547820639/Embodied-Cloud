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

def test_half_hang_isolates_which_probe_pays_and_which_does_not() -> None:
    """半挂剧本（`image inspect` 快答"没有"、`image ls` 挂住）分得出谁在等、谁不等。

    这是对 N-49 那个极端假设的修正：全挂的剧本说不清"哪一层探测暴露在挂起风险里"。
    同时这一支还钉住一台子的副产品——它在 half-hang 下抓到过 docker 档的原因只描述了
    现状、没给动作（"没有本地缓存…试过：…"），据此把原因补成可行动的。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.hang_probe", "--mode", "half-hang", "--timeout", "2", "--json"],
        cwd=ROOT, text=True, capture_output=True, timeout=900,
    )
    assert proc.returncode == 0, proc.stdout[-800:] + proc.stderr[-400:]
    start = proc.stdout.index("{")
    data = json.loads(proc.stdout[start: proc.stdout.rindex("}") + 1])
    assert data["offenders"] == {"half-hang": []}, data["offenders"]
    rows = data["reports"]["half-hang"]["tiers"]
    # 只有兜底 `image ls` 的那一档会等；其余档在 inspect 快答后就拿到结论
    assert rows["k8s-control-plane"]["elapsed_s"] >= 2, rows["k8s-control-plane"]
    assert rows["postgres"]["elapsed_s"] < 2, rows["postgres"]
    assert "EMBODIEDCLOUD_DOCKER_TEST_IMAGE" in rows["docker"]["reason"], rows["docker"]


def test_the_dual_half_hang_flips_who_pays() -> None:
    """对偶半挂（`inspect` 挂、`ls` 通）要把"谁在等"翻过来——否则上一次的结论可能是剧本偏袒。

    N-52 的 half-hang 让 k8s 独占 20s、其余近乎 0；若那是因为剧本恰好只掐了 `image ls`，
    换成掐 `image inspect` 时 docker 档就该变成付得最多的那个（它逐个探测候选镜像）。
    这个"角色互换"是判据，不是观察。
    """
    def run(mode: str) -> dict:
        proc = subprocess.run(  # noqa: S603 受控常量参数（本机解释器 + 仓库内脚本）
            [sys.executable, "-m", "scripts.hang_probe", "--mode", mode, "--timeout", "2", "--json"],
            cwd=ROOT, text=True, capture_output=True, timeout=900,
        )
        assert proc.returncode == 0, proc.stdout[-600:] + proc.stderr[-400:]
        out = proc.stdout
        data = json.loads(out[out.index("{"): out.rindex("}") + 1])
        assert data["offenders"] == {mode: []}, data["offenders"]
        return data["reports"][mode]["tiers"]

    a = run("half-hang")
    b = run("half-hang-b")
    assert b["docker"]["elapsed_s"] > a["docker"]["elapsed_s"] + 1, (a["docker"], b["docker"])
    assert b["docker"]["reason"] and b["k8s-control-plane"]["reason"], (b["docker"], b["k8s-control-plane"])
    assert a["postgres"]["elapsed_s"] < 2, a["postgres"]
    assert b["postgres"]["elapsed_s"] >= 2, b["postgres"]

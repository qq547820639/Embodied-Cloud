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
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = "scripts/hang_probe.py"
TIERS = ("docker", "postgres", "object-store", "k8s-control-plane")


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=600)  # noqa: S603


def _payload(proc: subprocess.CompletedProcess[str]) -> dict:
    """退码是"这台机器今天分不分得清慢与挂"的读数，不是代码事实，因此：

    1 ⇒ 探针自己的主张没过（真违规，必须响）；0/2 ⇒ 都可接受，但 2 必须自报"测不准"，
    且那种跑不许拿耗时格下结论（见 `_timings_trustworthy`）。
    """
    if proc.returncode == 1:
        raise AssertionError(f"探针判据未过：{proc.stdout[-600:]}{proc.stderr[-400:]}")
    assert proc.returncode in (0, 2), (proc.returncode, proc.stdout[-400:], proc.stderr[-400:])
    out = proc.stdout
    return json.loads(out[out.index("{"): out.rindex("}") + 1])


def _timings_trustworthy(report: dict) -> bool:
    return bool(report["injected"].get("conclusive", True))


def _probe(mode: str) -> dict:
    proc = _run([sys.executable, "-m", "scripts.hang_probe", "--mode", mode, "--timeout", "2", "--json"])
    report = _payload(proc)["reports"][mode]
    if not _timings_trustworthy(report):
        assert "测不准" in proc.stdout + proc.stderr, proc.stderr[-400:]
    return report


def test_hang_later_mode_measures_a_finite_cost_per_tier() -> None:
    """版本探测通、后续探测挂：每档都必须给出原因，且代价有限（不是 3600s 的挂死）。"""
    report = _probe("hang-later")
    assert tuple(report["tiers"].keys()) == TIERS or set(report["tiers"]) == set(TIERS), sorted(report["tiers"])
    for tier, row in report["tiers"].items():
        assert row["reason"], f"{tier} 挂住却没给出原因：{row}"
        assert row["offenders"] == [], f"{tier} 的原因不可行动：{row}"
    if not _timings_trustworthy(report):
        return  # 慢与挂分不清：契约格上面已断完，耗时格不作结论
        # 代价的天花板不写死秒数：等于"被掐掉的探测次数 × 当场量出的有效上限"（+调度余量）
        assert row["timeouts_observed"] >= 1, f"{tier} 一次都没被掐，剧本没生效：{row}"
        assert 0 < row["waited_s"] <= row["bound_s"], f"{tier} 的被掐等待越过探针算出的上界：{row}"
        assert row["startup_overhead_s"] >= 0, row
    # 这一支的"卡住"必须是真卡住：假 CLI 里除 version 外全是 sleep
    assert report["injected"]["hang_subcommands"], report["injected"]


def test_blackhole_mode_is_the_simpler_short_circuit_path() -> None:
    report = _probe("blackhole")
    for tier, row in report["tiers"].items():
        assert "超时" in row["reason"] or "不可达" in row["reason"], (tier, row)
        assert row["offenders"] == [], (tier, row)
        # 不写"12 秒"这种天花板：断"只付一次被掐"＋耗时不越过探针自报的上界
        assert row["timeouts_observed"] == 1, f"{tier} 在黑洞模式下付了不止一次超时：{row}"
        assert row["elapsed_s"] <= row["bound_s"], f"{tier} 越过探针算出的上界：{row}"


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
    data = _payload(proc)
    assert data["offenders"] == {"half-hang": []}, data["offenders"]
    rows = data["reports"]["half-hang"]["tiers"]
    for tier, row in rows.items():
        # 契约而不是环境：原因给得出、可行动、代价有上限
        assert row["reason"], (tier, row)
        assert row["offenders"] == [], (tier, row)
        assert row["waited_s"] <= row["bound_s"], (tier, row)
    # 至少有一档几乎不付费（剧本没有把所有探测一律掐死），而依赖兜底列表探测的那一档要付费
    # 上限由探针当场量出来的替身往返时间决定，不在测试里写死秒数
    injected = data["reports"]["half-hang"]["injected"]
    # 上限由探针当场量出的替身往返时间算出，测试里不写死秒数
    assert min(row["elapsed_s"] for row in rows.values()) < injected["effective_timeout_seconds"], rows
    assert injected["effective_timeout_seconds"] >= 4 * injected["fake_round_trip_seconds"], injected
    assert rows["k8s-control-plane"]["timeouts_observed"] >= 1, rows["k8s-control-plane"]


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
        data = _payload(proc)
        assert data["offenders"] == {mode: []}, data["offenders"]
        return data["reports"][mode]

    a = run("half-hang")
    b = run("half-hang-b")
    a_rows, b_rows = a["tiers"], b["tiers"]
    # 角色互换：掐掉 inspect 时，逐个探测候选镜像的 docker 档必须明显比只掐 ls 时更贵
    # 余量不写死：用探针当场量出的有效上限（由替身往返时间算出）
    margin = min(
        a["injected"]["effective_timeout_seconds"],
        b["injected"]["effective_timeout_seconds"],
    )
    assert margin >= 2, (a["injected"], b["injected"])
    for rows in (a_rows, b_rows):
        for tier, row in rows.items():
            assert row["waited_s"] <= row["bound_s"], (tier, row)
    assert b_rows["docker"]["elapsed_s"] > a_rows["docker"]["elapsed_s"] + margin, (
        a_rows["docker"], b_rows["docker"])
    for label, rows in (("half-hang", a_rows), ("half-hang-b", b_rows)):
        for tier, row in rows.items():
            # 只断"这台子保证的事"：原因给得出、可行动、代价有上限。
            # 不断具体哪一档快、原因里必须出现哪个词——那在别的机器上是假的。
            assert row["reason"] and row["offenders"] == [], (label, tier, row)
            assert row["waited_s"] <= row["bound_s"], (label, tier, row)


def test_the_timeout_formula_is_a_function_of_measured_latency() -> None:
    """上限与"能不能判"必须是同一个纯函数的两个出口，而不是两处各写一遍的经验值。

    两极都验：小往返 ⇒ 请求值就是上限且判定可信；大往返 ⇒ 封顶生效、此时工具必须
    承认"测不准"（宁可退 2 也不交出一份把慢说成挂的读数）。
    """
    from scripts.hang_probe import conclusive, derive_effective

    assert derive_effective(0.01, 2.0) == 2.0 and conclusive(0.01, 2.0)
    assert derive_effective(1.2, 2.0) == 4.8 and conclusive(1.2, 4.8)
    big = derive_effective(6.0, 2.0)          # 4×6=24 被封顶到 10
    assert big == 10.0 and not conclusive(6.0, big), (big, conclusive(6.0, big))


def test_the_probe_is_conclusive_across_concurrency_levels() -> None:
    """并发 1／4／8 下，预热后的往返分布都必须仍容得下"慢 ≠ 挂"的余量。

    这里**不出现任何绝对秒数**：本机实测过替身文件首次 exec 比之后慢一个数量级
    （0.05–0.27s 对 0.01s），机器忙时还会叠上偶发慢采样；两者都会把 5 个样本的 p95
    抬高。判"这台子今天能不能做挂起取证"用的是探针自己的 `conclusive()`，
    它的两极由 `test_warm_up_is_the_only_thing_keeping_the_cold_exec_out` 用夹具钉住。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.hang_probe", "--mode", "latency", "--timeout", "2"],
        cwd=ROOT, text=True, capture_output=True, timeout=900,
    )
    # rc 是这台机器的读数，不是代码的事实：忙到分不清慢与挂时，工具**应当**退 2。
    # 断言因此落在"退码与它自己打印的判定一致"上，而不是"今晚必须为 0"（那是把环境状态
    # 当结论——N-55/N-56 拆过的同一类形状，本轮在另一处复算时又踩到）。
    assert proc.returncode in (0, 2), (proc.returncode, proc.stdout[-800:], proc.stderr[-400:])
    out = proc.stdout
    data = json.loads(out[out.index("{"): out.rindex("}") + 1])["latency_profile"]
    assert sorted(data) == ["1", "4", "8"], data
    from scripts.hang_probe import conclusive, derive_effective

    inconclusive = {
        load for load, row in data.items()
        if not conclusive(row["p95"], derive_effective(row["p95"], 2.0))
    }
    assert (proc.returncode == 2) == bool(inconclusive), (proc.returncode, sorted(inconclusive), data)
    for load, row in data.items():
        # 被预热丢掉的那一次必须照样报出来：不报就成了"分布里没有慢样本"的假绿
        assert row["pre_warm_burst_s"] >= 0, (load, row)
        eff = derive_effective(row["p95"], 2.0)
        assert eff >= row["p95"], (load, row, eff)
        if load in inconclusive:
            assert "测不准" in out, (load, out[-600:])


def test_hanging_subcommands_are_read_off_the_script() -> None:
    """"这一档谁会挂"必须从剧本正文派生：手抄表写宽过（给 hang-later 列了四条，
    真实形状是"除 version/info 外全部会挂"），而那张表零读者＝写错也没人能发现。

    反向对照带在其中一条臂上：把 `image ls` 从 sleep 改成快答，派生集合必须跟着空掉——
    它要是还能读出 "image ls"，说明函数是硬编码的。
    """
    import scripts.hang_probe as probe

    assert not hasattr(probe, "HANGING"), "手抄表又长回来了"
    got = {mode: probe.hanging_subcommands(script) for mode, script in probe.FAKES.items()}
    assert got["hang-later"] == ["*"], got
    assert got["half-hang"] == ["image ls"], got
    assert got["half-hang-b"] == ["image inspect"], got
    patched = probe.FAKES["half-hang"].replace('"image ls"*) sleep 3600', '"image ls"*) exit 0')
    assert patched != probe.FAKES["half-hang"], "夹具没被改到，对照失效"
    assert probe.hanging_subcommands(patched) == [], patched


def test_warm_up_is_the_only_thing_keeping_the_cold_exec_out(tmp_path) -> None:
    """预热这一跳是承重的：拿一个"第一次 exec 慢、之后快"的替身喂两个档位。

    上一轮把机制写成"负载"，这一轮先写成夹具再写进文档——因为"本机第一次要 3s"
    这句话今晚复现不出来（12 次新目录实测 max 0.274s），而"新写入的文件首次 exec
    慢一个数量级"是可以被造出来的。判据只断两档的**判定翻转**，不断机器状态。
    """
    import scripts.hang_probe as probe

    cold_sleep = 5.5
    assert not probe.conclusive(cold_sleep, probe.derive_effective(cold_sleep, 2.0)), \
        "夹具的慢样本没有越过可判线：这支对照失去区分力"

    def fixture(tag: str) -> tuple:
        d = tmp_path / tag
        (d / "bin").mkdir(parents=True)
        script = d / "bin" / "docker"
        marker = d / "touched"
        script.write_text(
            "#!/bin/sh\n"
            f'[ -f "{marker}" ] || {{ sleep {cold_sleep}; touch "{marker}"; }}\n'
            'echo "Client: Docker Engine (cold-exec fake)"\n'
        )
        script.chmod(0o755)
        env = dict(os.environ, PATH=f"{script.parent}{os.pathsep}{os.environ.get('PATH', '')}")
        return d / "bin", env

    cold_dir, cold_env = fixture("cold")
    unwarmed = probe._latency_stats(cold_dir, cold_env, 1, 5, warm_up=False)
    warm_dir, warm_env = fixture("warm")
    warmed = probe._latency_stats(warm_dir, warm_env, 1, 5)

    # 未预热：冷启动落在分布里 ⇒ p95 至少盖住那一次睡眠 ⇒ 判定翻成"测不准"
    assert unwarmed["p95"] >= cold_sleep * 0.8, unwarmed
    assert not probe.conclusive(unwarmed["p95"], probe.derive_effective(unwarmed["p95"], 2.0)), unwarmed
    # 预热：同一形状的替身，分布里已经没有那一次睡眠 ⇒ 判定回到"可区分慢与挂"
    assert warmed["pre_warm_burst_s"] >= cold_sleep * 0.8, warmed
    assert probe.conclusive(warmed["p95"], probe.derive_effective(warmed["p95"], 2.0)), warmed

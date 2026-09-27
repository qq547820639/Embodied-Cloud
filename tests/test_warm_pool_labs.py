"""两张取证台必须有常驻读者：它们自己写的"读数可对账"要真能被对账。

动机（N-37）：`make warm-sla` / `make warm-capacity` 的读数被写进了登记表（N-28／N-29）
与门禁目录，但全仓没有任何常驻用例跑过它们 —— 于是"取证台还能跑"这件事没有读者：
API 一漂移（`maintain` 改签名、seed 改形状）它们就会悄悄变成打不出数的脚本，
而文档里那些引自它们的数会变成无法复算的引文。实测已经抓到两种真形状：

- 直接 `python scripts/warm_pool_capacity_lab.py` 当场 `ModuleNotFoundError: No module named 'tests'`
  （`make warm-capacity` 用 `-m scripts.…` 才活着）；
- SLA 台里"这次走的是 claim 而不是新建"与"直方图有没有样本"只 print 不记 rc，
  读错了也退 0。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPACITY = "scripts/warm_pool_capacity_lab.py"


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    import os

    # 不注入 PYTHONPATH：这条判据要量的就是"照用户自然的写法跑"活不活。
    return subprocess.run(  # noqa: S603 受控常量参数（本机 venv + 仓库内脚本）
        args, cwd=ROOT, text=True, capture_output=True, timeout=900, env={**os.environ, **(env or {})}
    )


def _capacity_json(*extra: str) -> list[dict]:
    """跑取证台的 --json，只取 stdout 里那段数组（日志走 stderr，但仍按括号切稳）。"""
    proc = _run([sys.executable, "-m", "scripts.warm_pool_capacity_lab", "--json", *extra])
    assert proc.returncode == 0, proc.stdout[-800:] + proc.stderr[-800:]
    start = proc.stdout.index("[")
    end = proc.stdout.rindex("]") + 1
    return json.loads(proc.stdout[start:end])


def test_capacity_lab_runs_from_both_entry_points() -> None:
    """`make warm-capacity`（-m）与自然写法（直接跑脚本文件）都必须活着。

    只测前者会让"照 README 直接跑"这条路是坏的这件事无人知晓——本轮实测就是这样。
    """
    via_module = _run([sys.executable, "-m", "scripts.warm_pool_capacity_lab", "--sizes", "1"])
    assert via_module.returncode == 0, via_module.stderr[-500:]
    via_file = _run([sys.executable, CAPACITY, "--sizes", "1"])
    assert via_file.returncode == 0, via_file.stdout[-500:] + via_file.stderr[-500:]
    assert "size" in via_file.stdout, via_file.stdout[-400:]  # 表头还在，说明没被改成只出 JSON


def test_capacity_lab_reports_both_shapes_and_respects_the_fleet() -> None:
    """两种形状（排空 / 积压）都要报；每一行的开格数都不许超过卡数。"""
    rows = _capacity_json("--sizes", "1,2")
    shapes = {r.get("shape") for r in rows}
    assert {"drained", "backlog"} <= shapes, f"取证台只报一种形状就看不见跨遍窗口：{shapes}"
    sizes = {r["warm_pool_size"] for r in rows}
    assert sizes == {1, 2}, sorted(sizes)
    for r in rows:
        assert r["warm_rows_created"] <= r["fleet_cards"], (
            f"shape={r['shape']} size={r['warm_pool_size']}: 建过 {r['warm_rows_created']} 行 "
            f"vs 舰队 {r['fleet_cards']} 张卡——闸门把同一张卡许诺了两次"
        )
        assert r["ready"] <= r["fleet_cards"], r
        assert r["provision_failed"] == 0, f"注定失败的行被创建出来了：{r}"
        assert r["warm_tombstones"] == 0, f"跑完留着一堆 tombstone：{r}"
    default = [r for r in rows if r["warm_pool_size"] == 1 and r["shape"] == "drained"]
    assert default and default[0]["ready"] == default[0]["requested_slots"], (
        f"生产默认 size=1 在 8 卡上该填满 {default[0]['requested_slots']} 位: {default[0]}"
    )


def test_backlog_shape_would_catch_the_cross_pass_bug(monkeypatch) -> None:
    """必开对照：把 N-35 的修复摘掉，积压形状必须立刻读出"开格 > 卡数"。

    没有这一支，上一条里的 `<= fleet_cards` 可以来自"闸门永远不开格"，
    也可以来自"这个形状压根读不到跨遍窗口"。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib

    lab = importlib.import_module("warm_pool_capacity_lab")
    from app.services.warmpool import WarmPoolManager

    real = WarmPoolManager._unbooked_inflight_gib
    monkeypatch.setattr(WarmPoolManager, "_unbooked_inflight_gib", lambda self, db: [])
    try:
        broken = lab.run_size(4, rounds=1, reserve=0, drain_between=False)
    finally:
        monkeypatch.setattr(WarmPoolManager, "_unbooked_inflight_gib", real)
    assert broken["warm_rows_created"] > broken["fleet_cards"], (
        f"摘掉跨遍预留之后取证台仍读不出超卖：{broken['warm_rows_created']} 行 / "
        f"{broken['fleet_cards']} 张卡 —— 这个形状是假的"
    )
    fixed = lab.run_size(4, rounds=1, reserve=0, drain_between=False)
    assert fixed["warm_rows_created"] <= fixed["fleet_cards"], fixed


def test_sla_lab_exit_code_carries_every_reading_it_prints() -> None:
    """`make warm-sla` 的 rc 必须随读数翻：池起不来时不许还退 0。"""
    ok = _run([sys.executable, "scripts/warm_pool_sla_lab.py"], env={"WARM_SLA_POOL_SIZE": "2"})
    assert ok.returncode == 0, ok.stdout[-900:] + ok.stderr[-400:]
    assert "overall=PASS" in ok.stdout, ok.stdout[-600:]

    starved = _run([sys.executable, "scripts/warm_pool_sla_lab.py"], env={"WARM_SLA_POOL_SIZE": "0"})
    assert starved.returncode != 0, (
        "池被设成 0 个 READY 时这次交付走的不是 claim，读数已失效却仍退 0："
        + starved.stdout[-900:]
    )
    assert "overall=FAIL" in starved.stdout, starved.stdout[-600:]

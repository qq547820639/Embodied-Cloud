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


def _load_lab():
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib

    return importlib.import_module("warm_pool_capacity_lab")


def test_recount_comparator_fires_on_every_polarity() -> None:
    """独立复算的比较器必须两头都会开火，否则"复算过了"只是又一句主张。"""
    lab = _load_lab()
    fn = lab.recount_discrepancies
    reading = {"ready": 3, "warm_rows_created": 3, "fleet_cards": 5, "free_cards_after": 2}
    gpu = {"allocated": 3, "free": 2, "ready_with_card": 3, "available_but_bound": 0}
    assert fn(reading, gpu, enforce_ready=True) == []
    # 池里说 3 格 READY，卡那边只认 2 张卡属于 READY 格 ⇒ 有一格没拿到卡/或拿到了两张
    assert any("ready" in line for line in fn({**reading}, {**gpu, "ready_with_card": 2}, enforce_ready=True))
    # AVAILABLE 的卡却还挂着 workspace ⇒ 释放没做干净
    assert any("AVAILABLE" in line for line in fn({**reading}, {**gpu, "available_but_bound": 1}, enforce_ready=True))
    # 空闲卡数对不上
    assert any("free_cards_after" in line for line in fn({**reading}, {**gpu, "free": 1}, enforce_ready=True))
    # 分母为 0 不算"干净"
    assert fn({**reading, "fleet_cards": 0}, gpu, enforce_ready=True)
    # backlog 形状里 READY 还没成形，不该误开火
    assert fn({"ready": 0, "warm_rows_created": 3, "fleet_cards": 3, "free_cards_after": 3},
              {"allocated": 0, "free": 3, "ready_with_card": 0, "available_but_bound": 0},
              enforce_ready=False) == []


def test_capacity_lab_reports_a_clean_independent_recount() -> None:
    """台子每行读数都带一份"换一条查法"的复算结果，且必须为空。

    读数来自 workspace 侧计数，复算走 Gpu 侧（`Gpu.status` + `Gpu.workspace_id` 联结），
    两条路必须给出同一个数——否则这张表（以及引用它的登记表 N-29／N-35）在说没法验证的话。
    """
    lab = _load_lab()
    drained = lab.run_size(2, rounds=2, reserve=0)
    assert drained["recount_offenders"] == [], drained["recount_offenders"]
    assert drained["ready"] > 0, f"drained 形状没填满任何格，复算就成了空对空：{drained}"
    backlog = lab.run_size(2, rounds=1, reserve=0, drain_between=False)
    assert backlog["recount_offenders"] == [], backlog["recount_offenders"]
    assert backlog["ready"] == 0, "积压形状里 worker 没跑，READY 不该有数（有的话说明形状名不副实）"


def test_gpu_side_counts_actually_reads_the_gpu_side(tmp_path) -> None:
    """复算的**传感器**也要单独验：造一张"AVAILABLE 却还挂着 workspace"的卡，它必须报出来。

    只验比较器（纯字典）证明不了 `gpu_side_counts` 读的是另一张表 —— 传感器坏了，
    复算就会永远"干净"。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.models import Gpu, GpuHost, GpuStatus, Template, Workspace, WorkspaceStatus

    lab = _load_lab()
    engine = create_engine(f"sqlite:///{tmp_path / 'recount-sensor.db'}")
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Factory() as db:
        db.add(GpuHost(id="s-host", name="s-h", address="127.0.0.1", provider="mock"))
        db.add(Template(
            id="s-t", slug="s-t", name="s", version="0.1.0", description="d", category="c",
            runtime="mock", launch_command="", enabled=True,
            gpu_requirement_gb=8, recommended_vram_gb=8,
        ))
        db.add(Workspace(
            id="s-ws", name="ghost-holder", template_id="s-t", provider="mock",
            status=WorkspaceStatus.CREATED.value, warm_pool_state="ready",
        ))
        db.add(Gpu(
            id="s-gpu", gpu_uuid="s-uuid", host_id="s-host", model="m",
            memory_total=8 * 1024, gpu_index=0,
            status=GpuStatus.AVAILABLE.value, workspace_id="s-ws",
        ))
        db.commit()
        gpu = lab.gpu_side_counts(db)
        assert gpu["available_but_bound"] == 1, gpu
        assert gpu["ready_with_card"] == 1, gpu
        offenders = lab.recount_discrepancies(
            {"ready": 1, "free_cards_after": 1, "fleet_cards": 1}, gpu, enforce_ready=True
        )
        assert any("AVAILABLE" in line for line in offenders), offenders
    engine.dispose()

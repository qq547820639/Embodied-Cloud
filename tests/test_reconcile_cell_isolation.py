"""一格收敛失败不得把整趟 reconcile 中止（N-90）。

`reconcile_all` 的逐格循环里只有一处 `try`（`provider.inspect`，:676-678）：MISSING 档
连着调 `_settle_running_segment` 与 `scheduler.release`，STOPPING 档连着调 `_stop_cleanup`
（内部还要过 streaming/provider/billing），任何一格抛错都从 `for` 里穿出去，`db.commit()`
与 `recover_stuck_gpu_allocations` 都不再执行，**后面所有格子这一轮没人看**。而这条路径的
调用面是启动恢复（`main.py:52` → `run_crash_recovery`）与 worker 线程 —— 一处 provider
故障会让一轮崩溃恢复只走完前几格。ADR 0002 给 provider/scheduler 边界立的正是"异常转成
原因、不外泄给调用面"，唯独这里没执行到底。

判据用一支可按 workspace 决定行为/provider 替身把「一格炸、其余格照看」做成确定性的，
另有一支结构判据钉住「逐格调用被 try 包住、且异常记进 `errors`」——没有它，把 try 拆掉
或把计数丢掉都不会有人发现，而这两件事恰恰是本轮修复的全部内容。
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    CreditLedger,
    Gpu,
    GpuAllocation,
    GpuStatus,
    LedgerType,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceStatus,
)
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.base import RuntimeState
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.utils import utcnow
from tests.dbfiles import db_url
from tests.gpu_drift import gpu_ownership_disagreements

ENGINE = create_engine(db_url("reconcile-isolation"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)
REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_SRC = REPO_ROOT / "app" / "services" / "orchestrator.py"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


class ScriptedProvider(MockProvider):
    """按 workspace 决定 `reconcile` 的行为：炸、或说 MISSING（其余格因此必须被看到）。"""

    def __init__(self, base_url: str, *, explode: set[str]) -> None:
        super().__init__(base_url)
        self.explode = explode
        self.observations: list[str] = []

    def reconcile(self, workspace: Workspace) -> RuntimeState:
        self.observations.append(workspace.id)
        if workspace.id in self.explode:
            raise RuntimeError(f"simulated: provider 在这一格炸了（{workspace.id[:8]}）")
        return RuntimeState.MISSING


def _orchestrator(provider: MockProvider) -> WorkspaceOrchestrator:
    return WorkspaceOrchestrator(
        Factory,
        provider,
        Path("/tmp/test-reconcile-cell-isolation"),  # noqa: S108 测试隔离目录
    )


def _seed_running_cells(count: int = 3) -> list[str]:
    """三格 RUNNING 的 workspace，各占一张够用的 mock 卡；`started_at=None` 让结算档空转。"""
    ids: list[str] = []
    with Factory() as db:
        scheduler = GpuScheduler(Factory)
        scheduler.sync_host(
            db,
            host_id="host-1",
            name="h1",
            address="127.0.0.1",
            provider="mock",
            gpus=[
                GpuInfo(gpu_uuid=f"gpu-{i}", model=f"RTX-{i}", memory_total=24564, index=i)
                for i in range(count)
            ],
        )
        db.add(
            User(
                id="u1",
                email="u1@x",
                username="u1",
                password_hash="x",  # noqa: S106 测试桩用户，非真实密码
                role=Role.STUDENT.value,
            )
        )
        db.add(
            Template(
                id="cartpole",
                slug="cartpole",
                name="cartpole",
                description="test",
                category="test",
                runtime="mock",
                launch_command="echo ok",
                enabled=True,
                recommended_vram_gb=16,
                estimated_hourly_cost_cny=1.0,
            )
        )
        db.commit()
        for i in range(count):
            wid = f"ws-{i}"
            db.add(
                Workspace(
                    id=wid,
                    name=f"cell-{i}",
                    template_id="cartpole",
                    provider="mock",
                    user_id="u1",
                    status=WorkspaceStatus.RUNNING.value,
                    # 显式递增：本文件的判据靠 created_at 决定遍历次序
                    created_at=BASE + timedelta(seconds=i),
                )
            )
            db.commit()
            gpu = scheduler.allocate(db, wid, gpu_requirement_gb=8)
            ws = db.get(Workspace, wid)
            ws.gpu_id = gpu.id
            db.commit()
            ids.append(wid)
    return ids


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


def _status(workspace_id: str) -> str:
    with Factory() as db:
        return db.get(Workspace, workspace_id).status


def _free_cards() -> int:
    with Factory() as db:
        return len(
            list(db.scalars(select(Gpu).where(Gpu.status == GpuStatus.AVAILABLE.value)))
        )


# ---------------------------------------------------------------------------
# 行为：一格炸，其余格照看
# ---------------------------------------------------------------------------


def test_a_failing_cell_does_not_abort_the_reconcile_pass() -> None:
    """第一格 provider 抛错 ⇒ 它留在 RUNNING，后两格照样被收敛，且这轮记了 errors。

    改前这一支拿到的是 `RuntimeError` 直接穿出 `reconcile_all`：第二、三格没人看，
    卡也没回到池里。
    """
    ids = _seed_running_cells(3)
    provider = ScriptedProvider("http://127.0.0.1:8000", explode={ids[0]})
    stats = _orchestrator(provider).reconcile_all()

    assert stats["errors"] == 1, stats
    assert _status(ids[0]) == WorkspaceStatus.RUNNING.value, "炸掉的那格被写成了别的状态"
    assert _status(ids[1]) == WorkspaceStatus.FAILED.value
    assert _status(ids[2]) == WorkspaceStatus.FAILED.value
    assert provider.observations == ids, "遍历在炸掉的那格就停了（后面的格没被 provider 看过）"
    assert _free_cards() == 2, "两格放了卡，池子里却少了两张"


def test_the_pass_keeps_working_after_a_cell_that_wrote_midway() -> None:
    """中间那格炸掉时，前一格已经放过卡（提交过）——第三格仍必须被看到。

    钉的是"循环游标在会话提交之后还能走完"：`reconcile_all` 现在先把行取成 list，
    游标不再跨越提交。
    """
    ids = _seed_running_cells(3)
    provider = ScriptedProvider("http://127.0.0.1:8000", explode={ids[1]})
    stats = _orchestrator(provider).reconcile_all()

    assert stats["errors"] == 1, stats
    assert provider.observations[-1] == ids[2], f"第三格没被看到：{provider.observations}"
    assert _status(ids[0]) == WorkspaceStatus.FAILED.value
    assert _status(ids[1]) == WorkspaceStatus.RUNNING.value


def test_a_healthy_pass_reports_no_errors_and_is_idempotent() -> None:
    """没有故障时：errors 恒为 0，且连跑两趟结果一致（幂等是这函数原有的契约）。"""
    ids = _seed_running_cells(3)
    orchestrator = _orchestrator(ScriptedProvider("http://127.0.0.1:8000", explode=set()))

    first = orchestrator.reconcile_all()
    second = orchestrator.reconcile_all()

    assert first["errors"] == 0 and second["errors"] == 0, (first, second)
    assert first["failed"] == len(ids)
    # 第二趟扫到 0 格：三格都已是终态，终态过滤在准入判定**之前**（N-99 给 `reconcile_all`
    # 加的 scanned/skipped 两格因此在这里也一起被钉住——形状不随定向档变化）。
    assert second == {
        "adopted": 0,
        "failed": 0,
        "stopped": 0,
        "requeued": 0,
        "kept": 0,
        "errors": 0,
        "scanned": 0,
        "skipped": 0,
    }, second
    assert all(_status(i) == WorkspaceStatus.FAILED.value for i in ids)


# ---------------------------------------------------------------------------
# 异常边界的另一半：本格退回非终态之后，下一趟必须收完（N-106）
#
# 牙齿（六臂变异电池实测，2026-09-28；各臂恢复后 `cmp` 逐字节相同、`git diff app/` 为空、末跑 8 passed）：
# 基线与无关注释臂 0 红。G2 让 RUNNING+MISSING 档不再结算 ⇒ 只红 R1+R2（本文件其余六支照绿）；
# G3 把入账幂等键从 `started_at.isoformat()` 换成 `utcnow().isoformat()` ⇒ **只红 R2**
# （"补做不得把同一段算两次"这一面有独立的牙）；G4 让 STOPPING+MISSING 档不叫 release ⇒ **只红 R3**。
# G1 让异常边界顺手写 FAILED ⇒ R1+R2 红，并连带打红既有那两支"本格留在原状态"的判据与 R3——
# 四支都读同一列，不是四支各有牙；这一臂说的是 ADR 0002 修订那条「可重试的失败不得写成终态」。
# ---------------------------------------------------------------------------


class _ReleaseGate:
    """让 `scheduler.release` 只在指定的那一格、指定的那一趟抛错。

    第二趟放行是设计的一部分：不这样就没法把「残留」与「补做」钉在同一条判据里。
    """

    def __init__(self, scheduler: GpuScheduler, workspace_id: str) -> None:
        self.scheduler = scheduler
        self.workspace_id = workspace_id
        self.real = scheduler.release
        self.arm = True

    def install(self) -> None:
        self.scheduler.release = self

    def disarm(self) -> None:
        self.scheduler.release = self.real

    def __call__(self, db, workspace_id: str) -> None:
        if self.arm and workspace_id == self.workspace_id:
            raise RuntimeError("simulated release failure")
        self.real(db, workspace_id)


def _give_a_run_segment(workspace_id: str, seconds: int = 30) -> None:
    """真给这一格一段运行段：`started_at` 非空，结算档才不是空转。"""
    with Factory() as db:
        ws = db.get(Workspace, workspace_id)
        ws.started_at = utcnow() - timedelta(seconds=seconds)
        db.commit()


def _cell_state(workspace_id: str, gpu_id: str | None = None) -> dict:
    """读这一格的权威事实。`gpu_id` 要在放卡之前先抓下来传进来：
    放卡之后两张表都不再指向那张卡，事后按绑定去找会读成 None。"""
    with Factory() as db:
        ws = db.get(Workspace, workspace_id)
        card_id = gpu_id or ws.gpu_id
        gpu = db.get(Gpu, card_id) if card_id else None
        if gpu is None:
            gpu = db.scalar(select(Gpu).where(Gpu.workspace_id == workspace_id))
        allocations = list(
            db.scalars(
                select(GpuAllocation).where(
                    GpuAllocation.workspace_id == workspace_id,
                    GpuAllocation.released_at.is_(None),
                )
            )
        )
        usage = list(
            db.scalars(
                select(CreditLedger).where(
                    CreditLedger.workspace_id == workspace_id,
                    CreditLedger.type == LedgerType.USAGE.value,
                )
            )
        )
        return {
            "status": ws.status,
            "gpu_id": ws.gpu_id,
            "card": None if gpu is None else (gpu.status, gpu.workspace_id),
            "open_allocations": len(allocations),
            "usage_rows": len(usage),
            "disagreements": [o["kind"] for o in gpu_ownership_disagreements(db)],
        }


def test_release_raising_inside_a_cell_rolls_back_the_status_not_the_booking() -> None:
    """R1 `_reconcile_one` 里 release 抛错：本格退回 RUNNING，已提交的结算不许被一起吞掉。

    顺序是 `:799` 结算 → `:800` release，而结算自己就提交（`ledger.record` 内部 commit，
    `orchestrator.py:561` 明写这件事），所以异常边界的 `db.rollback()` 只该退回未提交的那半
    ——状态、卡、格上绑定。既有两支注入的是 `provider.reconcile` 抛错（炸在判据之前，
    结算根本没跑过），这一支炸在 release，结算已经跑完：此前没有位点。
    """
    ids = _seed_running_cells(3)
    target = ids[0]
    _give_a_run_segment(target)
    orchestrator = _orchestrator(ScriptedProvider("http://127.0.0.1:8000", explode=set()))
    gate = _ReleaseGate(orchestrator.scheduler, target)
    gate.install()
    try:
        stats = orchestrator.reconcile_all()
    finally:
        gate.disarm()

    assert stats["errors"] == 1, stats
    assert stats["failed"] == 2, f"其余格必须照样被收敛：{stats}"
    snap = _cell_state(target)
    assert snap["status"] == WorkspaceStatus.RUNNING.value, snap["status"]
    assert snap["card"] == (GpuStatus.ALLOCATED.value, target), snap["card"]
    assert snap["open_allocations"] == 1, snap
    assert snap["gpu_id"] is not None, "卡没放掉却把绑定清了 ⇒ 又回到 N-82 那个形状"
    assert snap["disagreements"] == [], snap["disagreements"]
    assert snap["usage_rows"] == 1, f"已提交的结算被 rollback 一起吞了：{snap}"


def test_the_next_pass_converges_the_cell_whose_release_raised() -> None:
    """R2 下一趟（同一入口、release 已放行）必须把这格收完，且不重复入账。"""
    ids = _seed_running_cells(3)
    target = ids[0]
    _give_a_run_segment(target)
    card_id = _cell_state(target)["gpu_id"]
    orchestrator = _orchestrator(ScriptedProvider("http://127.0.0.1:8000", explode=set()))
    gate = _ReleaseGate(orchestrator.scheduler, target)
    gate.install()
    try:
        orchestrator.reconcile_all()
    finally:
        gate.disarm()
    stuck = _cell_state(target, card_id)
    assert stuck["status"] == WorkspaceStatus.RUNNING.value, "前提塌了：第一趟没留下残留"

    second = orchestrator.reconcile_all()
    after = _cell_state(target, card_id)

    assert second["failed"] >= 1, second
    assert after["status"] == WorkspaceStatus.FAILED.value, after
    assert after["card"] == (GpuStatus.AVAILABLE.value, None), after["card"]
    assert after["gpu_id"] is None, "卡回池而格上还绑着（N-86 的成对性）"
    assert after["open_allocations"] == 0, after
    assert after["disagreements"] == []
    assert after["usage_rows"] == 1, f"补做把同一段算了两次：{after}"


def test_release_raising_in_the_stopping_branch_is_finished_by_the_next_pass() -> None:
    """R3 STOPPING+MISSING 档（`:833` 的 `_finalize_stop`）：停在 STOPPING，下一趟收完。"""
    ids = _seed_running_cells(2)
    target = ids[0]
    with Factory() as db:
        ws = db.get(Workspace, target)
        ws.status = WorkspaceStatus.STOPPING.value
        db.commit()
    _give_a_run_segment(target)
    card_id = _cell_state(target)["gpu_id"]
    orchestrator = _orchestrator(ScriptedProvider("http://127.0.0.1:8000", explode=set()))
    gate = _ReleaseGate(orchestrator.scheduler, target)
    gate.install()
    try:
        stats = orchestrator.reconcile_all()
    finally:
        gate.disarm()

    assert stats["errors"] == 1, stats
    stuck = _cell_state(target, card_id)
    assert stuck["status"] == WorkspaceStatus.STOPPING.value, stuck["status"]
    assert stuck["card"] == (GpuStatus.ALLOCATED.value, target), stuck["card"]

    orchestrator.reconcile_all()
    after = _cell_state(target, card_id)
    assert after["status"] == WorkspaceStatus.STOPPED.value, after
    assert after["card"] == (GpuStatus.AVAILABLE.value, None), after["card"]
    assert after["gpu_id"] is None, after
    assert after["disagreements"] == []


# ---------------------------------------------------------------------------
# 结构：逐格调用必须被 try 包住，并且异常记进 errors
# ---------------------------------------------------------------------------


def guarded_per_cell_calls(source: str) -> dict[str, int]:
    """读 `reconcile_all` 的函数体：逐格调用了几次、其中几次被 try 包住、handler 里有没有记 errors。"""
    info = {"calls": 0, "guarded": 0, "counts_errors": 0}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "reconcile_all":
            guarded_ids: set[int] = set()
            for stmt in ast.walk(node):
                if not isinstance(stmt, ast.Try):
                    continue
                for inner in ast.walk(stmt):
                    if isinstance(inner, ast.Call) and inner is not stmt:
                        guarded_ids.add(id(inner))
                # 只在 except 分支里出现的 stats["errors"] 才算"异常被记账"
                for handler in stmt.handlers:
                    for inner in ast.walk(handler):
                        if (
                            isinstance(inner, ast.Subscript)
                            and isinstance(inner.value, ast.Name)
                            and inner.value.id == "stats"
                            and getattr(inner.slice, "value", None) == "errors"
                        ):
                            info["counts_errors"] += 1
                            break
            tried = guarded_ids
            for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
                func = call.func
                if isinstance(func, ast.Attribute) and func.attr == "_reconcile_one":
                    info["calls"] += 1
                    if id(call) in tried:
                        info["guarded"] += 1
    return info


def test_reconcile_all_wires_every_cell_through_the_guard() -> None:
    """逐格调用必须恰好一次、被 try 包住、异常记进 `errors`——三样缺一即回归。"""
    info = guarded_per_cell_calls(ORCHESTRATOR_SRC.read_text(encoding="utf-8"))
    assert info == {"calls": 1, "guarded": 1, "counts_errors": 1}, info


def test_the_structure_ruler_can_see_the_unguarded_shape() -> None:
    """反向对照：把 try 拆掉、或把计数丢掉，尺子必须各报一次。"""
    unguarded = (
        "def reconcile_all():\n"
        "    for w in rows:\n"
        "        self._reconcile_one(db, w, stats)\n"
    )
    assert guarded_per_cell_calls(unguarded) == {"calls": 1, "guarded": 0, "counts_errors": 0}
    uncounted = (
        "def reconcile_all():\n"
        "    try:\n"
        "        self._reconcile_one(db, w, stats)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    assert guarded_per_cell_calls(uncounted) == {"calls": 1, "guarded": 1, "counts_errors": 0}
    guarded = (
        "def reconcile_all():\n"
        "    try:\n"
        "        self._reconcile_one(db, w, stats)\n"
        "    except Exception:\n"
        '        stats["errors"] = stats.get("errors", 0) + 1\n'
    )
    assert guarded_per_cell_calls(guarded) == {"calls": 1, "guarded": 1, "counts_errors": 1}

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
    Gpu,
    GpuStatus,
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
from tests.dbfiles import db_url

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
    assert second == {
        "adopted": 0,
        "failed": 0,
        "stopped": 0,
        "requeued": 0,
        "kept": 0,
        "errors": 0,
    }, second
    assert all(_status(i) == WorkspaceStatus.FAILED.value for i in ids)


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

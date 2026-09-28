"""每一条**入账**路径都必须抬一次 `gpu_seconds_total`（N-89 闭登记项 N-89）。

N-88 把 counter 的取值改成「账本净增量」之后，顺手量出另一半：抬点只有 `_finalize_stop`
一处，而账本入秒数的路径不止一条——
- `destroy()` 经 `_settle_running_segment` 结算（`orchestrator.py:612`），
- `reconcile_all` 的 RUNNING＋MISSING 档同样先结算再放卡（`orchestrator.py:741`），
两条都入账、都不抬指标。探针实测两路 `counter +0.0` 而 `settled_gpu_seconds = 30`，
HEAD 与改后读数相同 ⇒ 是既有少计，不是 N-88 引入的。

为什么这是缺陷而不是"指标口径本来只管停止"：`docs/ARCHITECTURE.md` 的指标表把
`gpu_seconds_total` 写成"已计费的 GPU 秒"。按那个说法，任何一笔 USAGE 入账都该动它；
少计就是对外把用量报小——看板上"这租户跑了多少 GPU 秒"与账本对不上，而且是**安静地**少。

修法把抬点从调用臂挪进结算本身（`_settle_run_delta` 在算出 `delta` 的地方抬一次）：
"哪条路径该记账"从此不是一个每条臂要各自记住的问题。停止那一档的行为逐位不变
（`had_start` 与 `_settle_run_delta` 的 `if not workspace.started_at: return 0, 0` 是同一条件），
所以 N-88/N-64 那 22 支判据一字未改就照绿；本文件的判据补的是它们没覆盖的两条臂。
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    CreditLedger,
    Gpu,
    GpuStatus,
    LedgerType,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceStatus,
)
from app.services import orchestrator as orchestrator_module
from app.services.ledger import CreditLedgerService
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.base import RuntimeState
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.services.streaming import StreamingSessionService
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("gpu-seconds-paths"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)
REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_SRC = REPO_ROOT / "app" / "services" / "orchestrator.py"
SEGMENT_SECONDS = 30


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class AlwaysMissingProvider(MockProvider):
    """provider 亲口说 runtime 不在了：reconcile 的 MISSING 档与 destroy 都该走到收敛。"""

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self.destroy_calls = 0

    def reconcile(self, workspace: Workspace) -> RuntimeState:
        return RuntimeState.MISSING

    def destroy(self, workspace: Workspace) -> None:
        self.destroy_calls += 1


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    with Factory() as db:
        GpuScheduler(Factory).sync_host(
            db,
            host_id="host-1",
            name="h1",
            address="127.0.0.1",
            provider="mock",
            gpus=[GpuInfo(gpu_uuid="gpu-1", model="RTX", memory_total=24564, index=0)],
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
                recommended_vram_gb=8,
                estimated_hourly_cost_cny=1.0,
            )
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
        db.commit()
    yield
    Base.metadata.drop_all(ENGINE)


def _orchestrator(provider: MockProvider | None = None) -> WorkspaceOrchestrator:
    return WorkspaceOrchestrator(
        Factory,
        provider or MockProvider("http://127.0.0.1:8000"),
        Path("/tmp/test-gpu-seconds-booked-paths"),  # noqa: S108 测试隔离目录
        streaming=StreamingSessionService(Factory),
        ledger=CreditLedgerService(Factory),
    )


def _running_segment(monkeypatch) -> tuple[WorkspaceOrchestrator, Clock, str]:
    """一格真占了卡、started_at 在冻住的时钟之前 SEGMENT_SECONDS 秒的 RUNNING 工作区。"""
    clock = Clock(datetime.now(UTC))
    monkeypatch.setattr(orchestrator_module, "utcnow", clock)
    orchestrator = _orchestrator()
    with Factory() as db:
        ws = Workspace(
            id="ws-seg",
            name="seg",
            template_id="cartpole",
            provider="mock",
            user_id="u1",
            status=WorkspaceStatus.RUNNING.value,
            started_at=clock.now - timedelta(seconds=SEGMENT_SECONDS),
        )
        db.add(ws)
        db.commit()
        gpu = GpuScheduler(Factory).allocate(db, "ws-seg", gpu_requirement_gb=8)
        ws.gpu_id = gpu.id
        db.commit()
    return orchestrator, clock, "ws-seg"


def _counter() -> float:
    return REGISTRY.get_sample_value("gpu_seconds_total") or 0.0


def _ledger_sum(db, workspace_id: str) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(CreditLedger.gpu_seconds), 0)).where(
                CreditLedger.workspace_id == workspace_id,
                CreditLedger.type == str(LedgerType.USAGE),
            )
        )
        or 0
    )


# ---------------------------------------------------------------------------
# 两条此前少计的路径
# ---------------------------------------------------------------------------


def test_destroy_books_seconds_and_moves_the_counter(monkeypatch) -> None:
    """DESTROY 结算了 30 秒 ⇒ counter 必须动 30（改前实测 +0.0）。"""
    orchestrator, _clock, wid = _running_segment(monkeypatch)
    before = _counter()
    with Factory() as db:
        orchestrator.destroy(db, db.get(Workspace, wid))

    assert _counter() - before == SEGMENT_SECONDS, (
        f"destroy 之后 counter 只动了 {_counter() - before}，账本入了多少另说"
    )
    with Factory() as db:
        assert _ledger_sum(db, wid) == SEGMENT_SECONDS
        assert db.get(Workspace, wid).status == WorkspaceStatus.DELETED.value


def test_reconcile_running_to_missing_path_moves_the_counter(monkeypatch) -> None:
    """reconcile 的 RUNNING＋MISSING 档同样入账 ⇒ 同样要抬 counter（改前 +0.0）。"""
    provider = AlwaysMissingProvider("http://127.0.0.1:8000")
    clock = Clock(datetime.now(UTC))
    monkeypatch.setattr(orchestrator_module, "utcnow", clock)
    orchestrator = _orchestrator(provider)
    with Factory() as db:
        ws = Workspace(
            id="ws-rec",
            name="rec",
            template_id="cartpole",
            provider="mock",
            user_id="u1",
            status=WorkspaceStatus.RUNNING.value,
            started_at=clock.now - timedelta(seconds=SEGMENT_SECONDS),
        )
        db.add(ws)
        db.commit()
        gpu = GpuScheduler(Factory).allocate(db, "ws-rec", gpu_requirement_gb=8)
        ws.gpu_id = gpu.id
        db.commit()

    before = _counter()
    stats = orchestrator.reconcile_all()

    assert stats["failed"] == 1, stats
    assert _counter() - before == SEGMENT_SECONDS, (
        f"reconcile 这一档入账了却没抬 counter：动了 {_counter() - before}"
    )
    with Factory() as db:
        assert _ledger_sum(db, "ws-rec") == SEGMENT_SECONDS
        assert db.get(Workspace, "ws-rec").status == WorkspaceStatus.FAILED.value
        card = db.scalar(select(Gpu))
        assert card is not None and card.status == GpuStatus.AVAILABLE.value, (
            f"入账抬了 counter，卡却还回不了池：{card.status if card else None}"
        )
        assert db.scalar(
            select(CreditLedger).where(CreditLedger.type == str(LedgerType.USAGE))
        ) is not None


def test_replay_of_the_same_segment_still_records_nothing_extra(monkeypatch) -> None:
    """搬抬点不能把重放变成双计：同一段结算两次，counter 只动一次 SEGMENT。"""
    orchestrator, clock, wid = _running_segment(monkeypatch)
    before = _counter()
    with Factory() as db:
        orchestrator._settle_run(db, db.get(Workspace, wid))
    clock.advance(60)
    with Factory() as db:
        orchestrator._settle_run(db, db.get(Workspace, wid))

    moved = _counter() - before
    assert moved == SEGMENT_SECONDS, (
        f"同一段结算两次之后 counter 应只动 {SEGMENT_SECONDS}，实读 {moved}"
        "（多了＝重放被再计一次；少了＝这条路径根本没抬，正是 N-89 的少计）"
    )
    with Factory() as db:
        assert _ledger_sum(db, wid) == SEGMENT_SECONDS


def test_a_segment_without_started_at_records_nothing(monkeypatch) -> None:
    """没运行过就没有账本条目，也就没有 counter：0 秒段的两个面一起钉。"""
    clock = Clock(datetime.now(UTC))
    monkeypatch.setattr(orchestrator_module, "utcnow", clock)
    orchestrator = _orchestrator()
    with Factory() as db:
        db.add(
            Workspace(
                id="ws-idle",
                name="idle",
                template_id="cartpole",
                provider="mock",
                user_id="u1",
                status=WorkspaceStatus.RUNNING.value,
                started_at=None,
            )
        )
        db.commit()

    before = _counter()
    with Factory() as db:
        booked = orchestrator._settle_run(db, db.get(Workspace, "ws-idle"))
        db.commit()

    assert booked == 0
    assert _counter() == before, "无运行段的结算抬了 counter"
    with Factory() as db:
        assert _ledger_sum(db, "ws-idle") == 0


# ---------------------------------------------------------------------------
# 结构：抬点必须在结算里，而且只有一处
# ---------------------------------------------------------------------------


def increment_sites_by_function(source: str) -> dict[str, int]:
    """按函数统计 `record_gpu_seconds(...)` 的调用点（定义与文档字符串里的复述不算）。"""
    counts: dict[str, int] = {}
    tree = ast.parse(source)
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        hits = sum(
            1
            for call in (c for c in ast.walk(fn) if isinstance(c, ast.Call))
            if isinstance(call.func, ast.Name) and call.func.id == "record_gpu_seconds"
        )
        if hits:
            counts[fn.name] = hits
    return counts


def test_the_increment_lives_inside_the_settlement_helper() -> None:
    """整个 app/ 只有一处抬点，且它在 `_settle_run_delta` 里——两条臂不再各自决定记不记。"""
    tree = ast.parse(ORCHESTRATOR_SRC.read_text(encoding="utf-8"))
    per_function = increment_sites_by_function(ORCHESTRATOR_SRC.read_text(encoding="utf-8"))
    assert per_function == {"_settle_run_delta": 1}, per_function
    assert not any(
        isinstance(n, ast.FunctionDef) and n.name == "_finalize_stop" and "record_gpu_seconds"
        in ast.unparse(n)
        for n in ast.walk(tree)
    ), "_finalize_stop 又自己抬了一次 ⇒ 与结算层重复，重放翻倍回来"


def test_the_increment_placement_ruler_fires_on_the_split_shape() -> None:
    """反向对照：抬点散回各条臂（stop 一处、destroy 一处）时尺子必须点名两处。"""
    split = (
        "def _settle_run_delta(db, ws):\n"
        "    record_gpu_seconds(1)\n"
        "\n"
        "def _finalize_stop(db, ws):\n"
        "    record_gpu_seconds(2)\n"
    )
    assert increment_sites_by_function(split) == {"_settle_run_delta": 1, "_finalize_stop": 1}
    assert increment_sites_by_function("def only():\n    return 0\n") == {}
    # 文档字符串里复述这个名字不算一处调用
    quoted = 'def only():\n    """别在别处调 record_gpu_seconds。"""\n    return 0\n'
    assert increment_sites_by_function(quoted) == {}

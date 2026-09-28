"""Streaming 生命周期与 Workspace 生命周期耦合（B7）。

STOP:   streaming.stop → runtime.stop → billing settle → GPU release
DESTROY: streaming.stop → runtime.destroy → billing settle → GPU release
失败 cleanup 必须可 retry。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    CreditLedger,
    Gpu,
    GpuAllocation,
    GpuStatus,
    LedgerType,
    StreamingSession,
    StreamingStatus,
    Template,
    Workspace,
    WorkspaceStatus,
)
from app.services.ledger import CreditLedgerService
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.services.streaming import StreamingSessionService
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("stream-lifecycle"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


class TrackingMockProvider(MockProvider):
    def __init__(self):
        super().__init__("http://127.0.0.1:8000")
        self.stop_calls = 0
        self.destroy_calls = 0
        self.fail_stop_times = 0  # 前 N 次 stop 抛错
        self.fail_destroy_times = 0  # 前 N 次 destroy 抛错

    def stop(self, workspace):
        self.stop_calls += 1
        if self.fail_stop_times > 0:
            self.fail_stop_times -= 1
            raise RuntimeError("simulated: runtime stop failure")
        return None

    def destroy(self, workspace):
        self.destroy_calls += 1
        if self.fail_destroy_times > 0:
            self.fail_destroy_times -= 1
            raise RuntimeError("simulated: runtime destroy failure")


def _seed(db) -> None:
    GpuScheduler(Factory).sync_host(
        db,
        host_id="host-1",
        name="h1",
        address="127.0.0.1",
        provider="mock",
        gpus=[GpuInfo(gpu_uuid="gpu-1", model="RTX", memory_total=24564, index=0)],
    )
    t = Template(
        id="cartpole",
        slug="cartpole",
        name="cartpole",
        description="test",
        category="test",
        runtime="mock",
        launch_command="echo ok",
        enabled=True,
        requires_streaming=True,
        recommended_vram_gb=16,
        estimated_hourly_cost_cny=1.0,
    )
    db.add(t)
    db.commit()


def _running_workspace_with_stream(provider=None, orchestrator=None) -> tuple[WorkspaceOrchestrator, str, str]:
    """创建 RUNNING workspace + 一个 READY 的 streaming session。"""
    provider = provider or TrackingMockProvider()
    streaming = StreamingSessionService(Factory)
    # S108: /tmp 为测试隔离目录（有意使用）
    orchestrator = orchestrator or WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-stream-ws"), streaming=streaming  # noqa: S108
    )
    with Factory() as db:
        _seed(db)
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = ws.id
        orchestrator._start(wid)
        db.refresh(ws)
        assert ws.status == WorkspaceStatus.RUNNING.value
        # 制造活动流媒体会话（READY）
        session = StreamingSession(
            id="sess-1",
            workspace_id=wid,
            status=StreamingStatus.READY.value,
            signal_port=49100,
            media_port=47998,
        )
        db.add(session)
        ws.signal_port = 49100
        ws.media_port = 47998
        db.commit()
        sid = session.id
    return orchestrator, wid, sid


def _active_sessions(db, workspace_id: str) -> list[StreamingSession]:
    return list(
        db.scalars(
            select(StreamingSession).where(
                StreamingSession.workspace_id == workspace_id,
                StreamingSession.status != StreamingStatus.FAILED.value,
            )
        )
    )


def test_stop_terminates_streaming_and_releases_everything():
    """STOP：流会话关闭 + 端口释放 + runtime stop + GPU release。"""
    orchestrator, wid, sid = _running_workspace_with_stream()

    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator.stop(db, ws)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPED.value
        # 无活动 streaming session
        assert _active_sessions(db, wid) == []
        session = db.get(StreamingSession, sid)
        assert session.status == StreamingStatus.FAILED.value
        # streaming 端口可复用（已释放）
        assert session.signal_port is None
        assert session.media_port is None
        assert ws.signal_port is None
        assert ws.media_port is None
        # GPU 已释放
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value


def test_destroy_terminates_streaming_and_releases_everything():
    """DESTROY：流会话关闭 + 端口释放 + runtime destroy + GPU release + tombstone。"""
    orchestrator, wid, sid = _running_workspace_with_stream()

    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator.destroy(db, ws)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        # soft delete：行保留为 tombstone
        assert ws is not None
        assert ws.status == WorkspaceStatus.DELETED.value
        assert ws.deleted_at is not None
        assert _active_sessions(db, wid) == []
        session = db.get(StreamingSession, sid)
        assert session.status == StreamingStatus.FAILED.value
        assert session.signal_port is None
        assert session.media_port is None
        # GPU 已释放
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value


def test_failed_stop_cleanup_is_retryable():
    """runtime.stop 失败 → 留在 STOPPING（不是 FAILED）；再次 stop 完成全部 cleanup。

    N-63 改判：这条原先断"第一次尝试失败就写 FAILED"，钉的是 as-is 而不是应然。
    ADR 0002 的修订（可重试的失败不得写成终态）同样适用于 STOP，而且终态还会自己漏卡：
    `recover_stuck_gpu_allocations` 把"终态 workspace 的分配"当孤儿回收，写成 FAILED 等于
    让那条回收路绕过 `_release_admitted` 把还有人吃的卡放掉。现在没达成目标就留在
    STOPPING，由 operation 重试与 reconcile 收敛。
    """
    provider = TrackingMockProvider()
    provider.fail_stop_times = 1
    orchestrator, wid, _ = _running_workspace_with_stream(provider=provider)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator.stop(db, ws)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPING.value
        assert ws.error_message  # error persisted
        # 关键：这一轮没把 GPU 放掉（改前它会被重试路径直接放掉）
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is not None
        assert db.scalar(select(Gpu)).status != GpuStatus.AVAILABLE.value

    # retry：第二次 stop 先真把 runtime 停下，再完成 cleanup
    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator.stop(db, ws)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPED.value
        assert _active_sessions(db, wid) == []
        assert ws.signal_port is None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value


def test_stop_is_idempotent_no_double_release():
    """重复 stop：不重复结算（幂等 key）、不重复 release、状态保持 STOPPED。"""
    orchestrator, wid, _ = _running_workspace_with_stream()
    with Factory() as db:
        ws = db.get(Workspace, wid)
        ws.started_at = datetime.now(UTC) - timedelta(seconds=30)
        db.commit()

    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator.stop(db, ws)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator.stop(db, ws)  # 再次 stop：应保持 STOPPED

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPED.value
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value


def test_destroy_settle_failure_marks_auditable_and_still_releases():
    """结算抛异常：destroy 仍完成资源释放 + tombstone，并留下可审计痕迹（error_message）。"""
    orchestrator, wid, _ = _running_workspace_with_stream()

    class FailingLedger:
        def settle_workspace_run(self, db, workspace, seconds, started_iso):
            raise RuntimeError("simulated ledger outage")

    orchestrator.ledger = FailingLedger()

    with Factory() as db:
        ws = db.get(Workspace, wid)
        ws.started_at = datetime.now(UTC) - timedelta(seconds=30)
        db.commit()
        orchestrator.destroy(db, ws)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.DELETED.value
        assert ws.deleted_at is not None
        # 可审计痕迹：结算失败被记录，而非静默丢账
        assert ws.error_message and "settle failed" in ws.error_message
        # 资源释放不被结算失败阻断
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None


def test_destroy_settle_failure_leaves_the_segment_unbooked_and_stays_that_way():
    """N-105 的判决：结算失败丢掉的那一段**没人补**，而且不许有人按时钟补。

    代码注释原先写「修复方向是可补偿」（`app/services/orchestrator.py:618-619`）——本轮查清那是
    一条没有归属者的主张：`reconcile_all` 的循环先跳过 tombstone（`:687-688`），
    `monitor_runtime_quotas` 的选择集要求 `deleted_at IS NULL`，
    `recover_stuck_gpu_allocations` 只管卡不管账。而唯一还能算这一段的那一层
    （`_settle_run`，`:550-556`）按 `utcnow() - started_at` 取秒——对墓碑格那就是把"等待时长"
    计成"运行时长"。所以应然定在：**这一段就是不入账**，本档钉两件事——
    ① 失败当场没有任何 USAGE 条目，且周期驱动者再跑一趟也不会冒出一条；
    ② 墓碑的 `started_at` 被清空，留下"这一段结束了、未计费"的事实，而不是"还欠一段"的假象。

    牙齿（四臂变异电池实测，2026-09-28；各臂恢复后 `cmp` 逐字节相同、末跑 8 passed）：
    基线与无关注释臂 0 红。F1 让墓碑保留 `started_at` ⇒ 本档与对照档 E2 一起红（两支都读这一列）。
    F3 把 `_settle_running_segment` 的状态守卫改成恒假（destroy 从不尝试结算）⇒ E2 红，
    连带把既有那支 `test_destroy_settle_failure_marks_auditable_and_still_releases` 也打红——
    它钉的"留下可审计痕迹"其实依赖结算**被尝试过**，这是本轮顺带量出的一条既有依赖。
    本档"零条目"那一面**没有任何现存改动能让它红**（今天没有任何路径会为墓碑写 USAGE），
    它防的是尚不存在的补做者；"这个 0 不是恒真"由 E2 用同一个查询证明（对照档读出恰好 1 条）。
    """
    orchestrator, wid, _ = _running_workspace_with_stream()

    class FailingLedger:
        def settle_workspace_run(self, db, workspace, seconds, started_iso):
            raise RuntimeError("simulated ledger outage")

    orchestrator.ledger = FailingLedger()
    with Factory() as db:
        ws = db.get(Workspace, wid)
        ws.started_at = datetime.now(UTC) - timedelta(seconds=3600)  # 真的跑过一小时
        ws.accumulated_seconds = 0
        db.commit()
        orchestrator.destroy(db, ws)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.DELETED.value
        usage = list(
            db.scalars(
                select(CreditLedger).where(
                    CreditLedger.workspace_id == wid,
                    CreditLedger.type == LedgerType.USAGE.value,
                )
            )
        )
        assert usage == [], f"结算失败却留下了条目，本档的形状就不是这一条：{[u.gpu_seconds for u in usage]}"
        assert ws.started_at is None, "墓碑还留着 started_at ⇒ 看上去像有一段没算的账"
        assert ws.accumulated_seconds == 0, ws.accumulated_seconds

    # 换回真账本再跑一趟周期驱动者：没有任何一条路会把这一小时补进来（那会把等待算成运行）
    orchestrator.ledger = CreditLedgerService(Factory)
    orchestrator.reconcile_all()
    with Factory() as db:
        assert db.scalar(
            select(func.count(CreditLedger.id)).where(
                CreditLedger.workspace_id == wid,
                CreditLedger.type == LedgerType.USAGE.value,
            )
        ) == 0, "出现了补做者，而它按的是 utcnow()——这一笔会多计一小时"
        assert db.get(Workspace, wid).started_at is None


def test_destroy_books_the_segment_when_the_ledger_answers():
    """对照档：同一夹具、同一入口，账本没坏时必须正好入一段账。

    没有这一支，上一档的"零条目"可能只是 destroy 从不结算——那比丢一段账更坏。
    """
    orchestrator, wid, _ = _running_workspace_with_stream()
    with Factory() as db:
        ws = db.get(Workspace, wid)
        ws.started_at = datetime.now(UTC) - timedelta(seconds=3600)
        db.commit()
        orchestrator.destroy(db, ws)

    with Factory() as db:
        entries = list(
            db.scalars(
                select(CreditLedger).where(
                    CreditLedger.workspace_id == wid,
                    CreditLedger.type == LedgerType.USAGE.value,
                )
            )
        )
        assert len(entries) == 1, [e.gpu_seconds for e in entries]
        assert 3590 <= (entries[0].gpu_seconds or 0) <= 3600, entries[0].gpu_seconds
        assert db.get(Workspace, wid).started_at is None


def test_destroy_provider_failure_does_not_release_or_tombstone():
    """provider.destroy 失败：不释放 GPU、不置 DELETED，异常上抛由 DESTROY 重试。"""
    provider = TrackingMockProvider()
    provider.fail_destroy_times = 1
    orchestrator, wid, _ = _running_workspace_with_stream(provider=provider)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        with pytest.raises(RuntimeError, match="runtime destroy failure"):
            orchestrator.destroy(db, ws)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        # 未 tombstone、GPU 未释放（容器可能仍持有 GPU，不能双跑）
        assert ws.deleted_at is None
        assert ws.status != WorkspaceStatus.DELETED.value
        assert db.scalar(select(Gpu)).status == GpuStatus.ALLOCATED.value
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is not None

    # 重试：provider 恢复后 destroy 完成清理
    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator.destroy(db, ws)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.DELETED.value
        assert ws.deleted_at is not None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None

"""Streaming 生命周期与 Workspace 生命周期耦合（B7）。

STOP:   streaming.stop → runtime.stop → billing settle → GPU release
DESTROY: streaming.stop → runtime.destroy → billing settle → GPU release
失败 cleanup 必须可 retry。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    Gpu,
    GpuAllocation,
    GpuStatus,
    StreamingSession,
    StreamingStatus,
    Template,
    Workspace,
    WorkspaceStatus,
)
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.services.streaming import StreamingSessionService

ENGINE = create_engine("sqlite:///./test-stream-lifecycle.db", connect_args={"check_same_thread": False})
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

    def stop(self, workspace):
        self.stop_calls += 1
        if self.fail_stop_times > 0:
            self.fail_stop_times -= 1
            raise RuntimeError("simulated: runtime stop failure")
        return None

    def destroy(self, workspace):
        self.destroy_calls += 1


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
    """DESTROY：流会话关闭 + 端口释放 + runtime destroy + GPU release + 行删除。"""
    orchestrator, wid, sid = _running_workspace_with_stream()

    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator.destroy(db, ws)

    with Factory() as db:
        assert db.get(Workspace, wid) is None
        assert _active_sessions(db, wid) == []
        session = db.get(StreamingSession, sid)
        assert session.status == StreamingStatus.FAILED.value
        assert session.signal_port is None
        assert session.media_port is None
        # GPU 已释放
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value


def test_failed_stop_cleanup_is_retryable():
    """runtime.stop 失败 → FAILED；再次 stop 可完成全部 cleanup（幂等 retry）。"""
    provider = TrackingMockProvider()
    provider.fail_stop_times = 1
    orchestrator, wid, _ = _running_workspace_with_stream(provider=provider)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator.stop(db, ws)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.FAILED.value
        assert ws.error_message  # error persisted

    # retry：第二次 stop 完成 cleanup
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

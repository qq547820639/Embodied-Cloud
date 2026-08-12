"""Durable WorkspaceOperation + reconciliation 测试。

验证：
- API restart 后 PENDING operation 不丢（新 worker 可继续执行）
- RUNNING operation 有 lease；过期可重新 claim
- 同一 workspace 冲突 operation 串行（enqueue 拒绝）
- 失败重试至 MAX_ATTEMPTS → FAILED
- reconcile 规则（adopt/FAILED+release/requeue/STOPPED）与幂等性
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
    OperationStatus,
    OperationType,
    Template,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.base import RuntimeState
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.services.worker import OperationWorker

ENGINE = create_engine("sqlite:///./test-worker.db", connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


class ControllableMockProvider(MockProvider):
    """mock provider + 可控 reconcile 结果。"""

    def __init__(self, reconcile_state: RuntimeState = RuntimeState.UNKNOWN):
        super().__init__("http://127.0.0.1:8000")
        self.reconcile_state = reconcile_state
        self.destroy_calls = 0
        self.stop_calls = 0

    def reconcile(self, workspace):
        return self.reconcile_state

    def destroy(self, workspace):
        self.destroy_calls += 1

    def stop(self, workspace):
        self.stop_calls += 1


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
        recommended_vram_gb=16,
        estimated_hourly_cost_cny=1.0,
    )
    db.add(t)
    db.commit()


def _make_orchestrator(provider=None) -> tuple[WorkspaceOrchestrator, ControllableMockProvider]:
    provider = provider or ControllableMockProvider()
    orchestrator = WorkspaceOrchestrator(Factory, provider, Path("/tmp/test-worker-ws"))  # noqa: S108
    return orchestrator, provider


def _make_workspace(orchestrator, db) -> Workspace:
    return orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")


# ---------------------------------------------------------------------------
# durable operations
# ---------------------------------------------------------------------------


def test_pending_operation_survives_restart():
    """模拟 API 重启：enqueue 后新 worker 实例 claim 并执行，operation 不丢。"""
    orchestrator, _ = _make_orchestrator()
    with Factory() as db:
        _seed(db)
        wid = _make_workspace(orchestrator, db).id

    op = orchestrator.start_async(wid)
    assert op is not None
    assert op.status == OperationStatus.PENDING.value

    # 模拟重启：完全新的 worker（不共享任何内存状态），只读 DB
    worker2 = OperationWorker(Factory, orchestrator)
    processed = worker2.tick_once()

    assert processed == 1
    with Factory() as db:
        op = db.get(WorkspaceOperation, op.id)
        assert op.status == OperationStatus.SUCCEEDED.value
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value


def test_lease_expiry_reclaim():
    """RUNNING 且 lease 过期 → 新 worker 可重新 claim 执行。"""
    orchestrator, _ = _make_orchestrator()
    with Factory() as db:
        _seed(db)
        wid = _make_workspace(orchestrator, db).id

    op = orchestrator.start_async(wid)
    # 手动模拟：被旧 worker claim 后崩溃（RUNNING + 过期 lease）
    with Factory() as db:
        db_op = db.get(WorkspaceOperation, op.id)
        db_op.status = OperationStatus.RUNNING.value
        db_op.attempts = 1
        db_op.started_at = datetime.now(UTC) - timedelta(minutes=5)
        db_op.lease_expires_at = datetime.now(UTC) - timedelta(minutes=4)
        db.commit()

    worker2 = OperationWorker(Factory, orchestrator)
    assert worker2.tick_once() == 1
    with Factory() as db:
        db_op = db.get(WorkspaceOperation, op.id)
        assert db_op.status == OperationStatus.SUCCEEDED.value
        assert db_op.attempts == 2  # 重新 claim 累加 attempts


def test_serialization_same_workspace():
    """同一 workspace 已有 active operation → enqueue 拒绝（串行）。"""
    orchestrator, _ = _make_orchestrator()
    with Factory() as db:
        _seed(db)
        wid = _make_workspace(orchestrator, db).id

    op1 = orchestrator.start_async(wid)
    assert op1 is not None
    op2 = orchestrator.start_async(wid)
    assert op2 is None  # 串行：拒绝第二个 PROVISION
    # 不同 workspace 不受影响
    with Factory() as db:
        wid2 = _make_workspace(orchestrator, db).id
    op3 = orchestrator.start_async(wid2)
    assert op3 is not None


def test_operation_failure_retries_then_failed():
    """provider 持续失败 → attempts 到 MAX_ATTEMPTS → operation FAILED。"""
    provider = ControllableMockProvider()
    provider.provision = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    orchestrator, _ = _make_orchestrator(provider)
    with Factory() as db:
        _seed(db)
        wid = _make_workspace(orchestrator, db).id

    op = orchestrator.start_async(wid)
    worker = OperationWorker(Factory, orchestrator)
    # 失败后立即重试（测试加速：backoff 置 0）
    OperationWorker.RETRY_BASE_DELAY = 0
    # 逐个 tick，观察状态流转：RUNNING → RETRYING → … → FAILED
    statuses = []
    for _ in range(OperationWorker.MAX_ATTEMPTS + 2):
        # 确定性：显式把 RETRYING backoff lease 置为过期（不依赖真实时钟推进）
        with Factory() as db:
            db_op = db.get(WorkspaceOperation, op.id)
            if db_op is not None and db_op.status == OperationStatus.RETRYING.value:
                db_op.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                db.commit()
        worker.tick_once()
        with Factory() as db:
            statuses.append(db.get(WorkspaceOperation, op.id).status)
    # 最终状态必须是 FAILED
    with Factory() as db:
        db_op = db.get(WorkspaceOperation, op.id)
        assert db_op.status == OperationStatus.FAILED.value
        assert db_op.last_error == "boom"
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.FAILED.value
        assert ws.error_message == "boom"
        # GPU 已释放（provision 失败补偿）
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_running_alive_adopted():
    """DB RUNNING + runtime ALIVE → 保持 RUNNING（adopt），不释放 GPU。"""
    provider = ControllableMockProvider(reconcile_state=RuntimeState.ALIVE)
    orchestrator, _ = _make_orchestrator(provider)
    with Factory() as db:
        _seed(db)
        wid = _make_workspace(orchestrator, db).id
        orchestrator.start_async(wid)
    worker = OperationWorker(Factory, orchestrator)
    worker.tick_once()  # provision → RUNNING

    stats = orchestrator.reconcile_all()
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value
        assert db.scalar(select(Gpu)).status == GpuStatus.ALLOCATED.value
    assert stats["kept"] == 1


def test_reconcile_running_missing_failed_and_released():
    """DB RUNNING + runtime MISSING → FAILED + 结算 + 释放 GPU。"""
    provider = ControllableMockProvider(reconcile_state=RuntimeState.MISSING)
    orchestrator, _ = _make_orchestrator(provider)
    with Factory() as db:
        _seed(db)
        wid = _make_workspace(orchestrator, db).id
        orchestrator.start_async(wid)
    worker = OperationWorker(Factory, orchestrator)
    worker.tick_once()

    # 置为 RUNNING 且已启动一段（started_at 在过去）
    with Factory() as db:
        ws = db.get(Workspace, wid)
        ws.started_at = datetime.now(UTC) - timedelta(seconds=30)

    stats = orchestrator.reconcile_all()
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.FAILED.value
        assert "runtime missing" in ws.error_message
        gpu = db.scalar(select(Gpu))
        assert gpu.status == GpuStatus.AVAILABLE.value
        assert gpu.workspace_id is None
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None
    assert stats["failed"] == 1


def test_reconcile_provisioning_missing_requeues():
    """DB PROVISIONING + runtime MISSING + 无 active operation → 重新入队。"""
    provider = ControllableMockProvider(reconcile_state=RuntimeState.MISSING)
    orchestrator, _ = _make_orchestrator(provider)
    with Factory() as db:
        _seed(db)
        wid = _make_workspace(orchestrator, db).id
        ws = db.get(Workspace, wid)
        ws.status = WorkspaceStatus.PROVISIONING.value
        db.commit()

    stats = orchestrator.reconcile_all()
    assert stats["requeued"] == 1
    with Factory() as db:
        op = db.scalar(
            select(WorkspaceOperation).where(
                WorkspaceOperation.workspace_id == wid,
                WorkspaceOperation.status == OperationStatus.PENDING.value,
            )
        )
        assert op is not None
        assert op.operation_type == OperationType.PROVISION.value


def test_reconcile_stopping_missing_becomes_stopped():
    """DB STOPPING + runtime 缺失 → 结算 + 释放 + STOPPED。"""
    provider = ControllableMockProvider(reconcile_state=RuntimeState.MISSING)
    orchestrator, _ = _make_orchestrator(provider)
    with Factory() as db:
        _seed(db)
        wid = _make_workspace(orchestrator, db).id
        ws = db.get(Workspace, wid)
        ws.status = WorkspaceStatus.STOPPING.value
        ws.started_at = datetime.now(UTC) - timedelta(seconds=10)
        db.commit()

    stats = orchestrator.reconcile_all()
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPED.value
        assert ws.started_at is None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value
    assert stats["stopped"] == 1


def test_reconcile_idempotent():
    """连续两次 reconcile 结果一致：不重复结算、不重复 release、不误杀。"""
    provider = ControllableMockProvider(reconcile_state=RuntimeState.ALIVE)
    orchestrator, _ = _make_orchestrator(provider)
    with Factory() as db:
        _seed(db)
        wid = _make_workspace(orchestrator, db).id
        orchestrator.start_async(wid)
    worker = OperationWorker(Factory, orchestrator)
    worker.tick_once()

    stats1 = orchestrator.reconcile_all()
    stats2 = orchestrator.reconcile_all()

    assert stats1 == stats2
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value
        gpu = db.scalar(select(Gpu))
        assert gpu.status == GpuStatus.ALLOCATED.value
        assert gpu.workspace_id == wid


def test_reconcile_unknown_mock_keeps_state():
    """mock（UNKNOWN，无真实 runtime 可判定）→ 状态不动，不误杀。"""
    provider = ControllableMockProvider(reconcile_state=RuntimeState.UNKNOWN)
    orchestrator, _ = _make_orchestrator(provider)
    with Factory() as db:
        _seed(db)
        wid = _make_workspace(orchestrator, db).id
        orchestrator.start_async(wid)
    worker = OperationWorker(Factory, orchestrator)
    worker.tick_once()

    stats = orchestrator.reconcile_all()
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value
    assert stats["kept"] == 0  # UNKNOWN 不动，也不计入 kept

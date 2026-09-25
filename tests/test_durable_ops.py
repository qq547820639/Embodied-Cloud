"""Durable STOP/DESTROY（§12，P0）。

POST stop → enqueue STOP operation；DELETE → enqueue DESTROY operation；
OperationWorker 执行（PROVISION/START/STOP/DESTROY/RECONCILE 全覆盖）。
控制面重启不能让外部 cleanup 永久丢失。
"""

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
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.services.worker import OperationWorker
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("durable-stop"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


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


def _make_orchestrator(provider=None) -> tuple[WorkspaceOrchestrator, MockProvider]:
    provider = provider or MockProvider("http://127.0.0.1:8000")
    orchestrator = WorkspaceOrchestrator(Factory, provider, Path("/tmp/test-durable"))  # noqa: S108
    return orchestrator, provider


def _running_ws(db, orchestrator) -> str:
    ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
    wid = ws.id
    orchestrator._start(wid)
    db.refresh(ws)
    assert ws.status == WorkspaceStatus.RUNNING.value
    return wid


def test_stop_is_durable_operation():
    """POST stop 语义：enqueue STOP operation（持久化）→ worker 执行 → STOPPED。"""
    orchestrator, _ = _make_orchestrator()
    worker = OperationWorker(Factory, orchestrator)
    with Factory() as db:
        _seed(db)
        wid = _running_ws(db, orchestrator)

    op = worker.enqueue(wid, OperationType.STOP)
    assert op is not None
    assert op.operation_type == OperationType.STOP.value
    # 模拟"重启"：新 worker 实例（只读 DB）执行
    worker2 = OperationWorker(Factory, orchestrator)
    assert worker2.tick_once() == 1

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPED.value
        op = db.get(WorkspaceOperation, op.id)
        assert op.status == OperationStatus.SUCCEEDED.value
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value


def test_restart_during_stop_recovers():
    """stop 入队后控制面"重启"（不执行）→ 新 worker 恢复执行，cleanup 完成。"""
    orchestrator, _ = _make_orchestrator()
    with Factory() as db:
        _seed(db)
        wid = _running_ws(db, orchestrator)

    # enqueue 后不执行（模拟崩溃/重启）
    worker_a = OperationWorker(Factory, orchestrator)
    op = worker_a.enqueue(wid, OperationType.STOP)
    assert op is not None
    with Factory() as db:
        assert db.get(WorkspaceOperation, op.id).status == OperationStatus.PENDING.value

    # 重启后的新 worker 恢复
    worker_b = OperationWorker(Factory, orchestrator)
    assert worker_b.tick_once() == 1
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPED.value
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value


def test_restart_during_destroy_recovers():
    """destroy 入队后重启 → 新 worker 恢复执行，tombstone 完成 + GPU 释放。"""
    orchestrator, _ = _make_orchestrator()
    with Factory() as db:
        _seed(db)
        wid = _running_ws(db, orchestrator)

    worker_a = OperationWorker(Factory, orchestrator)
    op = worker_a.enqueue(wid, OperationType.DESTROY)
    assert op is not None
    # 重启
    worker_b = OperationWorker(Factory, orchestrator)
    assert worker_b.tick_once() == 1
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.DELETED.value
        assert ws.deleted_at is not None
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value


def test_duplicate_stop_is_idempotent():
    """重复 STOP operation：第二次不重复结算/释放，状态保持 STOPPED。"""
    orchestrator, _ = _make_orchestrator()
    worker = OperationWorker(Factory, orchestrator)
    with Factory() as db:
        _seed(db)
        wid = _running_ws(db, orchestrator)

    op1 = worker.enqueue(wid, OperationType.STOP)
    assert op1 is not None
    worker.tick_once()
    # 再次 stop（API 重复调用）：enqueue 成功（无 active op）→ 幂等执行
    op2 = worker.enqueue(wid, OperationType.STOP)
    assert op2 is not None
    worker.tick_once()
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPED.value
        # GPU 已释放且未被二次分配
        gpu = db.scalar(select(Gpu))
        assert gpu.status == GpuStatus.AVAILABLE.value


def test_duplicate_destroy_is_idempotent():
    """重复 DESTROY operation：tombstone 后再次 destroy 幂等（不报错）。"""
    orchestrator, _ = _make_orchestrator()
    worker = OperationWorker(Factory, orchestrator)
    with Factory() as db:
        _seed(db)
        wid = _running_ws(db, orchestrator)

    worker.enqueue(wid, OperationType.DESTROY)
    worker.tick_once()
    # 再次 destroy（直接调用，模拟 API 重复删除路径）
    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator.destroy(db, ws)  # 已 tombstone → 幂等返回
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.deleted_at is not None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value

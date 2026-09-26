"""Durable WorkspaceOperation + reconciliation 测试。

验证：
- API restart 后 PENDING operation 不丢（新 worker 可继续执行）
- RUNNING operation 有 lease；过期可重新 claim
- 同一 workspace 冲突 operation 串行（enqueue 拒绝）
- 失败重试至 MAX_ATTEMPTS → FAILED
- **没到 MAX_ATTEMPTS 的失败不得写成 workspace 终态**（否则读者看到假死、下一轮又活）
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
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("worker"), connect_args={"check_same_thread": False})
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
    # 失败后立即重试（测试加速：backoff 置 0）；结束必须还原（全局类属性，
    # 泄漏会污染其它用例的重试时序）
    original_delay = OperationWorker.RETRY_BASE_DELAY
    OperationWorker.RETRY_BASE_DELAY = 0
    try:
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
    finally:
        OperationWorker.RETRY_BASE_DELAY = original_delay
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


class OnceFlakyProvider(ControllableMockProvider):
    """只在第 1 次 provision 尝试抛错的 provider：制造"这次失败、但还要重试"的确定前提。"""

    def __init__(self, error: str = "No GPU available with >= 16 GB VRAM (workspace transient)"):
        super().__init__()
        self.error = error
        self.provision_calls = 0

    def provision(self, workspace, template, root, reservation):  # type: ignore[override]
        self.provision_calls += 1
        if self.provision_calls == 1:
            raise RuntimeError(self.error)
        return super().provision(workspace, template, root, reservation)


def test_retryable_provision_failure_is_not_published_as_terminal():
    """operation 还要重试时，workspace 的 status 不得替它下终态结论。

    起因（两次实测同一 victim，现场见 dist/validate-junit.xml 与本轮诊断留痕）：共享
    mock 卡池被上游用例借走的那 1s 里，provision 第 1 次尝试报 `No GPU available`，
    `_fail()` 当场把 workspace 写成 FAILED；随后第 2 次尝试成功，同一个 workspace 又
    变回 RUNNING。期间任何 `GET /api/workspaces/{id}` 的读者（真实用户的前端、以及
    tests/test_workspace_credential.py 这类等"收敛"的用例）看到的都是"这个任务已经死了"。

    为什么必须由 status 承担这个信息：`workspace_operations` 在 API 层零读者
    （`grep -rn WorkspaceOperation app/routers/` 为 0），所以"还在重试"这件事如果没有
    写进 status，就根本没有读者——下一轮尝试开头又会把 error_message 清成 None，
    那句"失败"连痕迹都不留。
    """
    provider = OnceFlakyProvider()
    orchestrator, _ = _make_orchestrator(provider)
    with Factory() as db:
        _seed(db)
        wid = _make_workspace(orchestrator, db).id

    op = orchestrator.start_async(wid)
    assert op is not None
    worker = OperationWorker(Factory, orchestrator)
    original_delay = OperationWorker.RETRY_BASE_DELAY
    OperationWorker.RETRY_BASE_DELAY = 0  # 测试加速；finally 必须还原（全局类属性）
    try:
        assert worker.tick_once() == 1
        with Factory() as db:
            db_op = db.get(WorkspaceOperation, op.id)
            db_ws = db.get(Workspace, wid)
            assert db_op is not None and db_ws is not None
            # 前提确认：这次失败确实是"还要重试"的失败，不是终态
            assert db_op.status == OperationStatus.RETRYING.value, db_op.status
            assert db_op.attempts < OperationWorker.MAX_ATTEMPTS, db_op.attempts
            # 判据：operation 还活着，workspace 就不许是 FAILED
            assert db_ws.status != WorkspaceStatus.FAILED.value, (
                f"第 {db_op.attempts}/{OperationWorker.MAX_ATTEMPTS} 次尝试失败就被写成终态 "
                f"FAILED，而 worker 还要重试（随后同一 workspace 会自己回到 RUNNING）："
                f"{db_ws.error_message}"
            )
            # 排队等下一次尝试，才是此刻的真话
            assert db_ws.status == WorkspaceStatus.QUEUED.value, db_ws.status
            # 上一次尝试为什么没成，仍然要看得见
            assert "No GPU available" in (db_ws.error_message or ""), db_ws.error_message
            # 卡必须回到池子里（否则重试永远抢不到，且会把池子抽干）
            gpu = db.scalar(select(Gpu))
            assert gpu is not None and gpu.status == GpuStatus.AVAILABLE.value, gpu.status
            assert db_ws.gpu_id is None

        with Factory() as db:  # 确定性推进 backoff：显式让 RETRYING 的 lease 过期
            db_op = db.get(WorkspaceOperation, op.id)
            assert db_op is not None
            db_op.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()
        assert worker.tick_once() == 1
        with Factory() as db:
            assert db.get(WorkspaceOperation, op.id) is not None
            assert db.get(WorkspaceOperation, op.id).status == OperationStatus.SUCCEEDED.value
            ws = db.get(Workspace, wid)
            assert ws is not None
            assert ws.status == WorkspaceStatus.RUNNING.value, ws.error_message
            # 成功那一轮要把"上一次尝试失败"的措辞清掉，否则读者以为还在失败
            assert ws.error_message is None, ws.error_message
    finally:
        OperationWorker.RETRY_BASE_DELAY = original_delay
    assert provider.provision_calls == 2


def test_terminal_provision_failure_keeps_failed_status():
    """反面（极性对照）：attempts 用尽的那一次必须还是 FAILED，且错误原因就是最后一次尝试的。

    没有这一支，上一条可以被"永远不写 FAILED"这种假修法骗过。
    """
    provider = OnceFlakyProvider(error="always boom")

    def always_fail(workspace, template, root, reservation):
        provider.provision_calls += 1
        raise RuntimeError("always boom")

    provider.provision = always_fail  # type: ignore[method-assign]
    orchestrator, _ = _make_orchestrator(provider)
    with Factory() as db:
        _seed(db)
        wid = _make_workspace(orchestrator, db).id
    op = orchestrator.start_async(wid)
    assert op is not None

    worker = OperationWorker(Factory, orchestrator)
    original_delay = OperationWorker.RETRY_BASE_DELAY
    OperationWorker.RETRY_BASE_DELAY = 0
    try:
        for _ in range(OperationWorker.MAX_ATTEMPTS):
            with Factory() as db:
                db_op = db.get(WorkspaceOperation, op.id)
                assert db_op is not None
                if db_op.status == OperationStatus.RETRYING.value:
                    db_op.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    db.commit()
            assert worker.tick_once() == 1
            with Factory() as db:
                db_op = db.get(WorkspaceOperation, op.id)
                db_ws = db.get(Workspace, wid)
                assert db_op is not None and db_ws is not None
                if db_op.attempts < OperationWorker.MAX_ATTEMPTS:
                    # 非终态的那几轮：不许出现 FAILED（同上一条判据，逐轮检查）
                    assert db_ws.status != WorkspaceStatus.FAILED.value, (
                        f"第 {db_op.attempts} 次尝试失败就被写成终态：{db_ws.error_message}"
                    )
                else:
                    assert db_op.status == OperationStatus.FAILED.value, db_op.status
                    assert db_ws.status == WorkspaceStatus.FAILED.value, db_ws.status
                    assert db_ws.error_message == "always boom", db_ws.error_message
    finally:
        OperationWorker.RETRY_BASE_DELAY = original_delay
    assert provider.provision_calls == OperationWorker.MAX_ATTEMPTS


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

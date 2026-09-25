"""Operation lease/fencing 正确性（§5，P0）。

- long_running_operation_renews_lease: 长操作执行期间 heartbeat 续期 → SUCCEEDED
- expired_worker_cannot_finish_after_reclaim: 过期 worker 不能写终态（fencing）
- two_workers_cannot_execute_same_operation: 并发 claim 至多一个持有
- worker_crash_allows_safe_reclaim: 崩溃 worker 的 lease 过期后可安全 reclaim
"""

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    Gpu,
    GpuHost,
    OperationStatus,
    OperationType,
    Template,
    Workspace,
    WorkspaceOperation,
)
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.docker import DockerProvider
from app.services.providers.mock import MockProvider
from app.services.worker import LeaseLostError, OperationWorker
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("worker-fencing"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


class SlowMockProvider(MockProvider):
    """provision 耗时可控的 mock（模拟长 provision）。"""

    def __init__(self, delay: float = 0.0, fail: bool = False):
        super().__init__("http://127.0.0.1:8000")
        self.delay = delay
        self.fail = fail

    def provision(self, workspace, template, workspace_dir, reservation=None):
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("simulated provision failure")
        return super().provision(workspace, template, workspace_dir, reservation)


def _seed(db) -> None:
    db.add(GpuHost(id="host-1", name="h1", address="127.0.0.1", provider="mock"))
    db.add(
        Gpu(
            id="gpu-1",
            gpu_uuid="gpu-1",
            host_id="host-1",
            model="Mock GPU",
            memory_total=32768,
            gpu_index=0,
        )
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


def _make_ws(db) -> str:
    provider = SlowMockProvider()
    orchestrator = WorkspaceOrchestrator(Factory, provider, Path("/tmp/test-fencing"))  # noqa: S108
    ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
    return ws.id


def _op_status(op_id: str) -> tuple[str, str | None, str | None]:
    with Factory() as db:
        op = db.get(WorkspaceOperation, op_id)
        return op.status, op.lease_owner, op.fencing_token


def test_long_running_operation_renews_lease(monkeypatch):
    """长 provision（> lease 时长）：heartbeat 持续续期，最终 SUCCEEDED。"""
    old_lease = OperationWorker.LEASE_SECONDS
    old_hb = OperationWorker.HEARTBEAT_INTERVAL
    try:
        OperationWorker.LEASE_SECONDS = 0.5
        OperationWorker.HEARTBEAT_INTERVAL = 0.1
        with Factory() as db:
            _seed(db)
            wid = _make_ws(db)
            orchestrator = WorkspaceOrchestrator(
                Factory, SlowMockProvider(delay=1.2), Path("/tmp/test-fencing2")  # noqa: S108
            )
        worker = OperationWorker(Factory, orchestrator)
        op = worker.enqueue(wid, OperationType.PROVISION)
        assert op is not None

        processed = worker.tick_once()  # 同步执行（1.2s > lease 0.5s，依赖 heartbeat 续期）
        assert processed == 1
        status, owner, _ = _op_status(op.id)
        assert status == OperationStatus.SUCCEEDED.value
        assert owner == worker.worker_id
    finally:
        OperationWorker.LEASE_SECONDS = old_lease
        OperationWorker.HEARTBEAT_INTERVAL = old_hb


def test_expired_worker_cannot_finish_after_reclaim():
    """worker A lease 过期并被 B reclaim 后：A 不能写终态（fencing 拒绝）。"""
    worker_a = OperationWorker(Factory, object())  # executor 不用
    worker_b = OperationWorker(Factory, object())
    with Factory() as db:
        _seed(db)
        wid = _make_ws(db)
        op = worker_a.enqueue(wid, OperationType.PROVISION)
        assert op is not None
        op_id = op.id

    # A claim（原子）→ 手动让 lease 过期
    with Factory() as db:
        db_op = db.get(WorkspaceOperation, op_id)
        assert worker_a._try_claim(db, db_op, datetime.now(UTC)) is True
        db_op = db.get(WorkspaceOperation, op_id)
        db_op.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)
        db.commit()
        token_a = db_op.fencing_token

    # B reclaim（过期 RUNNING 可 reclaim）
    with Factory() as db:
        db_op = db.get(WorkspaceOperation, op_id)
        assert worker_b._try_claim(db, db_op, datetime.now(UTC)) is True
        token_b = db.get(WorkspaceOperation, op_id).fencing_token
        assert token_b != token_a

    # A 尝试写终态（旧 token 的本地副本）→ LeaseLostError，状态未被修改。
    # 用独立构造的对象模拟 A 的本地快照（避免 ORM autoflush 把旧 token 写回 DB）
    with Factory() as db:
        stale_snapshot = WorkspaceOperation(
            id=op_id, workspace_id="ws-1", operation_type="provision",
            status=OperationStatus.RUNNING.value, fencing_token=token_a,
        )
        with pytest.raises(LeaseLostError):
            worker_a.finish_success(db, stale_snapshot)
    status, owner, _ = _op_status(op_id)
    assert status == OperationStatus.RUNNING.value  # 仍是 B 的 RUNNING，未被 A 置终态
    assert owner == worker_b.worker_id


def test_two_workers_cannot_execute_same_operation():
    """并发 claim 同一 PENDING operation：至多一个 worker 持有（原子 UPDATE）。"""
    worker_a = OperationWorker(Factory, object())
    worker_b = OperationWorker(Factory, object())
    with Factory() as db:
        _seed(db)
        wid = _make_ws(db)
        op = worker_a.enqueue(wid, OperationType.PROVISION)
        op_id = op.id

    now = datetime.now(UTC)
    results: list[bool] = []
    lock = threading.Lock()

    def claim(w):
        with Factory() as db:
            db_op = db.get(WorkspaceOperation, op_id)
            ok = w._try_claim(db, db_op, now)
            with lock:
                results.append(ok)

    threads = [threading.Thread(target=claim, args=(w,)) for w in (worker_a, worker_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1  # 恰好一个 claim 成功
    assert results.count(False) == 1
    status, owner, _ = _op_status(op_id)
    assert status == OperationStatus.RUNNING.value
    assert owner in {worker_a.worker_id, worker_b.worker_id}


def test_worker_crash_allows_safe_reclaim():
    """worker 崩溃（claim 后无 finish）：lease 过期 → 其他 worker 安全 reclaim 并完成。"""
    with Factory() as db:
        _seed(db)
        wid = _make_ws(db)
        orchestrator = WorkspaceOrchestrator(
            Factory, SlowMockProvider(), Path("/tmp/test-fencing3")  # noqa: S108
        )
    worker_a = OperationWorker(Factory, orchestrator)
    worker_b = OperationWorker(Factory, orchestrator)
    op = worker_a.enqueue(wid, "provision")
    op_id = op.id

    # A claim 后"崩溃"（不 finish，lease 自然过期）
    with Factory() as db:
        db_op = db.get(WorkspaceOperation, op_id)
        assert worker_a._try_claim(db, db_op, datetime.now(UTC)) is True
        db_op = db.get(WorkspaceOperation, op_id)
        db_op.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        db.commit()

    # B reclaim 并完成 → SUCCEEDED，workspace RUNNING
    assert worker_b.tick_once() == 1
    status, owner, _ = _op_status(op_id)
    assert status == OperationStatus.SUCCEEDED.value
    assert owner == worker_b.worker_id
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == "running"


def test_concurrent_enqueue_db_constraint():
    """§13：并发 enqueue 同 workspace 的 PROVISION/STOP —— 数据库级唯一性保证
    至多一个 active operation（不依赖先查再插）。"""
    from app.services.worker import enqueue_operation

    with Factory() as db:
        _seed(db)
        wid = _make_ws(db)

    results: list[bool] = []
    lock = threading.Lock()

    def worker_enqueue(op_type):
        op = enqueue_operation(Factory, wid, op_type)
        with lock:
            results.append(op is not None)

    threads = [
        threading.Thread(target=worker_enqueue, args=(OperationType.PROVISION,)),
        threading.Thread(target=worker_enqueue, args=(OperationType.STOP,)),
        threading.Thread(target=worker_enqueue, args=(OperationType.DESTROY,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1  # 恰好一个成功
    assert results.count(False) == 2
    with Factory() as db:
        actives = db.scalars(
            select(WorkspaceOperation).where(
                WorkspaceOperation.workspace_id == wid,
                WorkspaceOperation.status.in_(
                    [OperationStatus.PENDING.value, OperationStatus.RUNNING.value]
                ),
            )
        ).all()
        assert len(actives) == 1


class OperationFencingDockerProvider(MockProvider):
    """记录 provision 收到的 reservation.metadata（验证 operation/fencing 注入）。"""

    def __init__(self):
        super().__init__("http://127.0.0.1:8000")
        self.last_meta: dict = {}

    def provision(self, workspace, template, workspace_dir, reservation=None):
        self.last_meta = dict(reservation.metadata) if reservation else {}
        return super().provision(workspace, template, workspace_dir, reservation)


def test_provision_receives_operation_fencing_context():
    """§11：worker 执行 provision 时 reservation.metadata 携带 operation_id/fencing_token。"""
    provider = OperationFencingDockerProvider()
    orchestrator = WorkspaceOrchestrator(Factory, provider, Path("/tmp/test-fenc1"))  # noqa: S108
    with Factory() as db:
        _seed(db)
        wid = _make_ws(db)
    worker = OperationWorker(Factory, orchestrator)
    op = worker.enqueue(wid, OperationType.PROVISION)
    assert op is not None
    worker.tick_once()
    with Factory() as db:
        op_db = db.get(WorkspaceOperation, op.id)
        assert op_db.status == OperationStatus.SUCCEEDED.value
        assert provider.last_meta.get("operation_id") == op.id
        assert provider.last_meta.get("fencing_token") == op_db.fencing_token


def test_lost_lease_aborts_old_executor():
    """§11：heartbeat 失败（lease 失效）→ 旧 worker 停止业务步骤（LeaseLostError 传播）。"""
    worker_a = OperationWorker(Factory, object())
    with Factory() as db:
        _seed(db)
        wid = _make_ws(db)
        op = worker_a.enqueue(wid, OperationType.PROVISION)
        op_id = op.id
    # A claim 后 lease 立即失效（模拟被 B reclaim）
    with Factory() as db:
        db_op = db.get(WorkspaceOperation, op_id)
        assert worker_a._try_claim(db, db_op, datetime.now(UTC)) is True
        db_op = db.get(WorkspaceOperation, op_id)
        db_op.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    worker_b = OperationWorker(Factory, object())
    with Factory() as db:
        db_op = db.get(WorkspaceOperation, op_id)
        assert worker_b._try_claim(db, db_op, datetime.now(UTC)) is True
        # A 尝试 finish（旧 token）→ LeaseLostError
        from app.services.worker import LeaseLostError as LLE

        stale = WorkspaceOperation(
            id=op_id, workspace_id="ws-x", operation_type="provision",
            status=OperationStatus.RUNNING.value, fencing_token=db_op.fencing_token,
        )
        stale.fencing_token = "old-token-a"
        with pytest.raises(LLE):
            worker_a.finish_success(db, stale)


class AdoptDockerProvider(DockerProvider):
    """容器已存在（adopt）的 fake docker provider。"""

    def __init__(self, settings):
        super().__init__(settings)
        self.runs = 0
        self.container_present = False
        self.containers = set()

    def health(self):
        return True, "fake"

    def wait_ready(self, workspace, template, timeout_seconds=120):
        return True

    def _container_exists(self, workspace):
        name = workspace.container_name or f"ec-{workspace.id[:12]}"
        return name in self.containers

    def _streaming_workspace_running(self):
        return False

    def _run(self, args, *, check=True):
        if args[1] == "run":
            self.runs += 1
            idx = args.index("--name")
            self.containers.add(args[idx + 1])
            return type("R", (), {"returncode": 0, "stdout": "c\n", "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def test_same_operation_provision_is_idempotent(monkeypatch, tmp_path):
    """§11：同一 workspace 重试 provision → adopt 已有容器，不创建第二份。"""
    from app.config import Settings

    settings = Settings(eula_accepted=True, workspace_root=tmp_path)
    monkeypatch.setattr("app.services.providers.docker.allocate_tcp_port", lambda *a, **kw: 38101)
    provider = AdoptDockerProvider(settings)
    orchestrator = WorkspaceOrchestrator(Factory, provider, tmp_path)
    with Factory() as db:
        _seed(db)
        wid = _make_ws(db)
    # 第一次 provision（容器不存在 → run）
    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator._start(wid)
    assert provider.runs == 1
    # 第二次（容器已存在 → adopt，不 run）
    with Factory() as db:
        ws = db.get(Workspace, wid)
        ws.status = "queued"
        db.commit()
        orchestrator._start(wid)
    assert provider.runs == 1  # 未创建第二份

"""WarmPoolManager 测试。

使用文件版 SQLite (每会话独立连接): 预热/benchmark 在后台线程中写库,
:memory: + StaticPool 的共享单连接会让测试会话与后台线程发生事务竞争。
"""

import time
from pathlib import Path
from types import SimpleNamespace

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
    WarmPoolState,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.warmpool import WarmPoolManager
from app.services.worker import OperationWorker

_TEST_DB = Path("test-warmpool.db")


@pytest.fixture()
def db_factory():
    engine = create_engine(f"sqlite:///{_TEST_DB}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()
    _TEST_DB.unlink(missing_ok=True)


def _make_template(db, template_id: str = "cartpole", enabled: bool = True) -> Template:
    template = Template(
        id=template_id,
        slug=template_id,
        name=template_id,
        version="0.1.0",
        description="test",
        category="test",
        runtime="mock",
        enabled=enabled,
    )
    db.add(template)
    db.commit()
    return template


def _seed_gpu(db) -> None:
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
    db.commit()


def _make_manager(db_factory, *, warm_pool_enabled: bool = True, warm_pool_size: int = 1) -> WarmPoolManager:
    provider = MockProvider("http://127.0.0.1:8000")
    orchestrator = WorkspaceOrchestrator(
        db_factory,
        provider,
        Path("/tmp/test-embodiedcloud-warmpool"),  # noqa: S108 测试隔离目录
    )
    settings = SimpleNamespace(warm_pool_enabled=warm_pool_enabled, warm_pool_size=warm_pool_size)
    return WarmPoolManager(db_factory, orchestrator, settings)


def _drain_operations(db_factory, orchestrator, max_ticks: int = 20) -> bool:
    """驱动 worker 执行完所有 PENDING operation（maintain 异步入队后由 worker 异步执行）。"""
    worker = OperationWorker(db_factory, orchestrator)
    return any(worker.tick_once() == 0 for _ in range(max_ticks))


def _wait_terminal(db_factory, workspace_id: str, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db_factory() as db:
            workspace = db.get(Workspace, workspace_id)
            if workspace is not None and workspace.status in {
                WorkspaceStatus.RUNNING.value,
                WorkspaceStatus.FAILED.value,
            }:
                return workspace.status
        time.sleep(0.05)
    return "timeout"


def test_maintain_disabled_creates_nothing(db_factory):
    with db_factory() as db:
        _make_template(db, "cartpole")
        manager = _make_manager(db_factory, warm_pool_enabled=False)
        manager.maintain(db)
        assert list(db.scalars(select(Workspace))) == []


def test_maintain_enabled_builds_warm_workspace(db_factory):
    with db_factory() as db:
        _make_template(db, "cartpole")
        _seed_gpu(db)
        manager = _make_manager(db_factory, warm_pool_enabled=True, warm_pool_size=1)
        manager.maintain(db)
        workspaces = list(db.scalars(select(Workspace)))
        assert len(workspaces) == 1
        assert workspaces[0].template_id == "cartpole"
        # 异步预热：maintain 只 create + 入队 PROVISION，不再同步 _start
        assert workspaces[0].warm_pool_state == WarmPoolState.PREWARMING.value
        assert _drain_operations(db_factory, manager.orchestrator)
        # worker 异步执行后到达 RUNNING；下一次 maintain 收割 → READY
        assert _wait_terminal(db_factory, workspaces[0].id) == WorkspaceStatus.RUNNING.value
        with db_factory() as reap_db:
            manager.maintain(reap_db)
            ready = reap_db.get(Workspace, workspaces[0].id)
            assert ready.warm_pool_state == WarmPoolState.READY.value


def test_maintain_enqueues_provision_and_skips_sync_start(db_factory, monkeypatch):
    """maintain 后 PROVISION operation 已入队，且 _start 未被同步调用。"""
    with db_factory() as db:
        _make_template(db, "cartpole")
        _seed_gpu(db)
        manager = _make_manager(db_factory, warm_pool_enabled=True, warm_pool_size=1)
        sync_calls: list[str] = []
        monkeypatch.setattr(manager.orchestrator, "_start", lambda wid: sync_calls.append(wid))
        manager.maintain(db)
        assert sync_calls == []  # 未同步 _start
        ops = list(db.scalars(select(WorkspaceOperation)))
        assert len(ops) == 1
        assert ops[0].operation_type == OperationType.PROVISION.value
        assert ops[0].status == OperationStatus.PENDING.value


def test_maintain_reaps_prewarming_running_to_ready(db_factory):
    """「PREWARMING + RUNNING」→ 收割后 READY；池已满不再补位。"""
    with db_factory() as db:
        _make_template(db, "cartpole")
        _seed_gpu(db)
        db.add(
            Workspace(
                id="w-ready", name="warm-ready", template_id="cartpole", provider="mock",
                status=WorkspaceStatus.RUNNING.value,
                warm_pool_state=WarmPoolState.PREWARMING.value,
            )
        )
        db.commit()
        manager = _make_manager(db_factory, warm_pool_enabled=True, warm_pool_size=1)
        manager.maintain(db)
        ws = db.get(Workspace, "w-ready")
        assert ws.warm_pool_state == WarmPoolState.READY.value
        # READY 已补满 → 不创建新 workspace
        assert len(list(db.scalars(select(Workspace)))) == 1


def test_maintain_reaps_prewarming_failed_to_failed(db_factory):
    """「PREWARMING + FAILED」→ 收割后 FAILED；冷却退避跳过本轮补位，下一轮恢复。"""
    with db_factory() as db:
        _make_template(db, "cartpole")
        _seed_gpu(db)
        db.add(
            Workspace(
                id="w-failed", name="warm-failed", template_id="cartpole", provider="mock",
                status=WorkspaceStatus.FAILED.value,
                warm_pool_state=WarmPoolState.PREWARMING.value,
            )
        )
        db.commit()
        manager = _make_manager(db_factory, warm_pool_enabled=True, warm_pool_size=1)
        # 第一轮：收割 → FAILED；冷却退避 → 本轮不补位
        manager.maintain(db)
        assert db.get(Workspace, "w-failed").warm_pool_state == WarmPoolState.FAILED.value
        assert list(
            db.scalars(select(Workspace).where(Workspace.warm_pool_state == WarmPoolState.PREWARMING.value))
        ) == []
        # 第二轮：冷却过期 → 补位 1 个新 PREWARMING + 入队 PROVISION
        db.rollback()
        manager.maintain(db)
        created = list(
            db.scalars(select(Workspace).where(Workspace.warm_pool_state == WarmPoolState.PREWARMING.value))
        )
        assert len(created) == 1
        assert created[0].id != "w-failed"
        ops = list(
            db.scalars(select(WorkspaceOperation).where(WorkspaceOperation.workspace_id == created[0].id))
        )
        assert len(ops) == 1
        assert ops[0].operation_type == OperationType.PROVISION.value


def test_maintain_enqueues_destroy_for_failed_warm_workspace(db_factory):
    """reap 为 FAILED 的 warm workspace 被入队 DESTROY operation 异步清理。"""
    with db_factory() as db:
        _make_template(db, "cartpole")
        _seed_gpu(db)
        db.add(
            Workspace(
                id="w-failed", name="warm-failed", template_id="cartpole", provider="mock",
                status=WorkspaceStatus.FAILED.value,
                warm_pool_state=WarmPoolState.PREWARMING.value,
            )
        )
        db.commit()
        manager = _make_manager(db_factory, warm_pool_enabled=True, warm_pool_size=1)
        manager.maintain(db)
        ops = list(
            db.scalars(select(WorkspaceOperation).where(WorkspaceOperation.workspace_id == "w-failed"))
        )
        assert len(ops) == 1
        assert ops[0].operation_type == OperationType.DESTROY.value
        assert ops[0].status == OperationStatus.PENDING.value


def test_maintain_cooldown_prevents_recreate_after_failure(db_factory):
    """容量不足：reap FAILED 后本轮补位跳过（冷却），下一轮恢复，不无限 churn。"""
    with db_factory() as db:
        _make_template(db, "cartpole")
        _seed_gpu(db)
        manager = _make_manager(db_factory, warm_pool_enabled=True, warm_pool_size=1)
        # 第一轮：创建 1 个 PREWARMING + 入队 PROVISION
        manager.maintain(db)
        created = db.scalars(select(Workspace)).one()
        assert created.warm_pool_state == WarmPoolState.PREWARMING.value
        # 模拟容量不足：PROVISION 失败 → workspace 置 FAILED
        created.status = WorkspaceStatus.FAILED.value
        db.commit()
        # 第二轮：收割 → FAILED + 冷却 → 不重建（missing 不含刚 reap 的池位）
        db.rollback()
        manager.maintain(db)
        assert list(
            db.scalars(select(Workspace).where(Workspace.warm_pool_state == WarmPoolState.PREWARMING.value))
        ) == []
        assert len(list(db.scalars(select(Workspace)))) == 1  # 不重复创建
        # 第三轮：冷却过期 → 恢复补位
        db.rollback()
        manager.maintain(db)
        assert len(
            list(db.scalars(select(Workspace).where(Workspace.warm_pool_state == WarmPoolState.PREWARMING.value)))
        ) == 1


def test_maintain_does_not_create_beyond_existing_ready(db_factory):
    with db_factory() as db:
        _make_template(db, "cartpole")
        _seed_gpu(db)
        existing = Workspace(
            id="w-existing",
            name="w",
            template_id="cartpole",
            provider="mock",
            status=WorkspaceStatus.CREATED.value,
        )
        db.add(existing)
        db.commit()
        manager = _make_manager(db_factory, warm_pool_enabled=True, warm_pool_size=1)
        manager.maintain(db)
        workspaces = list(db.scalars(select(Workspace)))
        assert len(workspaces) == 1
        assert workspaces[0].id == "w-existing"


def test_maintain_ignores_disabled_templates(db_factory):
    with db_factory() as db:
        _make_template(db, "disabled-tpl", enabled=False)
        manager = _make_manager(db_factory, warm_pool_enabled=True, warm_pool_size=1)
        manager.maintain(db)
        assert list(db.scalars(select(Workspace))) == []


def test_metrics_shape(db_factory):
    with db_factory() as db:
        _make_template(db, "cartpole")
        _seed_gpu(db)
        manager = _make_manager(db_factory)
        metrics = manager.metrics(db)
        assert "cartpole" in metrics
        assert set(metrics["cartpole"]) == {"warm", "ready"}
        assert metrics["cartpole"]["warm"] == 0
        assert metrics["cartpole"]["ready"] == 0


def test_metrics_counts_warm_after_launch(db_factory):
    with db_factory() as db:
        _make_template(db, "cartpole")
        _seed_gpu(db)
        manager = _make_manager(db_factory, warm_pool_enabled=True, warm_pool_size=1)
        manager.maintain(db)
        workspace = db.scalars(select(Workspace)).one()
        # 异步 provision → RUNNING，再收割 → READY（warm 指标统计 READY）
        assert _drain_operations(db_factory, manager.orchestrator)
        assert _wait_terminal(db_factory, workspace.id) == WorkspaceStatus.RUNNING.value
        # 结束 db 事务，避免读到 drain 之前的旧快照
        db.rollback()
        manager.maintain(db)
        assert manager.metrics(db)["cartpole"]["warm"] == 1


def test_benchmark_launch_returns_p50_p95(db_factory):
    with db_factory() as db:
        _make_template(db, "cartpole")
        _seed_gpu(db)
        manager = _make_manager(db_factory)
        result = manager.benchmark_launch(db, "cartpole", iterations=2)
        assert set(result) == {"p50_s", "p95_s", "samples"}
        assert len(result["samples"]) == 2
        assert all(s > 0 for s in result["samples"])
        assert result["p50_s"] > 0
        assert result["p95_s"] >= result["p50_s"]
        # 真实完成了两次启动（终态为 RUNNING/FAILED 任一，耗时均被记录）
        with db_factory() as check_db:
            statuses = check_db.scalars(select(Workspace.status)).all()
        assert len(statuses) == 2
        assert all(s in {WorkspaceStatus.RUNNING.value, WorkspaceStatus.FAILED.value} for s in statuses)


def test_benchmark_launch_missing_template_raises(db_factory):
    with db_factory() as db:
        manager = _make_manager(db_factory)
        with pytest.raises(ValueError):
            manager.benchmark_launch(db, "no-such-template", iterations=1)


def test_count_legacy_pool_excludes_user_workspaces(db_factory):
    """legacy 计数只纳无归属（user_id IS NULL）的 workspace，普通用户 QUEUED 不计入。"""
    with db_factory() as db:
        _make_template(db, "cartpole")
        # 无归属（warm pool legacy）→ 计入
        db.add(
            Workspace(
                id="w-legacy", name="w", template_id="cartpole", provider="mock",
                status=WorkspaceStatus.QUEUED.value, user_id=None,
            )
        )
        # 普通用户 workspace（有 user_id）→ 不计入
        db.add(
            Workspace(
                id="w-user", name="w2", template_id="cartpole", provider="mock",
                status=WorkspaceStatus.QUEUED.value, user_id="u1",
            )
        )
        db.commit()
        manager = _make_manager(db_factory)
        assert manager._count_legacy_pool(db, "cartpole") == 1

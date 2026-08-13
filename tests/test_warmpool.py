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
from app.models import Gpu, GpuHost, Template, Workspace, WorkspaceStatus
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.warmpool import WarmPoolManager

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
        # mock provider 预热应快速到达 RUNNING
        assert _wait_terminal(db_factory, workspaces[0].id) == WorkspaceStatus.RUNNING.value


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
        assert _wait_terminal(db_factory, workspace.id) == WorkspaceStatus.RUNNING.value
        metrics = manager.metrics(db)
        assert metrics["cartpole"]["warm"] == 1


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

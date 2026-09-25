"""Runtime readiness contract（§9，P0）。

RUNNING 不能仅表示 docker run / Deployment 创建成功：
- workspace 在 provider.wait_ready() 成功前不得为 RUNNING
- readiness 超时 → 完整回滚（runtime destroy + GPU release）→ FAILED
- TemplateVersion.healthcheck 必须真实执行（不是 DB metadata）
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
    Template,
    Workspace,
    WorkspaceStatus,
)
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("readiness"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


class ReadinessControlledProvider(MockProvider):
    """可控 wait_ready 的 mock provider。"""

    def __init__(self, ready: bool = True, delay: float = 0.0):
        super().__init__("http://127.0.0.1:8000")
        self.ready = ready
        self.delay = delay
        self.destroy_calls = 0
        self.wait_calls = 0

    def wait_ready(self, workspace, template, timeout_seconds: int = 120) -> bool:
        import time

        self.wait_calls += 1
        if self.delay:
            time.sleep(self.delay)
        return self.ready

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
        recommended_vram_gb=16,
        estimated_hourly_cost_cny=1.0,
        healthcheck={"command": "echo ok", "interval_s": 60},
    )
    db.add(t)
    db.commit()


def test_workspace_not_running_before_provider_ready():
    """§9：wait_ready=False → workspace 不得 RUNNING（保持失败/回滚）。"""
    provider = ReadinessControlledProvider(ready=False)
    orchestrator = WorkspaceOrchestrator(Factory, provider, Path("/tmp/test-ready1"))  # noqa: S108
    with Factory() as db:
        _seed(db)
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = ws.id
    orchestrator._start(wid)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status != WorkspaceStatus.RUNNING.value
        assert ws.status == WorkspaceStatus.FAILED.value
        assert "readiness" in ws.error_message


def test_readiness_timeout_rolls_back():
    """§9：readiness 超时 → 完整回滚（runtime destroy + GPU release）→ FAILED。"""
    provider = ReadinessControlledProvider(ready=False)
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-ready2"), ready_timeout_seconds=1  # noqa: S108
    )
    with Factory() as db:
        _seed(db)
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = ws.id
    orchestrator._start(wid)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.FAILED.value
        # 补偿：provider.destroy 被调用 + GPU 释放（无孤儿）
        assert provider.destroy_calls >= 1
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None
        gpu = db.scalar(select(Gpu))
        assert gpu.status == GpuStatus.AVAILABLE.value
        assert ws.gpu_id is None


def test_healthcheck_failure_releases_gpu():
    """§9：readiness（含 healthcheck 真实执行）失败 → GPU 必须释放。"""
    provider = ReadinessControlledProvider(ready=False)
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-ready3"), ready_timeout_seconds=1  # noqa: S108
    )
    with Factory() as db:
        _seed(db)
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = ws.id
        orchestrator._start(wid)
        # 分配过的 GPU 已释放
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value


def test_provision_waits_for_ready_before_running():
    """§9：wait_ready 在 provision 后执行且成功后才 RUNNING（gate 语义）。"""
    provider = ReadinessControlledProvider(ready=True)
    orchestrator = WorkspaceOrchestrator(Factory, provider, Path("/tmp/test-ready4"))  # noqa: S108
    with Factory() as db:
        _seed(db)
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = ws.id
    orchestrator._start(wid)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value
    assert provider.wait_calls >= 1

"""v0.3.0 GPU Single Authority：证明 DB GpuAllocation 与实际 Provider 运行设备完全一致。

核心断言：
- GpuScheduler.allocate() 是唯一 GPU 决策入口
- DockerProvider 收到的 reservation 与 DB Gpu 行一致（--gpus device=N == DB gpu_index）
- 不存在任何路径让 provider 自行选择 GPU
- stop/destroy 后 GPU 与分配绑定必然释放
"""

import threading
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import Base, Gpu, GpuAllocation, GpuStatus, Template, Workspace, WorkspaceStatus
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.docker import DockerProvider
from app.services.scheduler import GpuInfo, GpuScheduler

ENGINE = create_engine("sqlite:///./test-gpu-authority.db", connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


class CapturingDockerProvider(DockerProvider):
    """记录 docker run 参数；模拟 docker daemon 返回成功。"""

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.runs: list[list[str]] = []

    def health(self):
        return True, "fake docker + gpu"

    def _streaming_workspace_running(self):
        return False

    def _run(self, args, *, check=True):
        if args[1] == "run":
            self.runs.append(args)
            return CompletedProcess(args=args, returncode=0, stdout="container-id\n", stderr="")
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")


def _setup(monkeypatch, tmp_path, n_gpus: int = 2) -> tuple[CapturingDockerProvider, WorkspaceOrchestrator, Settings]:
    settings = Settings(
        eula_accepted=True,
        workspace_root=tmp_path,
        ide_port_start=38100,
        ide_port_end=38120,
    )
    monkeypatch.setattr("app.services.providers.docker.is_port_free", lambda *a, **kw: True)
    monkeypatch.setattr("app.services.providers.docker.allocate_tcp_port", lambda *a, **kw: 38101)
    provider = CapturingDockerProvider(settings)
    scheduler = GpuScheduler(Factory)
    with Factory() as db:
        scheduler.sync_host(
            db,
            host_id="docker-host-0001",
            name="docker-host",
            address="127.0.0.1",
            provider="docker",
            gpus=[
                GpuInfo(gpu_uuid=f"GPU-{i}-uuid", model=f"RTX {i}", memory_total=24564, index=i)
                for i in range(n_gpus)
            ],
        )
    orchestrator = WorkspaceOrchestrator(Factory, provider, Path(tmp_path), scheduler)
    return provider, orchestrator, settings


def _add_template(db, template_id: str = "cartpole") -> Template:
    template = Template(
        id=template_id,
        slug=template_id,
        name=template_id,
        description="test",
        category="test",
        runtime="isaaclab",
        image=f"registry/{template_id}:0.1.0",
        launch_command="echo ok",
        enabled=True,
        recommended_vram_gb=16,
        estimated_hourly_cost_cny=1.0,
    )
    db.add(template)
    db.commit()
    return template


def _db_gpu_binding(db, workspace_id: str) -> tuple[Gpu, GpuAllocation]:
    alloc = db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == workspace_id))
    assert alloc is not None, "workspace 必须有 GpuAllocation 绑定"
    gpu = db.get(Gpu, alloc.gpu_id)
    assert gpu is not None
    return gpu, alloc


def test_scheduler_provider_gpu_consistency(monkeypatch, tmp_path):
    """核心：scheduler 分配 → orchestrator 传 reservation → docker --gpus device=N
    与 DB gpu_id / GpuAllocation / gpu_index 完全一致。"""
    provider, orchestrator, _ = _setup(monkeypatch, tmp_path)

    with Factory() as db:
        _add_template(db)
        workspace = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")

    orchestrator._start(workspace.id)  # 同步执行 provisioning

    with Factory() as db:
        ws = db.get(Workspace, workspace.id)
        assert ws.status == WorkspaceStatus.RUNNING.value, ws.error_message
        gpu, alloc = _db_gpu_binding(db, ws.id)
        # DB 事实：GPU ALLOCATED 且绑定 workspace
        assert gpu.status == GpuStatus.ALLOCATED.value
        assert gpu.workspace_id == ws.id
        assert alloc.host_id == gpu.host_id
        assert ws.gpu_id == gpu.id
        assert ws.gpu_index == gpu.gpu_index
        assert ws.gpu_name == gpu.model

        # 实际 docker 命令与 DB 完全一致（关键断言）
        assert len(provider.runs) == 1
        joined = " ".join(provider.runs[0])
        assert f"--gpus device={gpu.gpu_index}" in joined
        assert f"embodiedcloud.gpu={gpu.gpu_index}" in joined
        assert f"embodiedcloud.gpu_id={gpu.id}" in joined
        # 镜像来自 template
        assert "registry/cartpole:0.1.0" in joined
        assert "embodiedcloud/isaaclab-workspace" not in joined


def test_no_double_gpu_assignment(monkeypatch, tmp_path):
    """两个 workspace 依序启动：不得有 GPU 同时绑定两个 workspace。"""
    provider, orchestrator, _ = _setup(monkeypatch, tmp_path)

    with Factory() as db:
        _add_template(db)
        t = db.get(Template, "cartpole")
        w1 = orchestrator.create(db, t, user_id="u1")
        w2 = orchestrator.create(db, t, user_id="u1")

    orchestrator._start(w1.id)
    orchestrator._start(w2.id)

    with Factory() as db:
        ws1, ws2 = db.get(Workspace, w1.id), db.get(Workspace, w2.id)
        assert ws1.status == WorkspaceStatus.RUNNING.value, ws1.error_message
        assert ws2.status == WorkspaceStatus.RUNNING.value, ws2.error_message
        gpu1, alloc1 = _db_gpu_binding(db, ws1.id)
        gpu2, alloc2 = _db_gpu_binding(db, ws2.id)
        # 不同 GPU
        assert gpu1.id != gpu2.id
        # 无 GPU 被两个 workspace 同时绑定
        for gpu in db.scalars(select(Gpu)):
            if gpu.id in {gpu1.id, gpu2.id}:
                assert gpu.workspace_id in {ws1.id, ws2.id}
        assert alloc1.gpu_id != alloc2.gpu_id
        # docker 层也绑定了不同设备
        devices = []
        for run in provider.runs:
            joined = " ".join(run)
            for part in joined.split():
                if part.startswith("device="):
                    devices.append(part)
        assert len(devices) == 2
        assert len(set(devices)) == 2


def test_concurrent_workspace_gpu_allocation(monkeypatch, tmp_path):
    """并发启动 8 个 workspace（2 张 GPU）：成功者 GPU 绑定唯一，且 provider
    收到的 reservation 与 DB 一致。"""
    provider, orchestrator, _ = _setup(monkeypatch, tmp_path)

    with Factory() as db:
        _add_template(db)
        t = db.get(Template, "cartpole")
        ids = [orchestrator.create(db, t, user_id=f"u{i}").id for i in range(8)]

    threads = [threading.Thread(target=orchestrator._start, args=(wid,)) for wid in ids]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    with Factory() as db:
        running = [
            db.get(Workspace, wid)
            for wid in ids
            if db.get(Workspace, wid).status == WorkspaceStatus.RUNNING.value
        ]
        failed = [
            db.get(Workspace, wid)
            for wid in ids
            if db.get(Workspace, wid).status == WorkspaceStatus.FAILED.value
        ]
        assert len(running) == 2  # 恰好 2 张 GPU 可用
        assert len(failed) == 6  # 其余无 GPU 可分配，明确失败
        gpu_ids = [db.get(Gpu, a.gpu_id).id for a in db.scalars(select(GpuAllocation))]
        assert len(gpu_ids) == len(set(gpu_ids))  # 无重复 GPU 分配
        # provider 收到的 device 与 DB 一致
        devices = set()
        for run in provider.runs:
            for part in " ".join(run).split():
                if part.startswith("device="):
                    devices.add(part)
        db_gpu_indexes = {f"device={gpu.gpu_index}" for gpu in db.scalars(select(Gpu)) if gpu.workspace_id}
        assert devices == db_gpu_indexes


def test_gpu_release_after_stop(monkeypatch, tmp_path):
    _, orchestrator, _ = _setup(monkeypatch, tmp_path)

    with Factory() as db:
        _add_template(db)
        workspace = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
    orchestrator._start(workspace.id)

    with Factory() as db:
        ws = db.get(Workspace, workspace.id)
        gpu, _ = _db_gpu_binding(db, ws.id)
        orchestrator.stop(db, ws)

    with Factory() as db:
        gpu = db.get(Gpu, gpu.id)
        ws = db.get(Workspace, workspace.id)
        assert ws.status == WorkspaceStatus.STOPPED.value
        # stop 后 GPU 必须释放
        assert gpu.status == GpuStatus.AVAILABLE.value
        assert gpu.workspace_id is None
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == ws.id)) is None


def test_gpu_release_after_destroy(monkeypatch, tmp_path):
    _, orchestrator, _ = _setup(monkeypatch, tmp_path)

    with Factory() as db:
        _add_template(db)
        workspace = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
    orchestrator._start(workspace.id)

    with Factory() as db:
        ws = db.get(Workspace, workspace.id)
        gpu, _ = _db_gpu_binding(db, ws.id)
        orchestrator.destroy(db, ws)

    with Factory() as db:
        # destroy 后 GPU 必须释放（workspace 行删除）
        gpu = db.get(Gpu, gpu.id)
        assert gpu.status == GpuStatus.AVAILABLE.value
        assert gpu.workspace_id is None
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == workspace.id)) is None

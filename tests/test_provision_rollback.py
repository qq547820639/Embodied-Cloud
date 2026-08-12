"""Provision 补偿回滚（B12）：任意步骤失败必须清理全部已创建资源。

每个测试最终必须保证：
- no orphan GPU allocation（GPU 回 AVAILABLE、无未释放绑定）
- no orphan container/pod（provider.destroy 被调用）
- no orphan volume（K8s PVC 被删除）
- no orphan port（端口字段清空）
- workspace state == FAILED
- error persisted
"""

from pathlib import Path
from subprocess import CompletedProcess

import pytest
from k8s_fakes import make_fake_models
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import Base, Gpu, GpuAllocation, GpuStatus, Template, Workspace, WorkspaceStatus
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.docker import DockerProvider
from app.services.providers.k8s import KubernetesProvider
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler

ENGINE = create_engine("sqlite:///./test-rollback.db", connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


def _seed_gpu(db) -> None:
    scheduler = GpuScheduler(Factory)
    scheduler.sync_host(
        db,
        host_id="host-1",
        name="h1",
        address="127.0.0.1",
        provider="mock",
        gpus=[GpuInfo(gpu_uuid="gpu-1", model="RTX", memory_total=24564, index=0)],
    )


def _make_template(db) -> Template:
    t = Template(
        id="cartpole",
        slug="cartpole",
        name="cartpole",
        description="test",
        category="test",
        runtime="isaaclab",
        image="registry/cartpole:0.1.0",
        launch_command="echo ok",
        enabled=True,
        recommended_vram_gb=16,
        estimated_hourly_cost_cny=1.0,
    )
    db.add(t)
    db.commit()
    return t


def _assert_clean_rollback(workspace_id: str, provider_name: str, provider, monkeypatch) -> None:
    """回滚后的统一断言：无孤儿资源 + FAILED + error 持久化。"""
    with Factory() as db:
        ws = db.get(Workspace, workspace_id)
        assert ws.status == WorkspaceStatus.FAILED.value
        assert ws.error_message  # error persisted
        # no orphan GPU allocation
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == workspace_id)) is None
        gpu = db.scalar(select(Gpu))
        assert gpu.status == GpuStatus.AVAILABLE.value
        assert gpu.workspace_id is None
        # no orphan port
        assert ws.ide_port is None
        assert ws.signal_port is None
        assert ws.media_port is None
        # provider 侧清理被调用
        assert provider.destroy_calls >= 1


# ---------------------------------------------------------------------------
# Docker provider: fail at each stage
# ---------------------------------------------------------------------------


class FailingDockerProvider(DockerProvider):
    def __init__(self, settings, fail_at: str):
        super().__init__(settings)
        self.fail_at = fail_at
        self.destroy_calls = 0
        self.provision_calls = 0

    def health(self):
        return True, "fake"

    def _streaming_workspace_running(self):
        return False

    def _run(self, args, *, check=True):
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    def provision(self, workspace, template, workspace_dir, reservation=None):
        self.provision_calls += 1
        if self.fail_at == "after_gpu_allocate":
            raise RuntimeError("simulated: fail after GPU allocate")
        # 模拟 runtime create 之后、endpoint 完成之前失败
        if self.fail_at == "after_runtime_create":
            workspace.container_name = f"ec-{workspace.id[:12]}"
            raise RuntimeError("simulated: fail after runtime create")
        if self.fail_at == "after_endpoint_create":
            workspace.container_name = f"ec-{workspace.id[:12]}"
            raise RuntimeError("simulated: fail after endpoint create")
        if self.fail_at == "during_healthcheck":
            workspace.container_name = f"ec-{workspace.id[:12]}"
            raise RuntimeError("simulated: fail during healthcheck")
        raise AssertionError(f"unexpected fail_at {self.fail_at}")

    def destroy(self, workspace):
        self.destroy_calls += 1
        return None


def _docker_orchestrator(monkeypatch, tmp_path, fail_at):
    settings = Settings(
        eula_accepted=True,
        workspace_root=tmp_path,
        ide_port_start=38100,
        ide_port_end=38120,
    )
    monkeypatch.setattr("app.services.providers.docker.is_port_free", lambda *a, **kw: True)
    monkeypatch.setattr("app.services.providers.docker.allocate_tcp_port", lambda *a, **kw: 38101)
    provider = FailingDockerProvider(settings, fail_at)
    with Factory() as db:
        _seed_gpu(db)
        _make_template(db)
        orchestrator = WorkspaceOrchestrator(Factory, provider, Path(tmp_path))
        workspace = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = workspace.id
    return orchestrator, provider, wid


@pytest.mark.parametrize(
    "fail_at",
    ["after_gpu_allocate", "after_runtime_create", "after_endpoint_create", "during_healthcheck"],
)
def test_docker_provision_failure_rolls_back_everything(monkeypatch, tmp_path, fail_at):
    orchestrator, provider, wid = _docker_orchestrator(monkeypatch, tmp_path, fail_at)
    orchestrator._start(wid)
    _assert_clean_rollback(wid, "docker", provider, monkeypatch)


# ---------------------------------------------------------------------------
# K8s provider: fail after volume create (PVC) → 补偿删除 PVC
# ---------------------------------------------------------------------------


class FailingK8sClient:
    """PVC 创建成功，Service 创建失败：验证 PVC 补偿删除。"""

    def __init__(self):
        self.deleted_pvc: list[str] = []

    def CoreV1Api(self):
        return self

    def AppsV1Api(self):
        return self

    def create_namespaced_persistent_volume_claim(self, namespace, body, **kwargs):
        return body

    def create_namespaced_service(self, namespace, body, **kwargs):
        raise RuntimeError("simulated: service create failure")

    def delete_namespaced_service(self, name, namespace, **kwargs):
        self.deleted_pvc.append(f"svc:{name}")

    def delete_namespaced_persistent_volume_claim(self, name, namespace, **kwargs):
        self.deleted_pvc.append(name)


class RecordingK8sProvider(KubernetesProvider):
    def __init__(self, settings, fake):
        # offline 测试注入 fake model layer（零 Kubernetes SDK 依赖）
        super().__init__(settings, _client=fake, model_factory=make_fake_models)
        self.destroy_calls = 0

    def destroy(self, workspace):
        self.destroy_calls += 1
        return super().destroy(workspace)


def test_k8s_provision_failure_removes_orphan_pvc(monkeypatch, tmp_path):
    """fail after volume：PVC 已创建、Service 失败 → PVC 必须被补偿删除。"""
    fake = FailingK8sClient()
    settings = Settings(eula_accepted=True, k8s_namespace="embodiedcloud", host_public_ip="10.0.0.8")
    provider = RecordingK8sProvider(settings, fake)
    with Factory() as db:
        _seed_gpu(db)
        _make_template(db)
        orchestrator = WorkspaceOrchestrator(Factory, provider, Path(tmp_path))
        workspace = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = workspace.id

    orchestrator._start(wid)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.FAILED.value
        assert ws.error_message
        # GPU 已释放
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None
        gpu = db.scalar(select(Gpu))
        assert gpu.status == GpuStatus.AVAILABLE.value
    # provider 补偿 destroy 被调用，且 fake 内部已删 PVC（无孤儿 volume）
    assert provider.destroy_calls >= 1
    assert fake.deleted_pvc, "PVC 必须被补偿删除"


# ---------------------------------------------------------------------------
# Mock provider: no-op 路径不破坏回滚语义
# ---------------------------------------------------------------------------


class FailingMockProvider(MockProvider):
    def __init__(self, base_url):
        super().__init__(base_url)
        self.destroy_calls = 0

    def provision(self, workspace, template, workspace_dir, reservation=None):
        raise RuntimeError("simulated: mock provision failure")

    def destroy(self, workspace):
        self.destroy_calls += 1


def test_mock_provision_failure_still_releases_gpu(monkeypatch, tmp_path):
    provider = FailingMockProvider("http://127.0.0.1:8000")
    with Factory() as db:
        _seed_gpu(db)
        _make_template(db)
        orchestrator = WorkspaceOrchestrator(Factory, provider, Path(tmp_path))
        workspace = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = workspace.id
    orchestrator._start(wid)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.FAILED.value
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value
    # 补偿 destroy 被统一调用（mock 为幂等 no-op），不得抛错
    assert provider.destroy_calls == 1

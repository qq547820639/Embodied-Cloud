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
from app.services.providers.base import ProvisionResult
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


def _assert_clean_rollback(workspace_id: str, provider, monkeypatch) -> None:
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
        # 进程内 IDE 端口分配登记必须已释放（destroy 的 finally 语义）
        assert provider._allocated_ide_ports.get(workspace_id) is None


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
            # GPU 已分配、runtime 尚未创建 → 补偿 destroy 只能按 id 推导容器名
            raise RuntimeError("simulated: fail after GPU allocate")
        # 后续两个阶段：runtime（容器）已创建，模拟真实的中间产物
        workspace.container_name = f"ec-{workspace.id[:12]}"
        if self.fail_at == "after_runtime_create":
            # 容器已创建、端点未配置
            raise RuntimeError("simulated: fail after runtime create")
        if self.fail_at == "after_endpoint_create":
            # 容器 + IDE 端点均已创建：端口已在进程内分配池登记（真实行为），
            # 补偿 destroy 必须释放登记（断言见 _assert_clean_rollback）
            self._allocated_ide_ports[workspace.id] = 38101
            raise RuntimeError("simulated: fail after endpoint create")
        if self.fail_at == "during_healthcheck":
            # provision 成功返回，readiness gate（wait_ready）阶段才失败。
            # 真实行为：端口已登记进进程内分配池，补偿 destroy 必须释放。
            self._allocated_ide_ports[workspace.id] = 38101
            return ProvisionResult(
                container_name=workspace.container_name,
                ide_port=38101,
                ide_url="http://127.0.0.1:38101/",
                password="x",  # noqa: S106 失败注入测试数据
            )
        raise AssertionError(f"unexpected fail_at {self.fail_at}")

    def wait_ready(self, workspace, template, timeout_seconds: int = 120) -> bool:
        # during_healthcheck：readiness gate 失败（真实语义：runtime 起了但未就绪）
        if self.fail_at == "during_healthcheck":
            raise RuntimeError("simulated: fail during healthcheck")
        return True

    def destroy(self, workspace):
        self.destroy_calls += 1
        # 走真实 destroy（含 finally 释放端口登记），否则补偿清理的
        # 端口释放语义（本次断言）不会被覆盖到
        return super().destroy(workspace)


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
    _assert_clean_rollback(wid, provider, monkeypatch)


# ---------------------------------------------------------------------------
# K8s provider: fail after volume create (PVC) → 补偿删除 PVC
# ---------------------------------------------------------------------------


class FailingK8sClient:
    """PVC 创建成功，Service 创建失败：验证 PVC 补偿删除 + destroy 全组清理。"""

    def __init__(self):
        self.deleted_pvcs: list[str] = []
        self.deleted_svcs: list[str] = []
        self.deleted_deployments: list[str] = []

    def CoreV1Api(self):
        return self

    def AppsV1Api(self):
        return self

    def create_namespaced_persistent_volume_claim(self, namespace, body, **kwargs):
        return body

    def create_namespaced_service(self, namespace, body, **kwargs):
        raise RuntimeError("simulated: service create failure")

    def delete_namespaced_deployment(self, name, namespace, **kwargs):
        self.deleted_deployments.append(name)

    def delete_namespaced_service(self, name, namespace, **kwargs):
        self.deleted_svcs.append(name)

    def delete_namespaced_persistent_volume_claim(self, name, namespace, **kwargs):
        self.deleted_pvcs.append(name)


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
    # 补偿清理语义：Service 创建失败时 provision 内补偿删除已创建的 PVC
    # （Service 本身未创建成功，无 Service 可删）；随后 orchestrator 补偿
    # destroy 删除 Deployment/Service/PVC 全组资源（幂等）。两者都必须发生。
    assert provider.destroy_calls >= 1
    assert fake.deleted_pvcs, "PVC 必须被补偿删除（无孤儿 volume）"
    assert fake.deleted_svcs, "destroy 必须清理 Service（K8s 资源组删除）"


def test_k8s_compensating_deletes_404_vs_error(monkeypatch, tmp_path):
    """补偿删除的 404/非 404 分支（此前零覆盖）：
    404 = 资源已不存在 → 幂等成功不抛；其它错误（如 500/鉴权）必须上抛，
    否则「删除失败」被当成「已删除」会造成孤儿资源。"""

    class FakeApiError(RuntimeError):
        def __init__(self, status: int):
            super().__init__(f"kubernetes API {status}")
            self.status = status

    class Core:
        def __init__(self, status: int):
            self.status = status

        def delete_namespaced_service(self, name, namespace, **kwargs):
            raise FakeApiError(self.status)

        def delete_namespaced_persistent_volume_claim(self, name, namespace, **kwargs):
            raise FakeApiError(self.status)

    settings = Settings(eula_accepted=True, k8s_namespace="embodiedcloud")
    provider = KubernetesProvider(settings, _client=object())  # offline 模式：不加载 kubeconfig

    # 404 → 幂等成功（不抛）
    provider._try_delete_service(Core(404), "ns", "svc-1")
    provider._try_delete_pvc(Core(404), "ns", "pvc-1")

    # 非 404 → 必须上抛（不得吞掉真实删除失败）
    with pytest.raises(RuntimeError, match="补偿清理 Service"):
        provider._try_delete_service(Core(500), "ns", "svc-2")
    with pytest.raises(RuntimeError, match="补偿清理 PVC"):
        provider._try_delete_pvc(Core(500), "ns", "pvc-2")


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


def test_fail_persists_failed_state_when_release_raises(monkeypatch, tmp_path):
    """release 抛异常：FAILED + error_message 仍持久化，GPU 字段已清除（release 可被 reconcile 补做）。"""
    provider = MockProvider("http://127.0.0.1:8000")
    with Factory() as db:
        _seed_gpu(db)
        _make_template(db)
        orchestrator = WorkspaceOrchestrator(Factory, provider, Path(tmp_path))
        workspace = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = workspace.id
        # 模拟 _fail 调用前的状态：PROVISIONING 且已持有 GPU（与 _execute_provision 失败前一致）
        gpu = db.scalar(select(Gpu))
        gpu.status = GpuStatus.ALLOCATED.value
        gpu.workspace_id = wid
        ws = db.get(Workspace, wid)
        ws.status = WorkspaceStatus.PROVISIONING.value
        ws.gpu_id = gpu.id
        ws.gpu_index = gpu.gpu_index
        ws.gpu_name = gpu.model
        db.commit()

    def _boom_release(db, workspace_id):
        raise RuntimeError("simulated release failure")

    monkeypatch.setattr(orchestrator.scheduler, "release", _boom_release)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator._fail(db, ws, "boom")
        db.commit()

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.FAILED.value
        assert ws.error_message == "boom"
        assert ws.gpu_id is None
        assert ws.gpu_index is None
        assert ws.gpu_name is None

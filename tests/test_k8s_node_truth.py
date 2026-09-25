"""Kubernetes reservation→node 真相（§10，P0）。

- reservation.node_name 是明确字段（非 host_id 字符串解析）
- K8s provider 真实模式禁止 reservation=None
- nodeSelector 由 reservation.node_name 构造
- reconcile 检测 Pod 实际 nodeName != reserved node → 不得保持 RUNNING（FAILED）
"""

from types import SimpleNamespace

import pytest
from k8s_fakes import make_fake_models
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
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
from app.services.providers.base import ResourceReservation, RuntimeState
from app.services.providers.k8s import KubernetesProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("k8s-node"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


def _seed_with_node(db, node_name: str = "gpu-node-01") -> None:
    GpuScheduler(Factory).sync_host(
        db,
        host_id=f"k8s-node-{node_name}",
        name=node_name,
        address="10.0.0.8",
        provider="k8s",
        gpus=[GpuInfo(gpu_uuid=f"{node_name}:gpu-0", model="K8s GPU", memory_total=24576, index=0)],
    )
    t = Template(
        id="cartpole",
        slug="cartpole",
        name="cartpole",
        description="test",
        category="test",
        runtime="isaaclab",
        launch_command="echo ok",
        enabled=True,
        recommended_vram_gb=16,
        estimated_hourly_cost_cny=1.0,
    )
    db.add(t)
    db.commit()


def test_k8s_provider_requires_reservation(tmp_path):
    """§10：真实模式 reservation=None → 拒绝（scheduler 必须完成决策）。"""
    settings = Settings(eula_accepted=True, k8s_namespace="embodiedcloud")
    provider = KubernetesProvider(settings, _client=SimpleNamespace(), model_factory=make_fake_models)
    ws = Workspace(id="w1", name="w", template_id="t", provider="k8s", status="queued")
    template = Template(
        id="t", slug="t", name="t", description="d", category="c", runtime="isaaclab",
        launch_command="", enabled=True, recommended_vram_gb=16, estimated_hourly_cost_cny=1.0,
    )
    with pytest.raises(RuntimeError, match="ResourceReservation"):
        provider.provision(ws, template, tmp_path / "ws", None)


def test_orchestrator_passes_node_reservation(tmp_path):
    """§10：真实链路验证 —— orchestrator 构造的 reservation 的 node_name 来自
    GpuHost.name（明确字段），而非复刻构造逻辑自证。

    注入 RecordingProvider 捕获 orchestrator 实际传给 provision 的 reservation：
    node_name = GpuHost.name（≠ host_id），gpu 绑定来自 scheduler 分配。
    """
    from pathlib import Path

    from app.services.providers.base import ProvisionResult, RuntimeState

    class RecordingProvider:
        name = "k8s"

        def __init__(self):
            self.reservations: list[ResourceReservation | None] = []

        def health(self):
            return True, "fake"

        def provision(self, workspace, template, workspace_dir, reservation=None):
            self.reservations.append(reservation)
            return ProvisionResult(ide_url="http://10.0.0.8/ide/", password="p")  # noqa: S106 测试数据

        def wait_ready(self, workspace, template, timeout_seconds: int = 120) -> bool:
            return True

        def destroy(self, workspace):
            return None

        def inspect(self, workspace):
            return {}

        def logs(self, workspace, tail: int = 200) -> str:
            return ""

        def reconcile(self, workspace):
            return RuntimeState.UNKNOWN

        def rotate_credentials(self, workspace, credentials) -> bool:
            return False

        @property
        def supports_credential_rotation(self) -> bool:
            return False

    provider = RecordingProvider()
    orchestrator = WorkspaceOrchestrator(Factory, provider, Path(tmp_path) / "node-truth")
    with Factory() as db:
        _seed_with_node(db, node_name="gpu-node-07")
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id=None)
        wid = ws.id

    orchestrator._start(wid)

    assert provider.reservations and provider.reservations[0] is not None
    r = provider.reservations[0]
    assert r.node_name == "gpu-node-07"
    # 与 host_id 不同（明确字段不依赖字符串解析）
    assert r.host_id == "k8s-node-gpu-node-07"
    assert r.node_name != r.host_id
    # GPU 绑定来自 scheduler 真实分配（非手工构造）
    assert r.gpu_id
    assert r.gpu_uuid == "gpu-node-07:gpu-0"


class FakeNodeClient:
    """fake k8s client：记录 deployment 的 nodeSelector。"""

    def __init__(self):
        self.created_deployments: list = []

    def CoreV1Api(self):
        return self

    def AppsV1Api(self):
        return self

    def create_namespaced_deployment(self, namespace, body, **kwargs):
        self.created_deployments.append(body)
        return body

    def create_namespaced_persistent_volume_claim(self, namespace, body, **kwargs):
        return body

    def create_namespaced_service(self, namespace, body, **kwargs):
        return body


def test_pod_selector_matches_reserved_node(tmp_path):
    """§10：nodeSelector = {kubernetes.io/hostname: reservation.node_name}。"""
    fake = FakeNodeClient()
    settings = Settings(eula_accepted=True, k8s_namespace="embodiedcloud")
    provider = KubernetesProvider(settings, _client=fake, model_factory=make_fake_models)
    ws = Workspace(id="w3", name="w", template_id="t", provider="k8s", status="queued")
    template = Template(
        id="t", slug="t", name="t", description="d", category="c", runtime="isaaclab",
        launch_command="", enabled=True, recommended_vram_gb=16, estimated_hourly_cost_cny=1.0,
    )
    reservation = ResourceReservation(
        host_id="k8s-node-gpu-node-01", gpu_id="g1", gpu_uuid="u1",
        gpu_index=0, node_name="gpu-node-01",
    )
    provider.provision(ws, template, tmp_path / "ws", reservation)
    dep = fake.created_deployments[0]
    assert dep.spec.template.spec.node_selector == {"kubernetes.io/hostname": "gpu-node-01"}


class NodeMismatchProvider(KubernetesProvider):
    """inspect 返回与实际 node 不同的 Pod（模拟调度到错误节点）。"""

    def __init__(self, settings, fake):
        super().__init__(settings, _client=fake, model_factory=make_fake_models)
        self.actual_node = "wrong-node-99"

    def inspect(self, workspace):
        return {"node_name": self.actual_node}

    def reconcile(self, workspace):
        return RuntimeState.ALIVE


def test_reconcile_detects_node_mismatch():
    """§10：Pod 实际 node != reserved node → reconcile 置 FAILED + 释放 GPU。"""
    provider = NodeMismatchProvider(
        Settings(eula_accepted=True, k8s_namespace="embodiedcloud"), FakeNodeClient()
    )
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, __import__("pathlib").Path("/tmp/test-k8s-node-2")  # noqa: S108 测试隔离目录
    )
    with Factory() as db:
        _seed_with_node(db, node_name="gpu-node-01")
        ws = Workspace(
            id="w4", name="w", template_id="cartpole", provider="k8s",
            status=WorkspaceStatus.RUNNING.value,
        )
        db.add(ws)
        db.commit()
        gpu = GpuScheduler(Factory).allocate(db, "w4", gpu_requirement_gb=8)
        ws.gpu_id = gpu.id
        db.commit()

    stats = orchestrator.reconcile_all()
    assert stats["failed"] == 1
    with Factory() as db:
        ws = db.get(Workspace, "w4")
        assert ws.status == WorkspaceStatus.FAILED.value
        assert "node mismatch" in ws.error_message
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == "w4")) is None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value


def test_reconcile_keeps_running_when_node_matches():
    """§10：Pod 实际 node == reserved node → 保持 RUNNING（adopt）。"""
    provider = NodeMismatchProvider(
        Settings(eula_accepted=True, k8s_namespace="embodiedcloud"), FakeNodeClient()
    )
    provider.actual_node = "gpu-node-01"  # 与 reserved 一致
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, __import__("pathlib").Path("/tmp/test-k8s-node-3")  # noqa: S108 测试隔离目录
    )
    with Factory() as db:
        _seed_with_node(db, node_name="gpu-node-01")
        ws = Workspace(
            id="w5", name="w", template_id="cartpole", provider="k8s",
            status=WorkspaceStatus.RUNNING.value,
        )
        db.add(ws)
        db.commit()
        gpu = GpuScheduler(Factory).allocate(db, "w5", gpu_requirement_gb=8)
        ws.gpu_id = gpu.id
        db.commit()

    stats = orchestrator.reconcile_all()
    assert stats["failed"] == 0
    with Factory() as db:
        ws = db.get(Workspace, "w5")
        assert ws.status == WorkspaceStatus.RUNNING.value
        assert db.scalar(select(Gpu)).status == GpuStatus.ALLOCATED.value

"""Kubernetes reservation→node 真相（§10，P0）。

- reservation.node_name 是明确字段（非 host_id 字符串解析）
- K8s provider 真实模式禁止 reservation=None
- nodeSelector 由 reservation.node_name 构造
- reconcile 检测 Pod 实际 nodeName != reserved node → 先叫 provider.stop，认账了才 FAILED；
  provider 仍自述 ALIVE 时卡不回池、也不写终态（N-68）
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
    """inspect 返回与实际 node 不同的 Pod（模拟调度到错误节点）。

    `stop`/`reconcile` 是一对有状态的档：`retire_on_stop=True` 是诚实档（停过之后 provider
    亲口说 runtime 没了）；`retire_on_stop=False` 是撒谎档——`stop` 正常返回但 pod 还在，
    K8s 里对应"缩容请求已发、finalizer 还没放行"那一档。释放准入读的是 provider 的自述，
    不是 `stop` 的返回码，所以两档的差别只在 provider 说什么，不在命令成没成。
    `stop` 的签名照抄生产实现（`providers/k8s.py:317` 返回 None）。
    """

    def __init__(self, settings, fake, *, retire_on_stop: bool = True):
        super().__init__(settings, _client=fake, model_factory=make_fake_models)
        self.actual_node = "wrong-node-99"
        self.retire_on_stop = retire_on_stop
        self.stopped = False
        self.stop_calls = 0

    def inspect(self, workspace):
        return {"node_name": self.actual_node}

    def stop(self, workspace) -> None:
        self.stop_calls += 1
        if self.retire_on_stop:
            self.stopped = True

    def reconcile(self, workspace):
        return RuntimeState.MISSING if self.stopped else RuntimeState.ALIVE


def _mismatch_orchestrator(provider):
    return WorkspaceOrchestrator(
        Factory, provider, __import__("pathlib").Path("/tmp/test-k8s-node-2")  # noqa: S108 测试隔离目录
    )


def _seed_mismatch_workspace(workspace_id: str) -> None:
    with Factory() as db:
        _seed_with_node(db, node_name="gpu-node-01")
        ws = Workspace(
            id=workspace_id, name="w", template_id="cartpole", provider="k8s",
            status=WorkspaceStatus.RUNNING.value,
        )
        db.add(ws)
        db.commit()
        gpu = GpuScheduler(Factory).allocate(db, workspace_id, gpu_requirement_gb=8)
        ws.gpu_id = gpu.id
        db.commit()


def test_reconcile_detects_node_mismatch():
    """§10：Pod 实际 node != reserved node → 先停 pod，认账之后才置 FAILED + 释放 GPU。"""
    provider = NodeMismatchProvider(
        Settings(eula_accepted=True, k8s_namespace="embodiedcloud"), FakeNodeClient()
    )
    orchestrator = _mismatch_orchestrator(provider)
    _seed_mismatch_workspace("w4")

    stats = orchestrator.reconcile_all()
    assert stats["failed"] == 1
    # 缺陷的另一半：改前这条路一次都不叫 provider.stop（N-68）
    assert provider.stop_calls == 1, f"节点不一致没有去停那个 pod（stop_calls={provider.stop_calls}）"
    with Factory() as db:
        ws = db.get(Workspace, "w4")
        assert ws.status == WorkspaceStatus.FAILED.value
        assert "node mismatch" in ws.error_message
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == "w4")) is None
        assert db.scalar(select(Gpu)).status == GpuStatus.AVAILABLE.value


def test_node_mismatch_does_not_release_the_gpu_while_the_pod_is_still_alive():
    """撒谎档：stop 返回成功但 provider 仍自述 ALIVE ⇒ 不放卡、不写终态，下一轮接着停。

    这一条钉的是 N-63 的同一条准入在节点不一致分支上同样成立：把卡从还在吃卡的 pod 手里
    放掉就是「一卡双跑」。状态保持 RUNNING 而不是 FAILED/STOPPING —— 既没有可核对的
    runtime 事实支持写终态，也在 `recover_stuck_gpu_allocations` 的保护集内（否则它会
    绕过准入直接把卡清成 AVAILABLE）。
    """
    provider = NodeMismatchProvider(
        Settings(eula_accepted=True, k8s_namespace="embodiedcloud"),
        FakeNodeClient(),
        retire_on_stop=False,
    )
    orchestrator = _mismatch_orchestrator(provider)
    _seed_mismatch_workspace("w6")

    stats = orchestrator.reconcile_all()
    assert stats["failed"] == 0, f"没认账却计了失败：{stats}"
    with Factory() as db:
        ws = db.get(Workspace, "w6")
        assert ws.status == WorkspaceStatus.RUNNING.value
        assert "node mismatch" in ws.error_message
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == "w6")) is not None
        assert db.scalar(select(Gpu)).status == GpuStatus.ALLOCATED.value

    # 第二趟：不是"记一笔就算"，而是真再去停一次（重试仍受同一准入约束）
    orchestrator.reconcile_all()
    assert provider.stop_calls == 2
    with Factory() as db:
        assert db.get(Workspace, "w6").status == WorkspaceStatus.RUNNING.value

    # 单变量：provider 改口之后，同一条分支才把卡放掉并写终态
    provider.retire_on_stop = True
    stats = orchestrator.reconcile_all()
    assert stats["failed"] == 1
    with Factory() as db:
        assert db.get(Workspace, "w6").status == WorkspaceStatus.FAILED.value
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

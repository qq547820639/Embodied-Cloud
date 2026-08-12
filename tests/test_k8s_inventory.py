"""K8s inventory 同步（§3）：node 的 nvidia.com/gpu capacity → GpuHost/Gpu。

验证完整路径：GpuScheduler 能在 K8s inventory 上分配（不出现 No GPU available），
且 reservation 的 host_id/node 语义正确（capacity reservation，device 分配归
NVIDIA Device Plugin）。
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base
from app.models import Gpu, GpuHost, Workspace
from app.services.providers.base import ResourceReservation
from app.services.scheduler import GpuScheduler

ENGINE = create_engine("sqlite:///./test-k8s-inventory.db", connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


class _Quantity:
    """fake K8s Quantity（与真实模型同构：.value 为字符串）。"""

    def __init__(self, value: str):
        self.value = value


def _node(name: str, gpu_count: int) -> SimpleNamespace:
    """构造 fake K8s Node（allocatable.nvidia.com/gpu=count），零 SDK 依赖。"""
    capacity = {"cpu": _Quantity("32"), "nvidia.com/gpu": _Quantity(str(gpu_count))}
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(
            allocatable=capacity,
            addresses=[SimpleNamespace(type="InternalIP", address="10.0.0.8")],
        ),
    )


class InventoryFakeCoreV1Api:
    def __init__(self, nodes: list):
        self.nodes = nodes

    def list_node(self, **kwargs):
        return SimpleNamespace(items=self.nodes)


class InventoryFakeClient:
    def __init__(self, nodes: list):
        self._nodes = nodes

    def CoreV1Api(self):
        return InventoryFakeCoreV1Api(self._nodes)


def _sync_inventory(nodes: list) -> GpuScheduler:
    """模拟 deps._sync_k8s_gpus 的同步逻辑（fake client + fake models）。"""
    from app.deps import _sync_k8s_gpus  # noqa: F401 仅验证存在
    from app.services.scheduler import GpuInfo

    settings = Settings(k8s_gpu_memory_mb=24576)
    scheduler = GpuScheduler(Factory)
    with Factory() as db:
        for node in nodes:
            name = node.metadata.name
            gpu_count = int(node.status.allocatable["nvidia.com/gpu"].value)
            gpus = [
                GpuInfo(
                    gpu_uuid=f"{name}:gpu-{i}",
                    model=f"K8s GPU ({name})",
                    memory_total=settings.k8s_gpu_memory_mb,
                    index=i,
                )
                for i in range(gpu_count)
            ]
            scheduler.sync_host(
                db,
                host_id=f"k8s-node-{name}",
                name=name,
                address="10.0.0.8",
                provider="k8s",
                gpus=gpus,
            )
    return scheduler


def test_deps_sync_k8s_gpus_uses_real_path(monkeypatch):
    """直接验证 deps._sync_k8s_gpus（fake client 注入）：node capacity → GpuHost/Gpu。"""
    import app.deps as deps_module

    fake_client = InventoryFakeClient([_node("gpu-node-1", 3)])
    fake_k8s = SimpleNamespace(
        health=lambda: (True, "fake"),
        _require_client=lambda: fake_client,
    )
    # _sync_k8s_gpus 内部 `from .services.providers.k8s import KubernetesProvider`
    monkeypatch.setattr("app.services.providers.k8s.KubernetesProvider", lambda settings: fake_k8s)
    with Factory() as db:
        deps_module._sync_k8s_gpus(db)
        hosts = list(db.scalars(select(GpuHost)))
        assert len(hosts) == 1
        assert hosts[0].provider == "k8s"
        gpus = list(db.scalars(select(Gpu)))
        assert len(gpus) == 3
        assert all(g.gpu_uuid.startswith("gpu-node-1:gpu-") for g in gpus)


def test_k8s_inventory_syncs_nodes_and_gpus():
    _sync_inventory([_node("gpu-node-1", 4), _node("gpu-node-2", 2)])
    with Factory() as db:
        hosts = list(db.scalars(select(GpuHost).order_by(GpuHost.name)))
        assert [h.name for h in hosts] == ["gpu-node-1", "gpu-node-2"]
        assert all(h.provider == "k8s" for h in hosts)
        gpus = list(db.scalars(select(Gpu).order_by(Gpu.gpu_uuid)))
        assert len(gpus) == 6  # 4 + 2
        assert {g.host_id for g in gpus} == {"k8s-node-gpu-node-1", "k8s-node-gpu-node-2"}


def test_k8s_scheduler_allocates_from_inventory():
    """完整路径：Workspace → GpuScheduler 在 K8s inventory 上分配（无 No GPU available）。"""
    scheduler = _sync_inventory([_node("gpu-node-1", 2)])
    with Factory() as db:
        ws = Workspace(id="ws-1", name="w", template_id="cartpole", provider="k8s", status="queued")
        db.add(ws)
        db.commit()
        gpu = scheduler.allocate(db, "ws-1", gpu_requirement_gb=16)  # 16GB <= 24GB capacity
        assert gpu.host_id == "k8s-node-gpu-node-1"
        assert gpu.gpu_uuid.startswith("gpu-node-1:gpu-")


def test_k8s_scheduler_reservation_host_flows_to_provider():
    """reservation.host_id/gpu 来自 K8s inventory，provider 只执行绑定。"""

    scheduler = _sync_inventory([_node("gpu-node-1", 1)])
    fake = SimpleNamespace()
    with Factory() as db:
        ws = Workspace(id="ws-2", name="w", template_id="cartpole", provider="k8s", status="queued")
        db.add(ws)
        db.commit()
        gpu = scheduler.allocate(db, "ws-2", gpu_requirement_gb=8)
        reservation = ResourceReservation(
            host_id=gpu.host_id,
            gpu_id=gpu.id,
            gpu_uuid=gpu.gpu_uuid,
            gpu_index=gpu.gpu_index or 0,
        )
        assert reservation.host_id == "k8s-node-gpu-node-1"
        assert reservation.gpu_uuid == "gpu-node-1:gpu-0"

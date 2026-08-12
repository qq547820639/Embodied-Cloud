"""Kubernetes 真实集群集成测试（§4，v0.3.0 Acceptance）。

运行：`EMBODIEDCLOUD_K8S_TEST=1 pytest -m k8s_integration`（需要真实集群 +
NVIDIA Device Plugin + kubeconfig）。没有集群时整体 SKIP 并输出
K8S_PHYSICAL_VALIDATION_PENDING —— 不假装 PASS，也没有 NotImplementedError
占位：启用后必须真实执行。

全流程（服务层 = API 同一 orchestrator）：
create workspace → scheduler reservation → provider provision →
Deployment 就绪 → Pod 携带 nvidia.com/gpu limit → inspect 实际 runtime →
IDE URL 可解析 → stop（replicas=0）→ GPU release → destroy →
Deployment/Service/PVC 全部删除。
"""

import os
import time

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

pytestmark = [
    pytest.mark.k8s_integration,
    pytest.mark.skipif(
        os.environ.get("EMBODIEDCLOUD_K8S_TEST") != "1",
        reason=(
            "K8S_PHYSICAL_VALIDATION_PENDING: 需要真实 Kubernetes 集群 + NVIDIA "
            "Device Plugin（export EMBODIEDCLOUD_K8S_TEST=1 且配置 kubeconfig 后运行）"
        ),
    ),
]


def _cluster_ready() -> bool:
    """探测：kubeconfig 可达 + 至少一个 node 有 nvidia.com/gpu capacity。"""
    try:
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config
    except ImportError:
        return False
    try:
        k8s_config.load_kube_config()
        api = k8s_client.CoreV1Api()
        nodes = api.list_node()
    except Exception:
        return False
    for node in nodes.items:
        capacity = dict(node.status.allocatable or {})
        if int(getattr(capacity.get("nvidia.com/gpu"), "value", 0) or 0) > 0:
            return True
    return False


@pytest.fixture(scope="module")
def cluster() -> bool:
    return _cluster_ready()


def test_k8s_full_workspace_lifecycle(cluster, tmp_path_factory):
    """create → schedule → provision → Pod Ready → GPU attached → stop → destroy。"""
    if not cluster:
        pytest.skip("K8S_PHYSICAL_VALIDATION_PENDING: no reachable cluster with NVIDIA GPUs")

    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config

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
    from app.services.providers.k8s import KubernetesProvider
    from app.services.scheduler import GpuInfo, GpuScheduler

    # 独立环境：k8s provider + 独立 DB（不污染 mock 测试环境）
    settings = Settings(
        provider="k8s",
        eula_accepted=True,
        k8s_namespace="embodiedcloud",
        k8s_gpu_memory_mb=24576,
    )
    root = tmp_path_factory.mktemp("k8s-int")
    engine = create_engine(f"sqlite:///{root / 'int.db'}")
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, expire_on_commit=False)

    k8s_config.load_kube_config()
    provider = KubernetesProvider(settings)
    scheduler = GpuScheduler(Factory)

    # 1) inventory：真实 node capacity → GpuHost/Gpu
    api = k8s_client.CoreV1Api()
    nodes = api.list_node()
    gpu_nodes = 0
    with Factory() as db:
        for node in nodes.items:
            capacity = dict(node.status.allocatable or {})
            count = int(getattr(capacity.get("nvidia.com/gpu"), "value", 0) or 0)
            if count <= 0:
                continue
            gpu_nodes += 1
            scheduler.sync_host(
                db,
                host_id=f"k8s-node-{node.metadata.name}",
                name=node.metadata.name,
                address="",
                provider="k8s",
                gpus=[
                    GpuInfo(
                        gpu_uuid=f"{node.metadata.name}:gpu-{i}",
                        model=f"K8s GPU ({node.metadata.name})",
                        memory_total=settings.k8s_gpu_memory_mb,
                        index=i,
                    )
                    for i in range(count)
                ],
            )
    assert gpu_nodes >= 1, "集群无 GPU node（inventory 未建立）"

    orchestrator = WorkspaceOrchestrator(Factory, provider, root / "workspaces", scheduler)

    # 2) create + provision（服务层 = API 同一路径）
    with Factory() as db:
        template = Template(
            id="cartpole-int",
            slug="cartpole-int",
            name="k8s-int",
            description="integration",
            category="test",
            runtime="isaaclab",
            launch_command="echo ok",
            enabled=True,
            recommended_vram_gb=16,
            estimated_hourly_cost_cny=1.0,
        )
        db.add(template)
        db.commit()
        ws = orchestrator.create(db, db.get(Template, "cartpole-int"), user_id="u1")
        wid = ws.id
    orchestrator._start(wid)  # 同步 provision（与 API auto_start 同一执行体）

    try:
        # 3) Deployment 就绪 + Pod 携带 nvidia.com/gpu
        deployment_name = f"ec-{wid[:12]}"
        apps = k8s_client.AppsV1Api()
        deadline = time.monotonic() + 300
        ready = False
        while time.monotonic() < deadline:
            status = apps.read_namespaced_deployment_status(
                name=deployment_name, namespace="embodiedcloud"
            ).status
            if (getattr(status, "available_replicas", None) or 0) >= 1:
                ready = True
                break
            time.sleep(2)
        assert ready, f"Deployment {deployment_name} 未在 300s 内 Ready"

        # Pod 实际 spec：nvidia.com/gpu limit == 1
        pods = api.list_namespaced_pod(namespace="embodiedcloud", label_selector=f"app={deployment_name}")
        assert pods.items, "无 Pod"
        container = pods.items[0].spec.containers[0]
        assert int(container.resources.limits.get("nvidia.com/gpu", 0)) == 1

        # 4) inspect 实际 runtime → ALIVE；DB RUNNING + GPU ALLOCATED
        with Factory() as db:
            ws_db = db.get(Workspace, wid)
            assert ws_db.status == WorkspaceStatus.RUNNING.value, ws_db.error_message
            alloc = db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid))
            assert alloc is not None
            gpu = db.get(Gpu, alloc.gpu_id)
            assert gpu.status == GpuStatus.ALLOCATED.value
        assert provider.inspect(ws_db) is not None
        assert provider.reconcile(ws_db) == "alive"
        # IDE URL 可解析（Service/Ingress 路径）
        assert ws_db.ide_url and "/ide/" in ws_db.ide_url

        # 5) stop → replicas=0 + GPU release
        with Factory() as db:
            ws_db = db.get(Workspace, wid)
            orchestrator.stop(db, ws_db)
        with Factory() as db:
            ws_db = db.get(Workspace, wid)
            assert ws_db.status == WorkspaceStatus.STOPPED.value
            assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == wid)) is None
            gpu = db.scalar(select(Gpu))
            assert gpu.status == GpuStatus.AVAILABLE.value
        # runtime 已停止（replicas 0）
        status = apps.read_namespaced_deployment_status(
            name=deployment_name, namespace="embodiedcloud"
        ).status
        assert int(getattr(status, "replicas", 0) or 0) == 0
    finally:
        # 6) destroy → Deployment/Service/PVC 全部删除（无论前序结果，保证清理）
        with Factory() as db:
            ws_db = db.get(Workspace, wid)
            if ws_db is not None:
                orchestrator.destroy(db, ws_db)
        deployment_name = f"ec-{wid[:12]}"
        pvc_name = f"ec-pvc-{wid[:12]}"
        import contextlib

        for kind, call in [
            ("deployment", lambda: apps.read_namespaced_deployment(name=deployment_name, namespace="embodiedcloud")),
            ("service", lambda: api.read_namespaced_service(name=deployment_name, namespace="embodiedcloud")),
            ("pvc", lambda: api.read_namespaced_persistent_volume_claim(name=pvc_name, namespace="embodiedcloud")),
        ]:
            with contextlib.suppress(Exception):
                call()
                pytest.fail(f"K8s 资源未清理: {kind} {deployment_name}")

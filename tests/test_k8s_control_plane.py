"""Kubernetes provider 真控制面档（§4/§9/§20）。

`tests/test_k8s_integration.py` 要的是"带 NVIDIA Device Plugin 的集群"，本机没有，
于是 `wait_ready` / `rotate_credentials` / `supports_credential_rotation` 三条
控制面路径从未执行过。本档用 kind 起的**真集群**（真 kubelet、真调度器、真
endpoints 控制器）把这三条跑到，并且不依赖 GPU：

- provision 出来的 Deployment 原样保留 `nvidia.com/gpu` limit ⇒ Pod 必然
  Pending（`Insufficient nvidia.com/gpu`），这正好是 `wait_ready` 的**负向对照**；
- 随后只把**一个字段**换掉（container 的 `resources`，生产值是 cpu 4 / mem 16Gi，
  本机节点 allocatable 只有 4 CPU / 5.8Gi，即便有 GPU 也调度不动）⇒
  Pod 真的 Running+Ready、Endpoints 真的有地址 ⇒ `wait_ready` 返回 True。
  除此之外不改任何东西：名字、labels、selector、Service 端口、PVC、env 全部
  来自生产代码路径（`workspace.image` 走 TemplateVersion 快照那条优先级）。

仍属物理档（不假装验证）：`nvidia.com/gpu` 能否被 device plugin 实际分配、
Isaac Sim 镜像能否跑起来。见 scripts/gpu_acceptance.sh 与 docs/ACCEPTANCE_GATES.md G1。
"""

import os
import time
import uuid
from pathlib import Path

import pytest
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from app.config import Settings
from app.models import Template, Workspace
from app.services.providers.base import ResourceReservation
from app.services.providers.k8s import KubernetesProvider
from tests.k8s_server import GATE_SENTINEL, KindCluster, gate_reason

pytestmark = pytest.mark.k8s_control_plane

NAMESPACE = "ec-k8s-suite"


@pytest.fixture(scope="module")
def cluster():
    reason = gate_reason()
    if reason is not None:
        pytest.skip(f"{GATE_SENTINEL}: {reason}")
    built = KindCluster().start()
    previous = os.environ.get("KUBECONFIG")
    os.environ["KUBECONFIG"] = built.kubeconfig
    try:
        yield built
    finally:
        if previous is None:
            os.environ.pop("KUBECONFIG", None)
        else:
            os.environ["KUBECONFIG"] = previous
        built.stop()


@pytest.fixture(scope="module")
def api(cluster):
    k8s_config.load_kube_config(config_file=cluster.kubeconfig)
    core = k8s_client.CoreV1Api()
    core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=NAMESPACE)))
    return core


@pytest.fixture(scope="module")
def node_name(api) -> str:
    return api.list_node().items[0].metadata.name


@pytest.fixture
def provider(cluster, api, node_name) -> KubernetesProvider:
    """真客户端 provider（不注入 fake），命名空间指向本档专用 ns。

    kubeconfig 走 `k8s_kubeconfig` 显式指定：kubernetes SDK 在 import 期就把
    `KUBECONFIG` 环境变量固化（`KUBE_CONFIG_DEFAULT_LOCATION`），而 pytest 进程里
    kubernetes 模块早于本档的 fixture 就被 import 了，改环境变量不会生效。
    """
    return KubernetesProvider(
        Settings(
            provider="k8s",
            database_url="sqlite:///:memory:",
            eula_accepted=True,
            k8s_namespace=NAMESPACE,
            k8s_in_cluster=False,
            k8s_kubeconfig=cluster.kubeconfig,
        )
    )


def _workspace() -> Workspace:
    return Workspace(id=str(uuid.uuid4()), name="k8s-tier", template_id="cartpole", provider="k8s")


def _reservation(node_name: str, gpu_index: int = 0) -> ResourceReservation:
    return ResourceReservation(
        host_id=node_name,
        gpu_id="gpu-0",
        gpu_uuid="GPU-deadbeef",
        gpu_index=gpu_index,
        gpu_count=1,
        node_name=node_name,
        metadata={"operation_id": "op-k8s", "fencing_token": 3},
    )


def _provisioned(provider: KubernetesProvider, cluster: KindCluster, node_name: str, tmp_path: Path):
    """走生产 provision，返回 (workspace, template, ProvisionResult)。"""
    workspace = _workspace()
    workspace.image = cluster.pause_image()  # TemplateVersion 快照优先级，非测试特判
    template = Template(
        id="cartpole", slug="cartpole", name="CartPole", description="d", category="rl", runtime="k8s"
    )
    result = provider.provision(workspace, template, tmp_path / "ws", _reservation(node_name))
    workspace.container_name = result.container_name
    return workspace, template, result


def _make_schedulable(deployment_name: str) -> None:
    """唯一的 fixture 差异：把 container 的 `resources` 整块换掉。

    两处必要：
    1. `nvidia.com/gpu` limit —— 测试集群没有 device plugin，也没有它可分配；
    2. 生产的 cpu 4 / memory 16Gi —— kind 节点 allocatable 实测只有 4 CPU /
       5.8 GiB，连它都调度不动（集群自己报 "Insufficient cpu, Insufficient memory"）。

    用 read-modify-write 整块替换而不是 patch：SMP 对 `limits` 这种 map 是
    **按键合并**，实测 patch `{"limits":{"cpu":"100m","memory":"64Mi"}}` 之后
    `nvidia.com/gpu` 仍然留在新 ReplicaSet 的模板里，Pod 照样 Unschedulable。

    replace 带 resourceVersion ⇒ 控制器刚写完 status 时会撞 409（实测出现过），
    所以按乐观并发重试：每次重新读再改。
    """
    apps = _apps()
    for attempt in range(10):
        dep = apps.read_namespaced_deployment(name=deployment_name, namespace=NAMESPACE)
        container = dep.spec.template.spec.containers[0]
        container.resources.requests = {"cpu": "50m", "memory": "32Mi"}
        container.resources.limits = {"cpu": "100m", "memory": "64Mi"}
        try:
            apps.replace_namespaced_deployment(name=deployment_name, namespace=NAMESPACE, body=dep)
            return
        except k8s_client.ApiException as exc:
            if exc.status != 409 or attempt == 9:
                raise
            time.sleep(0.5)


def _apps() -> k8s_client.AppsV1Api:
    """AppsV1Api（`api` fixture 是 CoreV1Api 实例，不能从它取子 API）。"""
    return k8s_client.AppsV1Api()


def _pods(core: k8s_client.CoreV1Api, deployment_name: str):
    return core.list_namespaced_pod(namespace=NAMESPACE, label_selector=f"app={deployment_name}").items


def _env(pod) -> dict[str, str]:
    return {e.name: (e.value or "") for e in pod.spec.containers[0].env}


# ---------------------------------------------------------------------------
# health / provision 对象图
# ---------------------------------------------------------------------------


def test_health_reaches_the_real_api_server(provider, cluster):
    ok, detail = provider.health()
    assert ok is True, detail
    assert detail == "kubernetes ready"
    # 独立证据：SDK 直接问版本，确认不是"客户端自说自话"
    k8s_config.load_kube_config(config_file=cluster.kubeconfig)
    version = k8s_client.VersionApi().get_code()
    assert version.git_version.startswith("v1."), version.git_version


def test_provision_creates_pvc_service_and_deployment_as_designed(provider, cluster, node_name, api, tmp_path):
    workspace, _template, result = _provisioned(provider, cluster, node_name, tmp_path)
    try:
        name = workspace.container_name
        assert name == result.container_name == f"ec-{workspace.id[:12]}"
        dep = _apps().read_namespaced_deployment(name=name, namespace=NAMESPACE)
        svc = api.read_namespaced_service(name=name, namespace=NAMESPACE)
        pvc = api.read_namespaced_persistent_volume_claim(f"ec-pvc-{workspace.id[:12]}", namespace=NAMESPACE)

        # §14 NetworkPolicy selector 依据：Pod 模板要带 embodiedcloud.workspace 标记
        # （实测形态：Pod 侧是 "true" 标记，Deployment 侧才带 workspace id）
        assert dep.spec.template.metadata.labels["embodiedcloud.workspace"] == "true"
        assert dep.metadata.labels["embodiedcloud.workspace"] == workspace.id
        assert dep.spec.selector.match_labels == {"app": name}
        assert svc.spec.selector == {"app": name}
        assert svc.spec.ports[0].port == KubernetesProvider.IDE_PORT
        assert pvc.spec.resources.requests["storage"] == KubernetesProvider.STORAGE
        assert pvc.spec.access_modes == ["ReadWriteOnce"]

        container = dep.spec.template.spec.containers[0]
        # GPU 声明完全来自 reservation（唯一权威）；nodeSelector 是 §10 的明确字段
        assert container.resources.limits["nvidia.com/gpu"] == "1"
        assert dep.spec.template.spec.node_selector == {"kubernetes.io/hostname": node_name}
        env = {e.name: e.value for e in container.env}
        assert env["ACCEPT_EULA"] == "Y"
        assert len(env["WORKSPACE_PASSWORD"]) >= 16
        assert env["LIVESTREAM"] == "0"
        assert container.image == cluster.pause_image()
        assert result.password == env["WORKSPACE_PASSWORD"]
    finally:
        provider.destroy(workspace)


# ---------------------------------------------------------------------------
# wait_ready：负向对照 + 真实就绪
# ---------------------------------------------------------------------------


def test_wait_ready_is_false_while_the_gpu_request_cannot_be_scheduled(provider, cluster, node_name, api, tmp_path):
    """没有 device plugin ⇒ 真集群自己判定调度不了 ⇒ wait_ready 必须 False。

    这条是下一条的对照：如果 wait_ready 是"无条件 True"，这里就红。
    """
    workspace, template, _result = _provisioned(provider, cluster, node_name, tmp_path)
    try:
        assert provider.wait_ready(workspace, template, timeout_seconds=6) is False
        pod = _pods(api, workspace.container_name)[0]
        reasons = [c.reason for c in (pod.status.conditions or []) if c.status == "False"]
        assert "Unschedulable" in reasons, reasons  # 集群自己的判词，不是我们编的
    finally:
        provider.destroy(workspace)


def test_wait_ready_true_on_real_pod_ready_and_non_empty_endpoints(provider, cluster, node_name, api, tmp_path):
    """唯一的 fixture 差异（resources）改完，真 kubelet 让三段判据一起成立。"""
    workspace, template, _result = _provisioned(provider, cluster, node_name, tmp_path)
    try:
        _make_schedulable(workspace.container_name)
        assert provider.wait_ready(workspace, template, timeout_seconds=180) is True

        # 独立复核 wait_ready 读到的三段事实（不依赖 provider 的判读）
        status = _apps().read_namespaced_deployment_status(
            name=workspace.container_name, namespace=NAMESPACE
        ).status
        assert (status.available_replicas or 0) >= 1
        pod = _pods(api, workspace.container_name)[0]
        assert pod.status.phase == "Running"
        assert {c.type: c.status for c in pod.status.conditions}["Ready"] == "True"
        endpoints = api.read_namespaced_endpoints(name=workspace.container_name, namespace=NAMESPACE)
        assert [a.ip for s in (endpoints.subsets or []) for a in (s.addresses or [])], endpoints
    finally:
        provider.destroy(workspace)


# ---------------------------------------------------------------------------
# 凭据轮换：K8s 的"能换"必须真的换到新 Pod 上
# ---------------------------------------------------------------------------


def test_rotate_credentials_rolls_the_pod_onto_the_new_password(provider, cluster, node_name, api, tmp_path):
    workspace, template, result = _provisioned(provider, cluster, node_name, tmp_path)
    try:
        _make_schedulable(workspace.container_name)
        assert provider.wait_ready(workspace, template, timeout_seconds=180) is True
        old_password = result.password
        old_uid = _pods(api, workspace.container_name)[0].metadata.uid

        assert provider.supports_credential_rotation is True
        new_password = "rotated-" + uuid.uuid4().hex
        assert provider.rotate_credentials(workspace, {"password": new_password}) is True

        deadline = time.monotonic() + 180
        seen: list[tuple[str, str, str]] = []
        while time.monotonic() < deadline:
            pods = _pods(api, workspace.container_name)
            running_new = [
                p for p in pods
                if p.status.phase == "Running" and _env(p).get("WORKSPACE_PASSWORD") == new_password
            ]
            holding_old = [p for p in pods if _env(p).get("WORKSPACE_PASSWORD") == old_password]
            if running_new and not holding_old:
                # 真的换了一个 Pod（滚动重建），不是原地改 env——env 在容器创建后不可变
                assert {p.metadata.uid for p in running_new} != {old_uid}
                break
            seen = [(p.metadata.name[:14], p.status.phase, _env(p).get("WORKSPACE_PASSWORD", "")[:10]) for p in pods]
            time.sleep(2)
        else:
            pytest.fail(
                "轮换后没有出现「新口令在跑且没有任何 Pod 还持旧口令」的状态；最后一轮观测："
                f"{seen}（uid {old_uid[:8]} → 期望换新）"
            )
    finally:
        provider.destroy(workspace)


def test_rotate_credentials_refuses_empty_password(provider, cluster, node_name, api, tmp_path):
    """没给口令就不能谎报成功（返回 False，且不动 spec）。"""
    workspace, _template, _result = _provisioned(provider, cluster, node_name, tmp_path)
    try:
        dep = _apps().read_namespaced_deployment(name=workspace.container_name, namespace=NAMESPACE)
        generation = dep.metadata.generation
        env_before = [(e.name, e.value) for e in dep.spec.template.spec.containers[0].env]
        assert provider.rotate_credentials(workspace, {}) is False
        assert provider.rotate_credentials(workspace, {"password": ""}) is False
        after = _apps().read_namespaced_deployment(name=workspace.container_name, namespace=NAMESPACE)
        # 用 generation 而不是 resourceVersion 判"没动过"：控制器写 status 也会 bump
        # resourceVersion（实测 740 → 744），只有 spec 变更才 bump generation
        assert after.metadata.generation == generation
        assert [(e.name, e.value) for e in after.spec.template.spec.containers[0].env] == env_before
    finally:
        provider.destroy(workspace)


# ---------------------------------------------------------------------------
# stop / start / inspect / destroy
# ---------------------------------------------------------------------------


def test_stop_start_scale_the_real_deployment_and_destroy_leaves_nothing(provider, cluster, node_name, api, tmp_path):
    workspace, template, _result = _provisioned(provider, cluster, node_name, tmp_path)
    name = workspace.container_name
    try:
        _make_schedulable(name)
        assert provider.wait_ready(workspace, template, timeout_seconds=180) is True

        provider.stop(workspace)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and _pods(api, name):
            time.sleep(2)
        assert not _pods(api, name), "stop 后仍有 Pod 在跑"
        assert provider.inspect(workspace)["ready_replicas"] in (None, 0)

        provider.start(workspace)
        assert provider.wait_ready(workspace, template, timeout_seconds=180) is True
        assert provider.inspect(workspace)["available_replicas"] == 1
    finally:
        provider.destroy(workspace)

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        dep = _apps().list_namespaced_deployment(namespace=NAMESPACE).items
        svc = api.list_namespaced_service(namespace=NAMESPACE).items
        pvcs = api.list_namespaced_persistent_volume_claim(namespace=NAMESPACE).items
        if not (dep or svc or pvcs):
            return
        time.sleep(2)
    pytest.fail(
        "destroy 后集群里仍残留 "
        f"deployments={[d.metadata.name for d in dep]} services={[s.metadata.name for s in svc]} "
        f"pvcs={[p.metadata.name for p in pvcs]}"
    )

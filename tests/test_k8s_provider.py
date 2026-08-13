"""KubernetesProvider 单元测试: 全部使用 fake kubernetes client, 不连接真实集群."""

from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

try:  # 仅用于 monkeypatch 真实 config 加载；无 SDK 环境相关测试自动 skip
    from kubernetes import config as kube_config
except ImportError:  # pragma: no cover - offline 环境无 SDK
    kube_config = None

from k8s_fakes import make_fake_models

from app.config import Settings
from app.models import Template, Workspace
from app.services.providers.base import ResourceReservation
from app.services.providers.k8s import KubernetesProvider

# workspace.id[:12] = "11111111-222" (12 字符)
DEPLOYMENT_NAME = "ec-11111111-222"
PVC_NAME = "ec-pvc-11111111-222"


# ---------------------------------------------------------------------------
# fake kubernetes client (记录所有 API 调用)
# ---------------------------------------------------------------------------


class FakeCoreV1Api:
    def __init__(self, calls: list):
        self.calls = calls

    def list_namespace(self, **kwargs):
        self.calls.append(("list_namespace", kwargs))
        return SimpleNamespace(items=[])

    def create_namespaced_persistent_volume_claim(self, namespace, body, **kwargs):
        self.calls.append(("create_pvc", namespace, body))
        return body

    def create_namespaced_service(self, namespace, body, **kwargs):
        self.calls.append(("create_service", namespace, body))
        return body

    def delete_namespaced_service(self, name, namespace, **kwargs):
        self.calls.append(("delete_service", namespace, name))

    def delete_namespaced_persistent_volume_claim(self, name, namespace, **kwargs):
        self.calls.append(("delete_pvc", namespace, name))

    def list_namespaced_pod(self, namespace, label_selector=None, **kwargs):
        self.calls.append(("list_pods", namespace, label_selector))
        return SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name="ec-pod-1"))])

    def read_namespaced_pod_log(self, name, namespace, tail_lines=None, **kwargs):
        self.calls.append(("read_log", namespace, name, tail_lines))
        return "fake pod log"


class FakeAppsV1Api:
    def __init__(self, calls: list):
        self.calls = calls

    def create_namespaced_deployment(self, namespace, body, **kwargs):
        self.calls.append(("create_deployment", namespace, body))
        return body

    def delete_namespaced_deployment(self, name, namespace, **kwargs):
        self.calls.append(("delete_deployment", namespace, name))

    def patch_namespaced_deployment_scale(self, name, namespace, body, **kwargs):
        self.calls.append(("patch_scale", namespace, name, body))

    def read_namespaced_deployment_status(self, name, namespace, **kwargs):
        status = SimpleNamespace(
            replicas=1,
            ready_replicas=1,
            available_replicas=1,
            conditions=[SimpleNamespace(type="Available", status="True", reason="ok", message="deployed")],
        )
        return SimpleNamespace(status=status)


class FakeClientModule:
    """模拟 kubernetes.client 模块: 只提供 CoreV1Api / AppsV1Api 两个入口."""

    def __init__(self):
        self.calls: list = []

    def CoreV1Api(self):
        return FakeCoreV1Api(self.calls)

    def AppsV1Api(self):
        return FakeAppsV1Api(self.calls)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def make_template(streaming: bool = False) -> Template:
    return Template(
        id="test-template",
        name="Test",
        description="test",
        category="test",
        runtime="isaaclab",
        launch_command="echo ok",
        requires_streaming=streaming,
        recommended_vram_gb=16,
        estimated_hourly_cost_cny=1.0,
        enabled=True,
    )


def make_workspace() -> Workspace:
    return Workspace(
        id="11111111-2222-3333-4444-555555555555",
        name="Test Workspace",
        template_id="test-template",
        provider="k8s",
        status="queued",
        gpu_index=2,
        gpu_name="Fake RTX 4090",
    )


def make_settings(**overrides) -> Settings:
    base = {"eula_accepted": True, "k8s_namespace": "embodiedcloud", "host_public_ip": "10.0.0.8"}
    base.update(overrides)
    return Settings(**base)


def make_provider(**overrides) -> tuple[KubernetesProvider, FakeClientModule]:
    fake = FakeClientModule()
    # offline 测试必须注入 fake model layer（不依赖真实 Kubernetes SDK 模型类）
    return (
        KubernetesProvider(make_settings(**overrides), _client=fake, model_factory=make_fake_models),
        fake,
    )


def make_reservation(gpu_index: int = 2, gpu_id: str = "gpu-k8s-1") -> ResourceReservation:
    return ResourceReservation(
        host_id="k8s-node-1",
        gpu_id=gpu_id,
        gpu_uuid="GPU-k8s-uuid-1",
        gpu_index=gpu_index,
        metadata={"node_selector": {"kubernetes.io/hostname": "gpu-node-01"}},
    )


def running_workspace() -> Workspace:
    ws = make_workspace()
    ws.container_name = DEPLOYMENT_NAME
    return ws


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@pytest.mark.skipif(kube_config is None, reason="kubernetes SDK not installed")
def test_health_false_without_kubeconfig(monkeypatch):
    """无 kubeconfig 时 health 返回 (False, 原因) 且不抛异常."""

    def boom(*args, **kwargs):
        raise FileNotFoundError("no kubeconfig found")

    monkeypatch.setattr(kube_config, "load_kube_config", boom)
    provider = KubernetesProvider(make_settings(eula_accepted=False))

    ok, detail = provider.health()

    assert ok is False
    assert "no kubeconfig found" in detail


def test_health_ok_with_fake_client():
    """离线模式 (注入 fake client) health 直接 OK."""
    provider, _ = make_provider()
    ok, detail = provider.health()
    assert ok is True
    assert "ready" in detail


# ---------------------------------------------------------------------------
# provision
# ---------------------------------------------------------------------------


def test_provision_requires_eula(tmp_path):
    provider, _ = make_provider(eula_accepted=False)
    with pytest.raises(RuntimeError, match="EMBODIEDCLOUD_EULA_ACCEPTED"):
        provider.provision(make_workspace(), make_template(), tmp_path / "ws")


def test_provision_creates_deployment_service_pvc(tmp_path):
    provider, fake = make_provider()
    result = provider.provision(make_workspace(), make_template(), tmp_path / "ws", make_reservation())

    # ProvisionResult 字段（GPU 信息不属于 provision 决策，来自 scheduler reservation）
    assert result.container_name == DEPLOYMENT_NAME
    assert result.password
    assert result.ide_url == "http://10.0.0.8/ide/11111111-222/"
    assert result.ide_port is None

    kinds = [c[0] for c in fake.calls]
    assert "create_pvc" in kinds
    assert "create_service" in kinds
    assert "create_deployment" in kinds

    # PVC 名称正确且使用默认 StorageClass
    _, _, pvc = next(c for c in fake.calls if c[0] == "create_pvc")
    assert pvc.metadata.name == PVC_NAME
    assert pvc.spec.storage_class_name is None

    # Service 同名暴露 IDE 端口
    _, _, service = next(c for c in fake.calls if c[0] == "create_service")
    assert service.metadata.name == DEPLOYMENT_NAME
    assert service.spec.ports[0].port == 18000

    # Deployment 名称 / 镜像 / env / GPU 资源声明
    _, _, dep = next(c for c in fake.calls if c[0] == "create_deployment")
    assert dep.metadata.name == DEPLOYMENT_NAME
    container = dep.spec.template.spec.containers[0]
    assert container.image == "embodiedcloud/isaaclab-workspace:0.1.0"
    env = {e.name: e.value for e in container.env}
    assert env["ACCEPT_EULA"] == "Y"
    assert env["WORKSPACE_PASSWORD"]
    assert env["IDE_PORT"] == "18000"
    assert env["LIVESTREAM"] == "0"
    assert env["PUBLIC_IP"] == "10.0.0.8"

    # GPU 绑定严格来自 reservation：nvidia.com/gpu limit == reservation.gpu_count
    assert container.resources.limits["nvidia.com/gpu"] == "1"
    # nodeSelector 来自 reservation.metadata
    assert dep.spec.template.spec.node_selector == {"kubernetes.io/hostname": "gpu-node-01"}

    # 无 privileged / 无 docker.sock (hostPath 挂载) / 无 hostNetwork
    assert container.security_context is None
    assert dep.spec.template.spec.host_network is None
    volumes = dep.spec.template.spec.volumes
    assert all(v.host_path is None for v in volumes)
    assert volumes[0].persistent_volume_claim.claim_name == PVC_NAME


def test_provision_streaming_template_sets_livestream(tmp_path):
    provider, fake = make_provider()
    provider.provision(make_workspace(), make_template(streaming=True), tmp_path / "ws", make_reservation())
    _, _, dep = next(c for c in fake.calls if c[0] == "create_deployment")
    env = {e.name: e.value for e in dep.spec.template.spec.containers[0].env}
    assert env["LIVESTREAM"] == "1"


# ---------------------------------------------------------------------------
# stop / destroy
# ---------------------------------------------------------------------------


def test_stop_scales_replicas_to_zero():
    provider, fake = make_provider()

    provider.stop(running_workspace())

    assert len(fake.calls) == 1
    kind, ns, name, body = fake.calls[0]
    assert kind == "patch_scale"
    assert ns == "embodiedcloud"
    assert name == DEPLOYMENT_NAME
    assert body["spec"]["replicas"] == 0


def test_destroy_deletes_deployment_service_pvc():
    provider, fake = make_provider()

    provider.destroy(running_workspace())

    kinds = [c[0] for c in fake.calls]
    assert kinds == ["delete_deployment", "delete_service", "delete_pvc"]
    _, _, svc_name = next(c for c in fake.calls if c[0] == "delete_service")
    assert svc_name == DEPLOYMENT_NAME
    _, _, pvc_name = next(c for c in fake.calls if c[0] == "delete_pvc")
    assert pvc_name == PVC_NAME


# ---------------------------------------------------------------------------
# inspect / logs
# ---------------------------------------------------------------------------


def test_inspect_and_logs():
    provider, _ = make_provider()

    status = provider.inspect(running_workspace())
    assert status["replicas"] == 1
    assert status["ready_replicas"] == 1
    assert status["conditions"][0]["type"] == "Available"

    assert provider.logs(running_workspace()) == "fake pod log"


def test_start_scales_replicas_to_one():
    provider, fake = make_provider()

    provider.start(running_workspace())

    assert len(fake.calls) == 1
    kind, _, _, body = fake.calls[0]
    assert kind == "patch_scale"
    assert body["spec"]["replicas"] == 1


def test_reconcile_alive_when_available_replica():
    provider, _ = make_provider()
    assert provider.reconcile(running_workspace()) == "alive"


def test_offline_provision_never_imports_kubernetes_sdk(tmp_path, monkeypatch):
    """隔离语义证明：offline + fake models 的 provision 全程不得 import kubernetes SDK。"""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "kubernetes" or name.startswith("kubernetes."):
            raise ImportError("kubernetes SDK imported in offline test path")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    provider, _ = make_provider()
    result = provider.provision(make_workspace(), make_template(), tmp_path / "ws", make_reservation())
    assert result.container_name == DEPLOYMENT_NAME


# ---------------------------------------------------------------------------
# pull_artifact：kubectl cp 命令构造（真实集群执行待物理验证）
# ---------------------------------------------------------------------------


def test_pull_artifact_kubectl_cp(tmp_path, monkeypatch):
    """先用 client 列 Pod（定位 Deployment 的 Pod），再 kubectl cp 到临时目录。"""
    provider, fake = make_provider()
    ws = running_workspace()
    commands: list[list[str]] = []

    def fake_run(args, *, check=True):
        commands.append(args)
        if args[:2] == ["kubectl", "cp"]:
            dest = Path(args[-1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"artifact-bytes")
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provider, "_run", fake_run)
    result = provider.pull_artifact(ws, "outputs/checkpoint.pt")

    # 1) list pod：label_selector 定位 Deployment 的 Pod
    kinds = [c[0] for c in fake.calls]
    assert "list_pods" in kinds
    _, ns, selector = next(c for c in fake.calls if c[0] == "list_pods")
    assert ns == "embodiedcloud"
    assert selector == "app=ec-11111111-222"

    # 2) kubectl cp -n {ns} {pod}:{path} {tmp}
    assert commands[0][:2] == ["kubectl", "cp"]
    assert commands[0][2] == "-n"
    assert commands[0][3] == "embodiedcloud"
    assert commands[0][4] == "ec-pod-1:outputs/checkpoint.pt"
    assert commands[0][5] == str(result)
    assert result.read_bytes() == b"artifact-bytes"


def test_pull_artifact_derives_deployment_name_when_container_missing(tmp_path, monkeypatch):
    """container_name 缺失时按 ec-{id[:12]} 推导（与 destroy 一致）。"""
    provider, fake = make_provider()
    ws = make_workspace()  # container_name 为 None
    commands: list[list[str]] = []

    def fake_run(args, *, check=True):
        commands.append(args)
        if args[:2] == ["kubectl", "cp"]:
            Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(args[-1]).write_bytes(b"x")
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provider, "_run", fake_run)
    provider.pull_artifact(ws, "checkpoint.pt")

    _, _, selector = next(c for c in fake.calls if c[0] == "list_pods")
    assert selector == "app=ec-11111111-222"


def test_pull_artifact_no_pod_raises(tmp_path, monkeypatch):
    """Deployment 无 Pod → FileNotFoundError（调用方转 404）。"""
    provider, _ = make_provider()

    class EmptyPodsCore:
        def list_namespaced_pod(self, namespace, label_selector=None, **kwargs):
            return SimpleNamespace(items=[])

    class EmptyClient:
        def CoreV1Api(self):
            return EmptyPodsCore()

    monkeypatch.setattr(provider, "_require_client", lambda: EmptyClient())
    with pytest.raises(FileNotFoundError, match="no pod found"):
        provider.pull_artifact(running_workspace(), "checkpoint.pt")


def test_pull_artifact_raises_on_kubectl_cp_failure(tmp_path, monkeypatch):
    """kubectl cp 非零 returncode → 上抛 RuntimeError。"""
    provider, _ = make_provider()

    def fake_run(args, *, check=True):
        return CompletedProcess(args=args, returncode=1, stdout="", stderr="Error from kubectl")

    monkeypatch.setattr(provider, "_run", fake_run)
    with pytest.raises(RuntimeError, match="kubectl cp failed"):
        provider.pull_artifact(running_workspace(), "checkpoint.pt")

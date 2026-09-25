"""K8s provider 模型层一致性：真 `kubernetes.client` 模型 + 假传输客户端。

为什么要这一档：离线单测的 V1* 模型是 SimpleNamespace 工厂（tests/k8s_fakes.py），
它接受**任意** kwargs —— 于是"字段名写错""塞了不存在的字段""序列化后根本
不符合 API schema"这类问题在 fake 层永远不会翻红（fake 自己定义了自己的行为）。
本档把 model_factory 换成真 SDK，只保留传输层是假的：产出的对象必须是 SDK 的
V1Deployment/V1Service/V1PersistentVolumeClaim，并且要能过 SDK 自己的序列化器
（sanitize_for_serialization，即 API server 实际收到的那份 JSON）。

明确边界（不冒充）：这里证明的是"请求体形状合法"，不是"集群接受并跑起来了"
—— 后者仍属 tests/test_k8s_integration.py 的 K8S_PHYSICAL_VALIDATION_PENDING。
"""

import json

import pytest
from k8s_fakes import make_fake_models
from kubernetes import client as sdk_client
from test_k8s_provider import (
    FakeAppsV1Api,
    FakeClientModule,
    make_reservation,
    make_settings,
    make_template,
    make_workspace,
)

from app.services.providers.k8s import KubernetesProvider


class RealReadAppsApi(FakeAppsV1Api):
    """read 返回**真** V1Deployment：轮换走的是"读现网 → 改 env → patch 回写"。

    读回来的若是 SimpleNamespace，写回的补丁体就含非 SDK 对象，
    sanitize_for_serialization 直接报错 —— 那正是本档要暴露的"形状不合法"。
    """

    def __init__(self, calls: list):
        self.calls = calls

    def read_namespaced_deployment(self, name, namespace, **kwargs):
        self.calls.append(("read_deployment", namespace, name))
        env = sdk_client.V1EnvVar(name="WORKSPACE_PASSWORD", value="old-secret")
        container = sdk_client.V1Container(name="workspace", image="img:1", env=[env])
        return sdk_client.V1Deployment(
            metadata=sdk_client.V1ObjectMeta(name=name, namespace=namespace),
            spec=sdk_client.V1DeploymentSpec(
                selector=sdk_client.V1LabelSelector(match_labels={"app": name}),
                template=sdk_client.V1PodTemplateSpec(
                    metadata=sdk_client.V1ObjectMeta(labels={"app": name}),
                    spec=sdk_client.V1PodSpec(containers=[container]),
                ),
            ),
        )

    def patch_namespaced_deployment(self, name, namespace, body, **kwargs):
        self.calls.append(("patch_deployment", namespace, name, body))
        return body


class RealModelClientModule(FakeClientModule):
    """传输层：Core 沿用通用 fake，Apps 换成返回真 SDK 模型的版本。"""

    def AppsV1Api(self):
        return RealReadAppsApi(self.calls)


def real_model_provider(**settings_overrides) -> tuple[KubernetesProvider, FakeClientModule]:
    """真模型 + 假传输：唯一区别是 model_factory 指向 kubernetes.client。"""
    fake = RealModelClientModule()
    provider = KubernetesProvider(
        make_settings(**settings_overrides),
        _client=fake,
        model_factory=lambda: sdk_client,
    )
    return provider, fake


def provisioned(fake: FakeClientModule) -> dict:
    """按 kind 取回 create_* 记录的 body。"""
    out: dict = {}
    for call in fake.calls:
        if call[0].startswith("create_"):
            out[call[0]] = call[2]
    return out


def wire(obj) -> dict:
    """用 SDK 自己的序列化器出"发往 API server 的 JSON"（边界对象，不是我们复刻）。"""
    from kubernetes.client import ApiClient

    return json.loads(json.dumps(ApiClient().sanitize_for_serialization(obj)))


@pytest.fixture
def provisioned_bodies():
    provider, fake = real_model_provider()
    result = provider.provision(
        make_workspace(), make_template(), tmp_path_placeholder(), reservation=make_reservation()
    )
    return provisioned(fake), result


def tmp_path_placeholder():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp(prefix="ec-k8s-model-"))


# ---------------------------------------------------------------------------
# 1. 产出的确实是 SDK 模型，而不是 fake 层的宽松对象
# ---------------------------------------------------------------------------


def test_provision_builds_real_sdk_objects(provisioned_bodies):
    bodies, result = provisioned_bodies
    assert isinstance(bodies["create_pvc"], sdk_client.V1PersistentVolumeClaim)
    assert isinstance(bodies["create_service"], sdk_client.V1Service)
    assert isinstance(bodies["create_deployment"], sdk_client.V1Deployment)
    assert result.container_name and result.password


def test_real_models_reject_unknown_fields_but_fake_layer_does_not():
    """判据能开火的证明：真模型对未知字段报错，fake 层照单全收。

    没有这一条，"换成真模型"可能只是换了个名字同样的宽松实现。
    """
    with pytest.raises(TypeError):
        sdk_client.V1PodSpec(containers=[], host_networkx=True)  # 故意写错字段名
    # 同一输入在 fake 层不报错 → 说明旧离线档确实看不见这类缺陷
    make_fake_models().V1PodSpec(containers=[], host_networkx=True)


# ---------------------------------------------------------------------------
# 2. 线上传输形状（API server 会校验的那些规则）
# ---------------------------------------------------------------------------


def test_deployment_wire_shape_passes_api_validation_rules(provisioned_bodies):
    bodies, _result = provisioned_bodies
    dep = wire(bodies["create_deployment"])

    # selector 必须被 Pod 标签包含 —— API server 直接拒绝不匹配
    selector = dep["spec"]["selector"]["matchLabels"]
    pod_labels = dep["spec"]["template"]["metadata"]["labels"]
    assert set(selector.items()) <= set(pod_labels.items()), f"selector 不被标签覆盖：{selector} vs {pod_labels}"

    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["image"], "容器镜像为空"
    assert container["resources"]["limits"]["nvidia.com/gpu"] == "1"
    # 扩展资源（nvidia.com/gpu）按 K8s 规则 requests 默认等于 limits，故不重复声明；
    # 这里钉住"只声明 limits"这一形状，避免有人改成 requests-only 导致调度不到 GPU 节点
    assert "nvidia.com/gpu" not in container["resources"].get("requests", {})
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["ACCEPT_EULA"] == "Y"
    assert env["WORKSPACE_PASSWORD"], "凭据未注入 Pod env"
    assert dep["spec"]["template"]["spec"]["nodeSelector"]["kubernetes.io/hostname"] == "gpu-node-01"

    # 安全边界以"序列化后确实不存在"为准，而不是 fake 的默认值
    pod_spec = dep["spec"]["template"]["spec"]
    assert "hostNetwork" not in json.dumps(dep), "hostNetwork 出现在请求体里"
    assert "hostPath" not in pod_spec["volumes"][0], f"不得挂 hostPath：{pod_spec['volumes']}"
    assert "securityContext" not in json.dumps(container)
    assert pod_spec["volumes"][0]["persistentVolumeClaim"]["claimName"] == dep["metadata"]["name"].replace(
        "ec-", "ec-pvc-", 1
    )


def test_service_and_pvc_wire_shapes(provisioned_bodies):
    bodies, _result = provisioned_bodies
    svc = wire(bodies["create_service"])
    pvc = wire(bodies["create_pvc"])

    assert svc["spec"]["ports"][0]["port"] == KubernetesProvider.IDE_PORT
    assert svc["spec"]["selector"]["app"] == svc["metadata"]["name"], "Service selector 必须指向本 Deployment"
    assert pvc["spec"]["resources"]["requests"]["storage"]
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    # 不指定 storageClassName → 由集群默认 StorageClass 接管（有意决策，见 provider 注释）
    assert pvc["spec"].get("storageClassName") in (None, "")


# ---------------------------------------------------------------------------
# 3. rotate_credentials：凭据轮换写出的补丁形状（flow-only，标注不清白）
# ---------------------------------------------------------------------------


def test_rotate_credentials_patches_real_deployment_model():
    """K8s 路径支持运行时轮换：写回的是真 V1Deployment，且新口令真的进了 env。

    注意：这仍是对假传输客户端的流程验证（"API 调用形状正确"），
    不等于"pod 真的滚动重启了" —— 那属于物理档。
    """
    provider, fake = real_model_provider()
    ws = make_workspace()
    ws.container_name = "ec-111111112222"
    assert provider.supports_credential_rotation is True

    ok = provider.rotate_credentials(ws, {"password": "rotated-secret-1"})
    assert ok is True
    assert [c[0] for c in fake.calls if c[0] == "read_deployment"], "未先读现网 Deployment"
    patches = [c for c in fake.calls if c[0] == "patch_deployment"]
    assert patches, f"未见 Deployment patch 调用：{[c[0] for c in fake.calls]}"
    patched = wire(patches[-1][-1])
    containers = patched["spec"]["template"]["spec"]["containers"]
    assert containers[0]["name"] == "workspace", f"补丁必须指名容器：{containers}"
    envs = {e["name"]: e["value"] for e in containers[0]["env"]}
    assert envs["WORKSPACE_PASSWORD"] == "rotated-secret-1", envs


def test_rotate_credentials_without_password_is_refused():
    provider, fake = real_model_provider()
    ws = make_workspace()
    ws.container_name = "ec-111111112222"
    assert provider.rotate_credentials(ws, {}) is False
    assert not [c for c in fake.calls if c[0] == "patch_deployment"], "无口令却写了 Deployment"

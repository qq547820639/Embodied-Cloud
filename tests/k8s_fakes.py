"""Kubernetes V1* 模型 fake layer（离线测试专用，零 Kubernetes SDK 依赖）。

offline/单元测试的语义：provider 使用注入的 client + 模型工厂，
**不隐式依赖真实 kubernetes Python 包**（否则"看似 offline 实则需要 SDK"）。
"""

from types import SimpleNamespace


def make_fake_models():
    """返回 fake kubernetes.client 命名空间（V1* 模型类为 SimpleNamespace 工厂）。

    每个"类"是一个接受任意 kwargs 的工厂，产出 SimpleNamespace 对象，
    支持嵌套结构（metadata/spec/template/containers/...）。
    """

    def _factory(defaults: dict | None = None):
        base = dict(defaults or {})
        return lambda **kw: SimpleNamespace(**{**base, **kw})

    return SimpleNamespace(
        V1ObjectMeta=_factory(),
        V1PersistentVolumeClaim=_factory({"spec": None}),
        V1PersistentVolumeClaimSpec=_factory({"storage_class_name": None}),
        V1ResourceRequirements=_factory({"requests": None, "limits": None}),
        V1Service=_factory({"metadata": None, "spec": None}),
        V1ServiceSpec=_factory({"ports": []}),
        V1ServicePort=_factory(),
        V1Deployment=_factory({"metadata": None, "spec": None}),
        V1DeploymentSpec=_factory({"selector": None, "template": None}),
        V1LabelSelector=_factory({"match_labels": {}}),
        V1PodTemplateSpec=_factory({"metadata": None, "spec": None}),
        V1PodSpec=_factory({"node_selector": None, "containers": [], "volumes": [], "host_network": None}),
        V1Container=_factory(
            {
                "name": "",
                "image": "",
                "env": [],
                "resources": None,
                "volume_mounts": [],
                "security_context": None,
            }
        ),
        V1EnvVar=_factory(),
        V1VolumeMount=_factory(),
        V1Volume=_factory({"persistent_volume_claim": None, "host_path": None}),
        V1PersistentVolumeClaimVolumeSource=_factory({"claim_name": ""}),
    )

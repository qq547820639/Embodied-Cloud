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


def make_tripwire_models():
    """安全哨兵 fake 模型层（SECURITY.md T3 测试用）。

    与 make_fake_models 同构，但当生产代码**显式设置**危险字段时立即抛错：
    - V1PodSpec.host_network（hostNetwork 逃生）
    - V1Container.privileged / security_context（特权/越权上下文）
    - V1Volume.host_path（host 文件系统挂载，docker.sock 类泄漏）

    用途：断言「生产代码未设置」不能靠检查 fake 默认值（近乎恒真）——
    哨兵把「设置了」变成必然失败的异常，测试才真正触达生产决策。
    """
    base = make_fake_models()

    def guard(name):
        orig = getattr(base, name)

        def factory(**kw):
            if name == "V1PodSpec" and kw.get("host_network"):
                raise AssertionError("SECURITY: hostNetwork must not be set by provision")
            if name == "V1Container":
                if kw.get("privileged"):
                    raise AssertionError("SECURITY: privileged must not be set by provision")
                if kw.get("security_context") is not None:
                    raise AssertionError("SECURITY: security_context must not be set by provision")
            if name == "V1Volume" and kw.get("host_path") is not None:
                raise AssertionError("SECURITY: hostPath volume must not be mounted by provision")
            return orig(**kw)

        return factory

    return SimpleNamespace(**{name: guard(name) for name in vars(base)})

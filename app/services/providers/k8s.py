"""Kubernetes provider: 将 workspace 部署为 K8s 的 Deployment + Service + PVC.

策略与 docker provider 对齐但面向多节点 (每个 workspace 一组 K8s 资源):
- Deployment 名 `ec-{workspace.id[:12]}`, Service 同名, PVC 名 `ec-pvc-{workspace.id[:12]}`;
- 资源 requests/limits 固定 cpu=4 / memory=16Gi; 不设置 privileged / docker.sock / hostNetwork (默认隔离);
- IDE 端口固定 18000, 由 Service 暴露; Ingress 路径 `/ide/{workspace.id[:12]}/` 由 deploy 层提供 (占位约定);
- kubernetes 依赖懒加载: 模块顶层不 import, 保证 mock 模式下 `from app.deps import provider` 不受影响.
"""

import logging
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...config import Settings
from ...models import Template, Workspace
from .base import ProvisionResult, ResourceReservation, RuntimeState

logger = logging.getLogger("embodiedcloud.providers.k8s")


class KubernetesProvider:
    name = "k8s"

    # 固定资源/端口常量（与 settings 无关）
    CPU = "4"
    MEMORY = "16Gi"
    STORAGE = "100Gi"
    IDE_PORT = 18000

    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[], Any] | None = None,
        _client: Any = None,
        model_factory: Callable[[], Any] | None = None,
    ) -> None:
        """初始化 provider.

        client_factory / _client 用于离线单测注入 fake kubernetes client:
        - 任一提供即视为离线模式, 跳过 kubeconfig 加载, health() 直接 OK;
        - 否则懒加载真实 kubernetes 客户端, 配置失败仅缓存错误状态 (health() 汇报, 不抛异常).

        model_factory: V1* 模型类命名空间的工厂（默认懒加载真实
        `kubernetes.client`）。离线/单测环境必须注入 fake model layer，
        **不允许 offline 测试隐式依赖真实 Kubernetes SDK 的模型类**——
        否则"看似 offline 实则需要安装 kubernetes 包"。
        """
        self.settings = settings
        self._client: Any = _client
        self._client_factory = client_factory
        self._model_factory = model_factory
        self._offline = _client is not None or client_factory is not None
        self._config_error = ""
        if self._offline:
            if self._client is None and self._client_factory is not None:
                self._client = self._client_factory()
        else:
            self._load_client()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _models(self) -> Any:
        """返回 V1* 模型类命名空间。

        - 注入 model_factory（离线测试）→ 使用 fake model layer，零 SDK 依赖
        - 默认 → 懒加载真实 kubernetes.client（真实集群路径）
        """
        if self._model_factory is not None:
            return self._model_factory()
        from kubernetes import client  # 懒加载：仅真实集群路径才需要 SDK

        return client

    def _load_client(self) -> None:
        """加载 kubeconfig / in-cluster 配置并实例化 client (方法内 import, 懒加载)."""
        from kubernetes import client, config

        try:
            if self.settings.k8s_in_cluster:
                config.load_incluster_config()
            else:
                config.load_kube_config()
        except Exception as exc:  # 配置失败仅缓存，由 health() 汇报
            self._config_error = f"kube config 加载失败: {exc}"
            return
        self._client = self._client_factory() if self._client_factory is not None else client

    def _require_client(self) -> Any:
        """返回可用 client; 未初始化时抛 RuntimeError (供 provision/stop/destroy 使用)."""
        if self._client is None:
            raise RuntimeError(self._config_error or "kubernetes client 未初始化（kube config 加载失败）")
        return self._client

    @staticmethod
    def _describe(exc: BaseException) -> str:
        """把 ApiException (或任意异常) 转成可读详情, 不依赖 kubernetes 顶层 import."""
        status = getattr(exc, "status", None)
        reason = getattr(exc, "reason", None)
        if status is not None:
            return f"kubernetes API {status} {reason or ''}: {exc}".strip()
        return str(exc) or exc.__class__.__name__

    @staticmethod
    def _deployment_name(workspace: Workspace) -> str:
        return f"ec-{workspace.id[:12]}"

    @staticmethod
    def _pvc_name(workspace: Workspace) -> str:
        return f"ec-pvc-{workspace.id[:12]}"

    # ------------------------------------------------------------------
    # 补偿清理（provision 中途失败时删除已创建资源；幂等，404 视为成功）
    # ------------------------------------------------------------------

    def _try_delete_service(self, core: Any, ns: str, name: str) -> None:
        try:
            core.delete_namespaced_service(name=name, namespace=ns)
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                raise RuntimeError(f"补偿清理 Service {name} 失败: {self._describe(exc)}") from exc

    def _try_delete_pvc(self, core: Any, ns: str, name: str) -> None:
        try:
            core.delete_namespaced_persistent_volume_claim(name=name, namespace=ns)
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                raise RuntimeError(f"补偿清理 PVC {name} 失败: {self._describe(exc)}") from exc

    # ------------------------------------------------------------------
    # WorkspaceProvider 接口
    # ------------------------------------------------------------------

    def health(self) -> tuple[bool, str]:
        """检查 kube config 是否加载成功且 API 可达; 任何异常都吞掉并返回 (False, 原因)."""
        if self._offline:
            return True, "kubernetes ready (fake client)"
        if self._client is None:
            return False, self._config_error or "kubernetes client 未初始化"
        try:
            self._client.CoreV1Api().list_namespace(limit=1)
        except Exception as exc:
            return False, f"无法连接 Kubernetes API: {exc}"
        return True, "kubernetes ready"

    def provision(
        self,
        workspace: Workspace,
        template: Template,
        workspace_dir: Path,
        reservation: ResourceReservation | None = None,
    ) -> ProvisionResult:
        if not self.settings.eula_accepted:
            raise RuntimeError("Set EMBODIEDCLOUD_EULA_ACCEPTED=true before launching NVIDIA Isaac containers.")
        # §10：真实模式禁止 reservation=None（scheduler 必须完成 node/capacity 决策）
        if reservation is None:
            raise RuntimeError("KubernetesProvider requires a ResourceReservation from GpuScheduler")

        client = self._models()  # offline 测试注入 fake model layer；真实路径懒加载 kubernetes.client

        api = self._require_client()
        core = api.CoreV1Api()
        apps = api.AppsV1Api()
        ns = self.settings.k8s_namespace
        deployment_name = self._deployment_name(workspace)
        pvc_name = self._pvc_name(workspace)
        # 镜像优先级：workspace.image（TemplateVersion 快照）→ template.image →
        # settings.workspace_image（模板镜像为真实来源，禁止 latest）
        image = workspace.image or template.image or self.settings.workspace_image
        password = secrets.token_urlsafe(16)
        labels = {"app": deployment_name, "embodiedcloud.workspace": workspace.id}

        # GPU 资源声明：完全来自 scheduler 的 reservation（唯一决策入口）。
        # Kubernetes 侧由 NVIDIA Device Plugin 负责具体 device 分配；
        # nodeSelector 来自 reservation.node_name（§10 明确字段，非字符串解析）
        gpu_count = str(reservation.gpu_count)
        node_selector: dict[str, str] = {}
        if reservation.node_name:
            node_selector = {"kubernetes.io/hostname": reservation.node_name}
        node_selector.update(dict(reservation.metadata.get("node_selector", {})))

        # 1) PVC：挂载到 /workspace/project，不指定 StorageClass（使用集群默认）
        pvc = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=pvc_name, namespace=ns),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                resources=client.V1ResourceRequirements(requests={"storage": self.STORAGE}),
            ),
        )
        try:
            core.create_namespaced_persistent_volume_claim(namespace=ns, body=pvc)
        except Exception as exc:
            raise RuntimeError(f"创建 PVC {pvc_name} 失败: {self._describe(exc)}") from exc

        # 2) Service：同名单节点服务，暴露 IDE 端口
        service = client.V1Service(
            metadata=client.V1ObjectMeta(name=deployment_name, namespace=ns, labels=labels),
            spec=client.V1ServiceSpec(
                selector={"app": deployment_name},
                ports=[client.V1ServicePort(name="ide", port=self.IDE_PORT, target_port=self.IDE_PORT)],
            ),
        )
        try:
            core.create_namespaced_service(namespace=ns, body=service)
        except Exception as exc:
            # 补偿：PVC 已创建 → 删除，避免孤儿 volume
            self._try_delete_pvc(core, ns, pvc_name)
            raise RuntimeError(f"创建 Service {deployment_name} 失败: {self._describe(exc)}") from exc

        # 3) Deployment：不设置 privileged / docker.sock / hostNetwork（默认隔离）
        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(name=deployment_name, namespace=ns, labels=labels),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels={"app": deployment_name}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={"app": deployment_name}),
                    spec=client.V1PodSpec(
                        node_selector=node_selector or None,
                        containers=[
                            client.V1Container(
                                name="workspace",
                                image=image,
                                env=[
                                    client.V1EnvVar(name="ACCEPT_EULA", value="Y"),
                                    client.V1EnvVar(name="WORKSPACE_PASSWORD", value=password),
                                    client.V1EnvVar(name="IDE_PORT", value=str(self.IDE_PORT)),
                                    client.V1EnvVar(
                                        name="LIVESTREAM",
                                        value="1" if template.requires_streaming else "0",
                                    ),
                                    client.V1EnvVar(name="PUBLIC_IP", value=self.settings.host_public_ip),
                                ],
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": self.CPU, "memory": self.MEMORY},
                                    limits={
                                        "cpu": self.CPU,
                                        "memory": self.MEMORY,
                                        # GPU 绑定严格来自 scheduler reservation
                                        "nvidia.com/gpu": gpu_count,
                                    },
                                ),
                                volume_mounts=[
                                    client.V1VolumeMount(name="project", mount_path="/workspace/project")
                                ],
                            )
                        ],
                        volumes=[
                            client.V1Volume(
                                name="project",
                                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                                    claim_name=pvc_name
                                ),
                            )
                        ],
                    ),
                ),
            ),
        )
        try:
            apps.create_namespaced_deployment(namespace=ns, body=deployment)
        except Exception as exc:
            # 补偿：清理已创建的 Service + PVC，避免孤儿资源
            self._try_delete_service(core, ns, deployment_name)
            self._try_delete_pvc(core, ns, pvc_name)
            raise RuntimeError(f"创建 Deployment {deployment_name} 失败: {self._describe(exc)}") from exc

        return ProvisionResult(
            # K8s 下由 Service/Ingress 暴露端口，host 端口字段无需占用
            ide_port=None,
            signal_port=None,
            media_port=None,
            ide_url=f"http://{self.settings.host_public_ip}/ide/{workspace.id[:12]}/",
            password=password,
            container_name=deployment_name,
        )

    def start(self, workspace: Workspace) -> None:
        """把 Deployment 副本扩回 1（从 STOPPED 恢复）。"""
        if not workspace.container_name:
            return
        api = self._require_client()
        try:
            api.AppsV1Api().patch_namespaced_deployment_scale(
                name=workspace.container_name,
                namespace=self.settings.k8s_namespace,
                body={"spec": {"replicas": 1}},
            )
        except Exception as exc:
            raise RuntimeError(f"启动 deployment {workspace.container_name} 失败: {self._describe(exc)}") from exc

    def stop(self, workspace: Workspace) -> None:
        """把 Deployment 副本缩到 0 (保留资源定义, 便于再次启动)."""
        if not workspace.container_name:
            return
        api = self._require_client()
        try:
            api.AppsV1Api().patch_namespaced_deployment_scale(
                name=workspace.container_name,
                namespace=self.settings.k8s_namespace,
                body={"spec": {"replicas": 0}},
            )
        except Exception as exc:
            raise RuntimeError(f"停止 deployment {workspace.container_name} 失败: {self._describe(exc)}") from exc

    def destroy(self, workspace: Workspace) -> None:
        """删除 Deployment / Service / PVC (先删 Deployment 释放 PVC 挂载; 404 视为已删除)."""
        api = self._require_client()
        ns = self.settings.k8s_namespace
        deployment_name = workspace.container_name or self._deployment_name(workspace)
        pvc_name = self._pvc_name(workspace)
        apps = api.AppsV1Api()
        core = api.CoreV1Api()

        try:
            apps.delete_namespaced_deployment(name=deployment_name, namespace=ns)
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                raise RuntimeError(f"删除 deployment {deployment_name} 失败: {self._describe(exc)}") from exc
        try:
            core.delete_namespaced_service(name=deployment_name, namespace=ns)
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                raise RuntimeError(f"删除 service {deployment_name} 失败: {self._describe(exc)}") from exc
        try:
            core.delete_namespaced_persistent_volume_claim(name=pvc_name, namespace=ns)
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                raise RuntimeError(f"删除 PVC {pvc_name} 失败: {self._describe(exc)}") from exc

    def inspect(self, workspace: Workspace) -> dict:
        """读取 Deployment 状态 (replicas/ready/conditions); 失败时返回错误信息."""
        deployment_name = workspace.container_name or self._deployment_name(workspace)
        try:
            api = self._require_client()
            status = api.AppsV1Api().read_namespaced_deployment_status(
                name=deployment_name, namespace=self.settings.k8s_namespace
            ).status
        except Exception as exc:
            return {"error": self._describe(exc)}
        conditions = [
            {
                "type": cond.type,
                "status": cond.status,
                "reason": cond.reason,
                "message": cond.message,
            }
            for cond in (getattr(status, "conditions", None) or [])
        ]
        return {
            "replicas": getattr(status, "replicas", None),
            "ready_replicas": getattr(status, "ready_replicas", None),
            "available_replicas": getattr(status, "available_replicas", None),
            "conditions": conditions,
        }

    def logs(self, workspace: Workspace, tail: int = 200) -> str:
        """读取 Pod 日志; 无 Pod 或出错时返回空字符串."""
        deployment_name = workspace.container_name or self._deployment_name(workspace)
        try:
            api = self._require_client()
            core = api.CoreV1Api()
            pods = core.list_namespaced_pod(
                namespace=self.settings.k8s_namespace, label_selector=f"app={deployment_name}"
            )
            if not pods.items:
                return ""
            return core.read_namespaced_pod_log(
                name=pods.items[0].metadata.name,
                namespace=self.settings.k8s_namespace,
                tail_lines=tail,
            )
        except Exception:
            return ""

    def wait_ready(self, workspace, template, timeout_seconds: int = 120) -> bool:
        """§9 readiness：Deployment available_replicas>=1 + Pod Ready + Service 端点。"""
        import time as _time

        deployment_name = workspace.container_name or self._deployment_name(workspace)
        deadline = _time.monotonic() + max(1, timeout_seconds)
        try:
            api = self._require_client()
            apps = api.AppsV1Api()
            core = api.CoreV1Api()
        except Exception:
            return False
        while _time.monotonic() < deadline:
            try:
                status = apps.read_namespaced_deployment_status(
                    name=deployment_name, namespace=self.settings.k8s_namespace
                ).status
                if int(getattr(status, "available_replicas", None) or 0) < 1:
                    _time.sleep(2)
                    continue
                # Pod Ready（所有容器 ready）
                pods = core.list_namespaced_pod(
                    namespace=self.settings.k8s_namespace,
                    label_selector=f"app={deployment_name}",
                )
                if not pods.items:
                    _time.sleep(2)
                    continue
                pod = pods.items[0]
                if getattr(pod.status, "phase", "") != "Running":
                    _time.sleep(2)
                    continue
                conditions = {c.type: c.status for c in (getattr(pod.status, "conditions", None) or [])}
                if conditions.get("Ready") != "True":
                    _time.sleep(2)
                    continue
                # Service 端点健康（endpoints 非空）
                eps = core.read_namespaced_endpoints(
                    name=deployment_name, namespace=self.settings.k8s_namespace
                )
                subsets = getattr(eps, "subsets", None) or []
                if subsets:
                    return True
                _time.sleep(2)
            except Exception:
                _time.sleep(2)
        return False

    @property
    def supports_credential_rotation(self) -> bool:
        # K8s 通过 patch Deployment env → 滚动重启实现轮换
        return True

    def rotate_credentials(self, workspace: Workspace, credentials: dict) -> bool:
        """轮换 runtime 凭据：patch Deployment 的 WORKSPACE_PASSWORD env → 滚动重启。

        新 env 注入后 Pod 重建，新密码生效（旧密码不再可登录）。
        失败返回 False（调用方不得交付）。
        """
        password = credentials.get("password")
        if not password:
            return False
        deployment_name = workspace.container_name or self._deployment_name(workspace)
        try:
            api = self._require_client()
            apps = api.AppsV1Api()
            dep = apps.read_namespaced_deployment(
                name=deployment_name, namespace=self.settings.k8s_namespace
            )
            envs = dep.spec.template.spec.containers[0].env
            replaced = False
            for env in envs:
                if env.name == "WORKSPACE_PASSWORD":
                    env.value = password
                    replaced = True
            if not replaced:
                envs.append(type(envs[0])(name="WORKSPACE_PASSWORD", value=password))
            body = {
                "spec": {
                    "template": {
                        "spec": {"containers": [{"name": dep.spec.template.spec.containers[0].name, "env": envs}]}
                    }
                }
            }
            apps.patch_namespaced_deployment(
                name=deployment_name, namespace=self.settings.k8s_namespace, body=body
            )
            return True
        except Exception as exc:
            logger.warning("rotate_credentials failed for %s: %s", deployment_name, self._describe(exc))
            return False

    def reconcile(self, workspace: Workspace) -> RuntimeState:
        """判定 Deployment runtime 存活：available_replicas>=1 → ALIVE；404/0 副本 → MISSING；其他 → UNKNOWN。"""
        deployment_name = workspace.container_name or self._deployment_name(workspace)
        try:
            api = self._require_client()
            status = api.AppsV1Api().read_namespaced_deployment_status(
                name=deployment_name, namespace=self.settings.k8s_namespace
            ).status
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return RuntimeState.MISSING
            return RuntimeState.UNKNOWN
        available = getattr(status, "available_replicas", None) or 0
        if int(available) >= 1:
            return RuntimeState.ALIVE
        return RuntimeState.MISSING

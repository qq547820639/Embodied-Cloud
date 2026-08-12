"""Kubernetes provider: 将 workspace 部署为 K8s 的 Deployment + Service + PVC.

策略与 docker provider 对齐但面向多节点 (每个 workspace 一组 K8s 资源):
- Deployment 名 `ec-{workspace.id[:12]}`, Service 同名, PVC 名 `ec-pvc-{workspace.id[:12]}`;
- 资源 requests/limits 固定 cpu=4 / memory=16Gi; 不设置 privileged / docker.sock / hostNetwork (默认隔离);
- IDE 端口固定 18000, 由 Service 暴露; Ingress 路径 `/ide/{workspace.id[:12]}/` 由 deploy 层提供 (占位约定);
- kubernetes 依赖懒加载: 模块顶层不 import, 保证 mock 模式下 `from app.deps import provider` 不受影响.
"""

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...config import Settings
from ...models import Template, Workspace
from .base import ProvisionResult


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
    ) -> None:
        """初始化 provider.

        client_factory / _client 用于离线单测注入 fake kubernetes client:
        - 任一提供即视为离线模式, 跳过 kubeconfig 加载, health() 直接 OK;
        - 否则懒加载真实 kubernetes 客户端, 配置失败仅缓存错误状态 (health() 汇报, 不抛异常).
        """
        self.settings = settings
        self._client: Any = _client
        self._client_factory = client_factory
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

    def provision(self, workspace: Workspace, template: Template, workspace_dir: Path) -> ProvisionResult:
        if not self.settings.eula_accepted:
            raise RuntimeError("Set EMBODIEDCLOUD_EULA_ACCEPTED=true before launching NVIDIA Isaac containers.")

        from kubernetes import client  # 懒加载：仅使用 V1* 模型类构造对象

        api = self._require_client()
        core = api.CoreV1Api()
        apps = api.AppsV1Api()
        ns = self.settings.k8s_namespace
        deployment_name = self._deployment_name(workspace)
        pvc_name = self._pvc_name(workspace)
        # 镜像优先级：workspace.image（预留字段）→ template.image → settings.workspace_image
        image = getattr(workspace, "image", None) or template.image or self.settings.workspace_image
        password = secrets.token_urlsafe(16)
        labels = {"app": deployment_name, "embodiedcloud.workspace": workspace.id}

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
                                    limits={"cpu": self.CPU, "memory": self.MEMORY},
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
            raise RuntimeError(f"创建 Deployment {deployment_name} 失败: {self._describe(exc)}") from exc

        return ProvisionResult(
            gpu_index=workspace.gpu_index,
            gpu_name=workspace.gpu_name,
            # K8s 下由 Service/Ingress 暴露端口，host 端口字段无需占用
            ide_port=None,
            signal_port=None,
            media_port=None,
            ide_url=f"http://{self.settings.host_public_ip}/ide/{workspace.id[:12]}/",
            password=password,
            container_name=deployment_name,
        )

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

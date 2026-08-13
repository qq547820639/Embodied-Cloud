from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from ...models import Template, Workspace


class RuntimeState(StrEnum):
    """Provider.inspect/reconcile 对 runtime 存活性的判定结果。

    - ALIVE: runtime 进程/容器/Pod 存在且健康
    - MISSING: runtime 不存在（容器被删、Pod 消失、进程退出）
    - UNKNOWN: 无法判定（provider 无真实 runtime，如 mock）
    """

    ALIVE = "alive"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ResourceReservation:
    """GpuScheduler 产出的唯一资源 reservation —— ONE RESOURCE = ONE SOURCE OF TRUTH。

    Provider 只执行 reservation 描述的绑定，禁止自行选择 GPU（禁止自带 allocator）。
    node_name（§10）：K8s 路径的明确 node identity（不依赖隐藏字符串解析
    host_id 约定）；Docker 单机为 host 名。
    """

    host_id: str
    gpu_id: str
    gpu_uuid: str
    gpu_index: int
    gpu_count: int = 1
    memory_mb: int = 0
    node_name: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class ProvisionResult:
    ide_port: int | None = None
    signal_port: int | None = None
    media_port: int | None = None
    ide_url: str | None = None
    stream_hint: str | None = None
    password: str | None = None
    container_name: str | None = None


class WorkspaceProvider(Protocol):
    """统一 runtime 契约：Mock / Docker / Kubernetes 必须全部实现。

    - provision 接受 ResourceReservation（唯一 GPU 决策来源），不得自行分配 GPU
    - start/stop 用于运行段生命周期（runtime 语义需要时实现；mock 为 no-op）
    - inspect 返回 runtime 实况（dict）；logs 返回日志；reconcile 判定存活
    """

    name: str

    def health(self) -> tuple[bool, str]: ...
    def provision(
        self,
        workspace: Workspace,
        template: Template,
        workspace_dir: Path,
        reservation: ResourceReservation | None = None,
    ) -> ProvisionResult: ...
    def start(self, workspace: Workspace) -> None: ...
    def stop(self, workspace: Workspace) -> None: ...
    def destroy(self, workspace: Workspace) -> None: ...
    def inspect(self, workspace: Workspace) -> dict: ...
    def logs(self, workspace: Workspace, tail: int = 200) -> str: ...
    def reconcile(self, workspace: Workspace) -> RuntimeState: ...
    def pull_artifact(self, workspace: Workspace, source_path: str) -> Path:
        """把 workspace runtime 内的文件拉到控制面本地临时路径并返回。

        契约：
        - source_path 是 workspace 运行时内的相对路径（相对其工作目录/挂载点）；
        - 返回控制面本地**临时**文件的 Path（内容为 source_path 指向文件的拷贝）；
        - 调用方负责在消费完毕后清理该临时文件（用 try/finally 保证不泄漏）；
        - 源文件不存在 / 拉取失败：上抛异常（如 RuntimeError / FileNotFoundError），
          由调用方（DeploymentService.create_artifact）转换为 404/500 语义。
        """
    def wait_ready(
        self,
        workspace: Workspace,
        template: Template,
        timeout_seconds: int = 120,
    ) -> bool:
        """§9 typed readiness：runtime 存在 + 健康 + IDE TCP/HTTP 可达 +
        TemplateVersion.healthcheck 真实执行。

        返回 False = 未就绪（调用方不得置 RUNNING，应 rollback → FAILED）。
        """

    def rotate_credentials(self, workspace: Workspace, credentials: dict) -> bool:
        """轮换 runtime 凭据（§7，warm pool claim 用）。

        - 返回 True：新凭据已生效（runtime 不再接受旧凭据）
        - 返回 False：不支持/失败 —— 调用方**不得**把 workspace 交付用户，
          应 DRAINING/FAILED 并 fallback 正常 provision
        """

    @property
    def supports_credential_rotation(self) -> bool:
        """§7：warm pool 能力检测 —— 不支持运行时凭据轮换的 provider
        （如 Docker env 不可变）默认禁用 warm pool。"""

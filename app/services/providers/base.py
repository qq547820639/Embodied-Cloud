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
    """

    host_id: str
    gpu_id: str
    gpu_uuid: str
    gpu_index: int
    gpu_count: int = 1
    memory_mb: int = 0
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

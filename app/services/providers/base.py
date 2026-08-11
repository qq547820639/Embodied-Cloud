from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ...models import Template, Workspace


@dataclass
class ProvisionResult:
    gpu_index: int | None = None
    gpu_name: str | None = None
    ide_port: int | None = None
    signal_port: int | None = None
    media_port: int | None = None
    ide_url: str | None = None
    stream_hint: str | None = None
    password: str | None = None
    container_name: str | None = None


class WorkspaceProvider(Protocol):
    name: str

    def health(self) -> tuple[bool, str]: ...
    def provision(self, workspace: Workspace, template: Template, workspace_dir: Path) -> ProvisionResult: ...
    def stop(self, workspace: Workspace) -> None: ...
    def destroy(self, workspace: Workspace) -> None: ...

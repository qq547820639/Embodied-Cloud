import secrets
from pathlib import Path

from ...models import Template, Workspace
from .base import ProvisionResult, ResourceReservation, RuntimeState


class MockProvider:
    name = "mock"

    def __init__(self, public_base_url: str):
        self.public_base_url = public_base_url.rstrip("/")

    def health(self) -> tuple[bool, str]:
        return True, "mock provider ready"

    def provision(
        self,
        workspace: Workspace,
        template: Template,
        workspace_dir: Path,
        reservation: ResourceReservation | None = None,
    ) -> ProvisionResult:
        password = secrets.token_urlsafe(12)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return ProvisionResult(
            ide_url=f"{self.public_base_url}/demo-workspace/{workspace.id}",
            stream_hint=(
                f"{self.public_base_url}/demo-workspace/{workspace.id}?view=stream"
                if template.requires_streaming else None
            ),
            password=password,
            container_name=f"mock-{workspace.id[:8]}",
        )

    def start(self, workspace: Workspace) -> None:
        return None

    def stop(self, workspace: Workspace) -> None:
        return None

    def destroy(self, workspace: Workspace) -> None:
        return None

    def inspect(self, workspace: Workspace) -> dict:
        # mock 无真实 runtime；返回会话中可见的静态信息
        return {"provider": self.name, "runtime": "mock", "container_name": workspace.container_name}

    def logs(self, workspace: Workspace, tail: int = 200) -> str:
        return ""

    def reconcile(self, workspace: Workspace) -> RuntimeState:
        # mock runtime 无法判定存活（没有可观测的真实进程）；由上层策略处理
        return RuntimeState.UNKNOWN

    def wait_ready(self, workspace, template, timeout_seconds: int = 120) -> bool:
        # mock 无真实 runtime；视为就绪
        return True

    @property
    def supports_credential_rotation(self) -> bool:
        return True

    def rotate_credentials(self, workspace: Workspace, credentials: dict) -> bool:
        # mock 无真实 runtime；凭据轮换视为成功（仅用于 warm pool claim 路径验证）
        return True

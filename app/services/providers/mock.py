import secrets
from pathlib import Path

from .base import ProvisionResult
from ...models import Template, Workspace


class MockProvider:
    name = "mock"

    def __init__(self, public_base_url: str):
        self.public_base_url = public_base_url.rstrip("/")

    def health(self) -> tuple[bool, str]:
        return True, "mock provider ready"

    def provision(self, workspace: Workspace, template: Template, workspace_dir: Path) -> ProvisionResult:
        password = secrets.token_urlsafe(12)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return ProvisionResult(
            gpu_index=0,
            gpu_name="Mock GPU / no real compute",
            ide_url=f"{self.public_base_url}/demo-workspace/{workspace.id}",
            stream_hint=(
                f"{self.public_base_url}/demo-workspace/{workspace.id}?view=stream"
                if template.requires_streaming else None
            ),
            password=password,
            container_name=f"mock-{workspace.id[:8]}",
        )

    def stop(self, workspace: Workspace) -> None:
        return None

    def destroy(self, workspace: Workspace) -> None:
        return None

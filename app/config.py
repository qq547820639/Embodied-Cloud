from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="EMBODIEDCLOUD_",
        case_sensitive=False,
        extra="ignore",
    )

    provider: str = "mock"
    database_url: str = "sqlite:///./embodiedcloud.db"
    public_base_url: str = "http://127.0.0.1:8000"
    bind_host: str = "0.0.0.0"
    bind_port: int = 8000

    workspace_root: Path = Path("/tmp/embodiedcloud-workspaces")
    workspace_image: str = "embodiedcloud/isaaclab-workspace:0.1.0"
    host_public_ip: str = "127.0.0.1"

    ide_port_start: int = 18000
    ide_port_end: int = 18999

    eula_accepted: bool = False
    privacy_consent: bool = False
    default_idle_timeout_minutes: int = 60

    def ensure_dirs(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)

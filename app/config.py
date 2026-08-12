from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="EMBODIEDCLOUD_",
        case_sensitive=False,
        extra="ignore",
    )

    # --- runtime ---
    provider: str = "mock"
    database_url: str = "sqlite:///./embodiedcloud.db"
    public_base_url: str = "http://127.0.0.1:8000"
    bind_host: str = "0.0.0.0"  # noqa: S104 服务默认监听所有接口，生产经 TLS 网关收敛
    bind_port: int = 8000

    # 开发便利：true 时启动自动 create_all（生产 compose 应显式关闭并走 alembic）
    auto_create_tables: bool = True

    # --- workspace ---
    # S108: /tmp 为本地开发默认值；生产必须显式配置 EMBODIEDCLOUD_WORKSPACE_ROOT。
    workspace_root: Path = Path("/tmp/embodiedcloud-workspaces")  # noqa: S108
    workspace_image: str = "embodiedcloud/isaaclab-workspace:0.1.0"
    host_public_ip: str = "127.0.0.1"

    ide_port_start: int = 18000
    ide_port_end: int = 18999

    eula_accepted: bool = False
    privacy_consent: bool = False
    default_idle_timeout_minutes: int = 60

    # --- billing（§12）---
    # 启动预授权（生产开启）：余额必须 ≥ minimum_launch_minutes × 60 credits 才能启动
    billing_enforce_preauthorization: bool = False
    billing_minimum_launch_minutes: int = 5

    # --- auth ---
    session_ttl_hours: int = 168  # 7 天
    password_pepper: str = ""  # 生产建议配置；未配置时退化为空盐前缀
    # workspace 凭据加密密钥（§20）：未配置时使用开发默认密钥（仅本地/mock）
    workspace_credential_key: str = ""

    # --- observability ---
    log_json: bool = False
    log_level: str = "INFO"

    # --- warm pool ---
    warm_pool_enabled: bool = False
    warm_pool_size: int = 1  # 每启用模板的目标 warm workspace 数

    # --- k8s provider ---
    k8s_namespace: str = "embodiedcloud"
    k8s_in_cluster: bool = False  # 默认使用 kubeconfig
    # K8s capacity reservation 的 GPU 显存粒度（MB）。K8s 不暴露 per-device
    # memory 给普通 resource request；精确显存由 NVIDIA Device Plugin 负责。
    k8s_gpu_memory_mb: int = 24576

    def ensure_dirs(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)

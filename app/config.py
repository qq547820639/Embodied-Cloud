from pathlib import Path
from typing import Literal

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
    # 预留：当前未启用（缺 runtime 活动信号，无法区分「空闲」与「长训练」），
    # 后续接入 IDE/流连接活动检测后生效。
    default_idle_timeout_minutes: int = 60
    # §9：provision readiness 等待超时（秒）
    provision_ready_timeout_seconds: int = 120

    # --- 对象存储（§21）---
    # local：产物落 workspace_root（开发/单机）；s3：S3 兼容对象存储（生产，
    # 控制面不再直接读每个 Workspace PVC）。backend=s3 而凭据不全时启动即
    # BLOCKED_EXTERNAL_DEPENDENCY —— 不静默回退 local，那会把产物写进控制面
    # 文件系统而账目仍记 store_name=s3。
    artifact_backend: Literal["local", "s3"] = "local"
    artifact_s3_bucket: str = ""
    artifact_s3_endpoint_url: str = ""
    artifact_s3_access_key_id: str = ""
    artifact_s3_secret_access_key: str = ""

    # --- billing（§12）---
    # 启动预授权：默认**开启**（N-72 定档）。余额必须 ≥ minimum_launch_minutes × 60
    # credits 才能启动，且开启时会真的圈住这笔额度（CreditHold），结算转正、余量退回。
    # 定档依据（本机打开读过 Vast.ai 计费文档）：新算力需要先有额度
    # 「requires pre-payment of credits for GPU rentals」，而运行中的实例在余额耗尽时被
    # 「stopped automatically」并允许一段「balance may go negative」的宽限 —— 正好是
    # 「启动前圈额度 + 运行中由配额 monitor 兜」这两半。改 False 是演示档（显式关掉即可）。
    billing_enforce_preauthorization: bool = True
    billing_minimum_launch_minutes: int = 5
    # 注册即发的体验额度（个人池，`signup:<user_id>` 幂等键）。0 = 关掉发放：
    # 此时新账户因为不够最低启动额度会被 402 拒，这是上面那档默认的预期行为，不是回归。
    billing_signup_credits: int = 300
    # pending hold 的最长存活时间：控制面在 hold 与结算之间崩溃时，扫描器按此回收
    # （泄漏方向是"少报可用额"，不会多扣钱）
    billing_hold_ttl_minutes: int = 60

    # --- edge agent（§25）---
    # `online` 必须由最近一次心跳背书：超过这个秒数没心跳就是 `offline`。
    # 阈值方向有成熟先例：AWS IoT 对 MQTT 连接的判据是「IoT core will wait 1.5x of the
    # configured keep-alive time before the client is determined to be disconnected」
    # （device-connectivity-status 页，本机打开读过）——判死必须大于 1× 客户端周期以留去抖。
    # 这里取 18×：设备端每 5 s 一轮（`edge_agent/agent.py:93` 与 `__main__.py:49` 的默认值），
    # 而我们这条通路是 HTTP 轮询、没有连接级存活信号可感知，单次请求卡住就可能吃掉一整轮。
    edge_agent_offline_after_seconds: int = 90

    # --- auth ---
    session_ttl_hours: int = 168  # 7 天
    # 密码 pepper：生产（provider != mock）**必须**显式配置，否则 fail-closed 拒绝启动
    # （见 security.validate_credential_configuration）；mock/本地开发可留空。
    password_pepper: str = ""
    # workspace 凭据加密密钥（§20）：未配置时使用开发默认密钥（仅本地/mock）
    workspace_credential_key: str = ""

    # --- observability ---
    log_json: bool = False
    log_level: str = "INFO"

    # --- warm pool ---
    warm_pool_enabled: bool = False
    warm_pool_size: int = 1  # 每启用模板的目标 warm workspace 数
    # 给交互式启动预留的卡数：池子补位时不许把舰队吃干（0 = 不预留，与改造前行为一致）。
    # 理由与实测读数见 docs/SUPPLY_CHAIN.md 的 warm pool 那一格 / tests/test_warmpool.py 容量段。
    warm_pool_reserve_slots: int = 0

    # --- k8s provider ---
    k8s_namespace: str = "embodiedcloud"
    k8s_in_cluster: bool = False  # 默认使用 kubeconfig
    # kubeconfig 路径；留空 = 走 SDK 默认解析（$KUBECONFIG 或 ~/.kube/config）。
    # 需要显式指定是因为 SDK 在 import 期就把环境变量固化成常量（改环境不生效）。
    k8s_kubeconfig: str = ""
    # K8s capacity reservation 的 GPU 显存粒度（MB）。K8s 不暴露 per-device
    # memory 给普通 resource request；精确显存由 NVIDIA Device Plugin 负责。
    k8s_gpu_memory_mb: int = 24576

    def ensure_dirs(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)

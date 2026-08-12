from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class WorkspaceStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    DELETED = "deleted"


class RuntimeKind(StrEnum):
    MOCK = "mock"
    ISAACLAB = "isaaclab"


class GpuStatus(StrEnum):
    AVAILABLE = "available"
    ALLOCATED = "allocated"
    UNHEALTHY = "unhealthy"
    DRAINING = "draining"


class LedgerType(StrEnum):
    RECHARGE = "recharge"
    USAGE = "usage"
    PROMOTION = "promotion"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"


class StreamingStatus(StrEnum):
    STARTING = "starting"
    READY = "ready"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    FAILED = "failed"


class Role(StrEnum):
    USER = "user"
    ADMIN = "admin"
    ORG_ADMIN = "org_admin"
    INSTRUCTOR = "instructor"
    STUDENT = "student"


class DeploymentStatus(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    VERIFIED = "verified"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class AgentStatus(StrEnum):
    REGISTERED = "registered"
    ONLINE = "online"
    OFFLINE = "offline"


class OperationType(StrEnum):
    PROVISION = "provision"
    START = "start"
    STOP = "stop"
    DESTROY = "destroy"
    RECONCILE = "reconcile"


class OperationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WarmPoolState(StrEnum):
    """Warm pool 真实状态机（§18）：READY 的 runtime 没有用户归属/用户数据。"""

    PREWARMING = "prewarming"
    READY = "ready"
    CLAIMING = "claiming"
    CLAIMED = "claimed"
    DRAINING = "draining"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Identity / Auth
# ---------------------------------------------------------------------------


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", use_alter=True, name="fk_organizations_owner"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=Role.USER.value)
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


# ---------------------------------------------------------------------------
# Template Registry
# ---------------------------------------------------------------------------


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="0.1.0")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime: Mapped[str] = mapped_column(String(32), nullable=False, default=RuntimeKind.ISAACLAB.value)
    # 版本锁定的镜像，禁止 latest
    image: Mapped[str] = mapped_column(String(255), nullable=True)
    # GPU 需求（VRAM GB）
    gpu_requirement_gb: Mapped[int] = mapped_column(Integer, default=16, nullable=False)
    # repo/asset（例如 IsaacLab checkout 路径或 asset URL）
    repo_asset: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 启动命令（entrypoint）
    entrypoint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 产出物（outputs: checkpoints/logs/datasets 列表）
    outputs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    requires_streaming: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # healthcheck（JSON：command/interval 等）
    healthcheck: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 自由 metadata
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    launch_command: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recommended_vram_gb: Mapped[int] = mapped_column(Integer, default=16, nullable=False)
    estimated_hourly_cost_cny: Mapped[float] = mapped_column(Float, default=6.0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class TemplateVersion(Base):
    """Template 的不可变版本（identity 与版本分离，§15）。

    - UNIQUE(template_id, version)
    - 已发布（released=True）版本禁止 mutable update：修改模板必须创建新版本
    - 记录 image/image_digest/entrypoint/gpu/streaming/outputs/healthcheck/
      dependency metadata/source revision
    - Workspace 必须引用具体 TemplateVersion（workspaces.template_version_id）
    """

    __tablename__ = "template_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    template_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("templates.id"), nullable=False, index=True
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    entrypoint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    gpu_requirement_gb: Mapped[int] = mapped_column(Integer, default=16, nullable=False)
    requires_streaming: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    outputs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    healthcheck: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    source_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    released: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_template_version"),
    )


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 具体引用的不可变 TemplateVersion（runtime image 的单一事实来源）
    template_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # 启动时快照的 runtime image（来自 TemplateVersion；provider 据此启动容器）
    image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WorkspaceStatus.CREATED.value
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    gpu_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gpu_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    gpu_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    ide_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signal_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ide_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    stream_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    password: Mapped[str | None] = mapped_column(String(128), nullable=True)
    container_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # soft delete tombstone：destroy 后行保留（billing/audit/deployment history/安全调查）
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    # warm pool 状态（§18）：None = 普通 workspace；否则为池状态机
    warm_pool_state: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    accumulated_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# ---------------------------------------------------------------------------
# GPU inventory / scheduling
# ---------------------------------------------------------------------------


class GpuHost(Base):
    __tablename__ = "gpu_hosts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="docker")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="online")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Gpu(Base):
    __tablename__ = "gpus"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    gpu_uuid: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    host_id: Mapped[str] = mapped_column(String(36), ForeignKey("gpu_hosts.id"), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    memory_total: Mapped[int] = mapped_column(Integer, nullable=False)
    # host 上设备索引（Docker provider 用 --gpus device=<index>）
    gpu_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=GpuStatus.AVAILABLE.value)
    workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        # 一个 workspace 至多绑定一张 GPU；同时防重复分配（唯一约束兜底）
        UniqueConstraint("workspace_id", name="uq_gpus_workspace"),
        Index("ix_gpus_status", "status"),
    )


class GpuAllocation(Base):
    __tablename__ = "gpu_allocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    gpu_id: Mapped[str] = mapped_column(String(36), ForeignKey("gpus.id"), nullable=False, unique=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), nullable=False, unique=True, index=True
    )
    host_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Credit ledger（不可变账本）
# ---------------------------------------------------------------------------


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 正负金额（单位：credits；1 credit = 1 GPU 秒 或按费率换算，见 ledger 服务）
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CREDITS")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gpu_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (Index("ix_ledger_user_created", "user_id", "created_at"),)


# ---------------------------------------------------------------------------
# Durable workspace operations（DB-backed worker，替换 threading.Thread 裸线程）
# ---------------------------------------------------------------------------


class WorkspaceOperation(Base):
    """一次 workspace 生命周期操作的持久化记录。

    - PENDING → RUNNING（带 lease）→ SUCCEEDED / FAILED
    - RUNNING lease 过期可由 worker 重新 claim（崩溃恢复）
    - 同一 workspace 同一时刻至多一个 active（PENDING/RUNNING/RETRYING）operation
    """

    __tablename__ = "workspace_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id"), nullable=False, index=True
    )
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=OperationStatus.PENDING.value, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # lease/fencing（§5）：claim 时写入 lease_owner（worker id）+ fencing_token；
    # 执行期间周期 heartbeat（heartbeat_at/lease_expires_at 续期）。
    # 只有持有有效 token 的 worker 才能写终态（防止过期 worker 被 reclaim 后
    # 再 mark SUCCEEDED/FAILED —— 双重执行/双终态）。
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fencing_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_ops_workspace_status", "workspace_id", "status"),
        Index("ix_ops_status_lease", "status", "lease_expires_at"),
    )


# ---------------------------------------------------------------------------
# Streaming sessions
# ---------------------------------------------------------------------------


class StreamingSession(Base):
    __tablename__ = "streaming_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StreamingStatus.STARTING.value
    )
    signal_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Courses / Labs / Assignments
# ---------------------------------------------------------------------------


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=True, index=True
    )
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CourseMember(Base):
    __tablename__ = "course_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(36), ForeignKey("courses.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=Role.STUDENT.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("course_id", "user_id", name="uq_course_member"),)


class Lab(Base):
    __tablename__ = "labs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(36), ForeignKey("courses.id"), nullable=False, index=True)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 学生可用配额（GPU 秒）
    quota_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lab_id: Mapped[str] = mapped_column(String(36), ForeignKey("labs.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    assignment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("assignments.id"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="in_progress")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("assignment_id", "user_id", name="uq_submission_user"),)


# ---------------------------------------------------------------------------
# Deployment / Edge
# ---------------------------------------------------------------------------


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False, default="0.1.0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DeploymentRecord(Base):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)
    robot_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    checksum: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DeploymentStatus.PENDING.value
    )
    edge_agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class EdgeAgent(Base):
    __tablename__ = "edge_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=AgentStatus.REGISTERED.value)
    device_info: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TelemetryEvent(Base):
    __tablename__ = "telemetry_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    edge_agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# 向后兼容别名（早期代码引用）
WorkspaceStatusLegacy = WorkspaceStatus

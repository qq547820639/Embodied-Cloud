from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Auth / User
# ---------------------------------------------------------------------------


class RegisterIn(BaseModel):
    email: EmailStr
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    organization_name: str | None = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(ORM):
    id: str
    email: str
    username: str
    role: str
    organization_id: str | None
    created_at: datetime


class AuthOut(BaseModel):
    token: str
    token_type: str = "bearer"  # noqa: S105 OAuth2 标准字段，非密码
    user: UserOut


class OrganizationOut(ORM):
    id: str
    name: str
    owner_id: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------


class TemplateOut(ORM):
    id: str
    slug: str
    name: str
    version: str
    description: str
    category: str
    runtime: str
    image: str | None
    gpu_requirement_gb: int
    repo_asset: str | None
    entrypoint: str
    outputs: list
    requires_streaming: bool
    healthcheck: dict | None
    metadata_json: dict
    launch_command: str
    recommended_vram_gb: int
    estimated_hourly_cost_cny: float
    enabled: bool


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


class WorkspaceCreate(BaseModel):
    template_id: str
    name: str | None = Field(default=None, max_length=120)
    auto_start: bool = True


class WorkspaceOut(ORM):
    id: str
    name: str
    template_id: str
    user_id: str | None
    organization_id: str | None
    status: str
    provider: str
    gpu_index: int | None
    gpu_name: str | None
    gpu_id: str | None
    ide_url: str | None
    stream_hint: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    stopped_at: datetime | None
    accumulated_seconds: int
    # 不是模型列：`ledger.mark_usage_segments()` 在序列化前打到行上的临时属性。
    # 前端为了让数字在两秒一轮的轮询里继续跳，自己算 `accumulated_seconds + live`；
    # "这一段是否已进账本"必须由服务端说，否则它照旧双计（N-64 的第三个读者）。
    usage_segment_booked: bool = False


class WorkspaceAccessOut(BaseModel):
    workspace_id: str
    status: str
    ide_url: str | None
    ide_password: str | None
    stream_hint: str | None
    signal_port: int | None
    media_port: int | None


class DemoCheckpointOut(BaseModel):
    """mock 演示模式的模拟训练产出：供 Sim2Real 部署流程端到端演示。"""

    path: str
    size_bytes: int
    sha256: str


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------


class GpuOut(ORM):
    id: str
    gpu_uuid: str
    host_id: str
    model: str
    memory_total: int
    gpu_index: int | None
    status: str
    workspace_id: str | None
    # 管理员的 drain 意图（证据列）：非 NULL＝已要求这张卡离开池子；`status` 仍是观察值。
    drain_requested_at: datetime | None
    updated_at: datetime


class GpuHostOut(ORM):
    id: str
    name: str
    address: str
    provider: str
    status: str
    # `status` 的凭证：最后一次 inventory 同步成功的时刻（N-110）。
    # 读端自己就能核结论，不必信那一列单边写的词。
    last_synced_at: datetime | None = None


# ---------------------------------------------------------------------------
# Usage / Ledger
# ---------------------------------------------------------------------------


class UsageOut(BaseModel):
    running_workspaces: int
    total_workspaces: int
    accumulated_gpu_seconds: int
    estimated_cost_cny: float
    credits_balance: int = 0


class LedgerEntryOut(ORM):
    id: str
    user_id: str | None
    organization_id: str | None
    type: str
    amount: int
    currency: str
    description: str
    workspace_id: str | None
    template_id: str | None
    gpu_seconds: int | None
    idempotency_key: str
    created_at: datetime


class RechargeIn(BaseModel):
    amount: int = Field(gt=0)
    idempotency_key: str | None = None


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class StreamingOut(ORM):
    id: str
    workspace_id: str
    status: str
    signal_port: int | None
    media_port: int | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------


class CourseOut(ORM):
    id: str
    organization_id: str | None
    owner_id: str
    name: str
    description: str
    slug: str
    created_at: datetime


class CourseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    slug: str = Field(min_length=1, max_length=64)


class LabOut(ORM):
    id: str
    course_id: str
    template_id: str
    name: str
    description: str
    quota_seconds: int
    created_at: datetime


class LabCreate(BaseModel):
    template_id: str
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    quota_seconds: int = 3600


class AssignmentOut(ORM):
    id: str
    lab_id: str
    name: str
    description: str
    due_at: datetime | None
    created_at: datetime


class AssignmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    due_at: datetime | None = None


class SubmissionOut(ORM):
    id: str
    assignment_id: str
    user_id: str
    workspace_id: str | None
    status: str
    created_at: datetime
    completed_at: datetime | None


class CourseMemberOut(ORM):
    id: str
    course_id: str
    user_id: str
    role: str


# ---------------------------------------------------------------------------
# Edge / Deployment
# ---------------------------------------------------------------------------


class EdgeAgentOut(ORM):
    id: str
    name: str
    status: str
    device_info: dict
    last_heartbeat: datetime | None
    created_at: datetime


class EdgeAgentRegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    device_info: dict = Field(default_factory=dict)


class EdgeHeartbeatIn(BaseModel):
    device_info: dict = Field(default_factory=dict)


class ArtifactOut(ORM):
    id: str
    workspace_id: str
    name: str
    path: str
    checksum: str
    size_bytes: int
    model_version: str
    created_at: datetime


class DeploymentCreate(BaseModel):
    workspace_id: str
    artifact_path: str
    robot_type: str = Field(min_length=1, max_length=64)
    model_version: str = "0.1.0"
    # §25/ADR 0007：部署时就把执行设备绑上，agent 之后凭 X-Agent-Token 自行发现并取件。
    edge_agent_id: str | None = None


class DeploymentOut(ORM):
    id: str
    workspace_id: str
    artifact_id: str | None
    model_version: str
    template_version: str
    robot_type: str
    checksum: str
    status: str
    edge_agent_id: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class TelemetryOut(ORM):
    id: str
    edge_agent_id: str
    kind: str
    payload: dict
    created_at: datetime


class HealthOut(BaseModel):
    status: str
    provider: str
    provider_ready: bool
    provider_detail: str
    version: str

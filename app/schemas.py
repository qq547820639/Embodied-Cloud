from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str
    category: str
    runtime: str
    launch_command: str
    requires_streaming: bool
    recommended_vram_gb: int
    estimated_hourly_cost_cny: float
    enabled: bool


class WorkspaceCreate(BaseModel):
    template_id: str
    name: str | None = Field(default=None, max_length=120)
    auto_start: bool = True


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    template_id: str
    status: str
    provider: str
    gpu_index: int | None
    gpu_name: str | None
    ide_url: str | None
    stream_hint: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    stopped_at: datetime | None
    accumulated_seconds: int


class WorkspaceAccessOut(BaseModel):
    workspace_id: str
    status: str
    ide_url: str | None
    ide_password: str | None
    stream_hint: str | None
    signal_port: int | None
    media_port: int | None


class HealthOut(BaseModel):
    status: str
    provider: str
    provider_ready: bool
    provider_detail: str
    version: str


class UsageOut(BaseModel):
    running_workspaces: int
    total_workspaces: int
    accumulated_gpu_seconds: int
    estimated_cost_cny: float

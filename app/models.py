from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class WorkspaceStatus(StrEnum):
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class RuntimeKind(StrEnum):
    MOCK = "mock"
    ISAACLAB = "isaaclab"


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime: Mapped[str] = mapped_column(String(32), nullable=False, default=RuntimeKind.ISAACLAB.value)
    launch_command: Mapped[str] = mapped_column(Text, nullable=False)
    requires_streaming: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recommended_vram_gb: Mapped[int] = mapped_column(Integer, default=16, nullable=False)
    estimated_hourly_cost_cny: Mapped[float] = mapped_column(Float, default=6.0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=WorkspaceStatus.QUEUED.value)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    gpu_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gpu_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
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
    accumulated_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

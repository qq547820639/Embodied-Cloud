import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from ..models import Template, Workspace, WorkspaceStatus
from .providers.base import WorkspaceProvider


def utcnow() -> datetime:
    return datetime.now(UTC)


class WorkspaceOrchestrator:
    def __init__(self, session_factory: sessionmaker[Session], provider: WorkspaceProvider, workspace_root: Path):
        self.session_factory = session_factory
        self.provider = provider
        self.workspace_root = workspace_root

    def create(self, db: Session, template: Template, name: str | None = None) -> Workspace:
        workspace_id = str(uuid.uuid4())
        workspace = Workspace(
            id=workspace_id,
            name=name or f"{template.name} · {workspace_id[:6]}",
            template_id=template.id,
            provider=self.provider.name,
            status=WorkspaceStatus.QUEUED.value,
        )
        db.add(workspace)
        db.commit()
        db.refresh(workspace)
        return workspace

    def start_async(self, workspace_id: str) -> None:
        thread = threading.Thread(target=self._start, args=(workspace_id,), daemon=True)
        thread.start()

    def _start(self, workspace_id: str) -> None:
        with self.session_factory() as db:
            workspace = db.get(Workspace, workspace_id)
            if workspace is None:
                return
            template = db.get(Template, workspace.template_id)
            if template is None:
                workspace.status = WorkspaceStatus.FAILED.value
                workspace.error_message = "Template not found"
                db.commit()
                return
            if workspace.status == WorkspaceStatus.RUNNING.value:
                return
            workspace.status = WorkspaceStatus.PROVISIONING.value
            workspace.error_message = None
            db.commit()
            try:
                result = self.provider.provision(
                    workspace,
                    template,
                    self.workspace_root / workspace.id,
                )
                workspace.gpu_index = result.gpu_index
                workspace.gpu_name = result.gpu_name
                workspace.ide_port = result.ide_port
                workspace.signal_port = result.signal_port
                workspace.media_port = result.media_port
                workspace.ide_url = result.ide_url
                workspace.stream_hint = result.stream_hint
                workspace.password = result.password
                workspace.container_name = result.container_name
                workspace.status = WorkspaceStatus.RUNNING.value
                workspace.started_at = utcnow()
                workspace.stopped_at = None
            except Exception as exc:
                workspace.status = WorkspaceStatus.FAILED.value
                workspace.error_message = str(exc)
            db.commit()

    def stop(self, db: Session, workspace: Workspace) -> Workspace:
        if workspace.status in {WorkspaceStatus.STOPPED.value, WorkspaceStatus.FAILED.value}:
            return workspace
        workspace.status = WorkspaceStatus.STOPPING.value
        db.commit()
        try:
            self.provider.stop(workspace)
            now = utcnow()
            if workspace.started_at:
                started = workspace.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                workspace.accumulated_seconds += max(0, int((now - started).total_seconds()))
            workspace.status = WorkspaceStatus.STOPPED.value
            workspace.stopped_at = now
            workspace.started_at = None
        except Exception as exc:
            workspace.status = WorkspaceStatus.FAILED.value
            workspace.error_message = str(exc)
        db.commit()
        db.refresh(workspace)
        return workspace

    def destroy(self, db: Session, workspace: Workspace) -> None:
        try:
            self.provider.destroy(workspace)
        finally:
            db.delete(workspace)
            db.commit()

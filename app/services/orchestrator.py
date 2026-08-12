import contextlib
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..models import Template, Workspace, WorkspaceStatus
from .ledger import CreditLedgerService
from .providers.base import ResourceReservation, WorkspaceProvider
from .scheduler import GpuScheduler, recover_stuck_gpu_allocations, recover_stuck_workspaces


def utcnow() -> datetime:
    return datetime.now(UTC)


class WorkspaceOrchestrator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: WorkspaceProvider,
        workspace_root: Path,
        scheduler: GpuScheduler | None = None,
        ledger: CreditLedgerService | None = None,
    ):
        self.session_factory = session_factory
        self.provider = provider
        self.workspace_root = workspace_root
        self.scheduler = scheduler or GpuScheduler(session_factory)
        self.ledger = ledger or CreditLedgerService(session_factory)

    # ------------------------------------------------------------------
    def create(
        self,
        db: Session,
        template: Template,
        name: str | None = None,
        user_id: str | None = None,
        organization_id: str | None = None,
    ) -> Workspace:
        workspace_id = str(uuid.uuid4())
        workspace = Workspace(
            id=workspace_id,
            name=name or f"{template.name} · {workspace_id[:6]}",
            template_id=template.id,
            user_id=user_id,
            organization_id=organization_id,
            provider=self.provider.name,
            status=WorkspaceStatus.CREATED.value,
        )
        db.add(workspace)
        db.commit()
        db.refresh(workspace)
        return workspace

    # ------------------------------------------------------------------
    def start_async(self, workspace_id: str) -> None:
        thread = threading.Thread(target=self._start, args=(workspace_id,), daemon=True)
        thread.start()

    def _start(self, workspace_id: str) -> None:
        with self.session_factory() as db:
            workspace = db.get(Workspace, workspace_id)
            if workspace is None:
                return
            template = db.get(Template, workspace.template_id)
            if template is None or not template.enabled:
                self._fail(db, workspace, "Template not found or disabled")
                return
            if workspace.status in {WorkspaceStatus.RUNNING.value, WorkspaceStatus.PROVISIONING.value}:
                return
            workspace.status = WorkspaceStatus.PROVISIONING.value
            workspace.error_message = None
            db.commit()
            try:
                # 1) 原子分配 GPU（GpuScheduler 是唯一 GPU reservation 决策入口）
                gpu = self.scheduler.allocate(
                    db, workspace.id, gpu_requirement_gb=template.gpu_requirement_gb
                )
                workspace.gpu_id = gpu.id
                workspace.gpu_index = gpu.gpu_index
                workspace.gpu_name = gpu.model
                db.commit()
                reservation = ResourceReservation(
                    host_id=gpu.host_id,
                    gpu_id=gpu.id,
                    gpu_uuid=gpu.gpu_uuid,
                    gpu_index=gpu.gpu_index or 0,
                    memory_mb=gpu.memory_total,
                )
                # 2) provider 严格按 reservation 绑定资源，禁止二次决策
                try:
                    result = self.provider.provision(
                        workspace,
                        template,
                        self.workspace_root / workspace.id,
                        reservation,
                    )
                except Exception:
                    # 补偿回滚：清理 provider 已创建的下游资源（容器/Pod/PVC/Service），
                    # 幂等（container_name 未落库时按 workspace.id 推导）
                    with contextlib.suppress(Exception):
                        self.provider.destroy(workspace)
                    raise
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
                # 盲捕获是有意设计：provider/scheduler 边界任意异常 → FAILED（ADR 0002）
                self._fail(db, workspace, str(exc))
            db.commit()

    def _fail(self, db: Session, workspace: Workspace, message: str) -> None:
        workspace.status = WorkspaceStatus.FAILED.value
        workspace.error_message = message
        try:
            self.scheduler.release(db, workspace.id)
        except Exception:
            db.rollback()

    # ------------------------------------------------------------------
    def stop(self, db: Session, workspace: Workspace) -> Workspace:
        if workspace.status in {WorkspaceStatus.STOPPED.value, WorkspaceStatus.FAILED.value}:
            return workspace
        workspace.status = WorkspaceStatus.STOPPING.value
        db.commit()
        try:
            now = utcnow()
            run_seconds = 0
            if workspace.started_at:
                started = workspace.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                run_seconds = max(0, int((now - started).total_seconds()))
                workspace.accumulated_seconds += run_seconds
            self.provider.stop(workspace)
            # 幂等结算该运行段 GPU 秒数（同一运行段重复结算不会重复扣款）
            if workspace.started_at:
                self.ledger.settle_workspace_run(
                    db, workspace, run_seconds, workspace.started_at.isoformat()
                )
            # 释放 GPU（stop 后释放）
            self.scheduler.release(db, workspace.id)
            workspace.status = WorkspaceStatus.STOPPED.value
            workspace.stopped_at = now
            workspace.started_at = None
        except Exception as exc:
            workspace.status = WorkspaceStatus.FAILED.value
            workspace.error_message = str(exc)
        db.commit()
        db.refresh(workspace)
        return workspace

    def _settle_running_segment(self, db: Session, workspace: Workspace) -> None:
        """结算当前 RUNNING 段并累计秒数（供 stop/delete 复用）。"""
        now = utcnow()
        if workspace.status == WorkspaceStatus.RUNNING.value and workspace.started_at:
            started = workspace.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            seconds = max(0, int((now - started).total_seconds()))
            workspace.accumulated_seconds += seconds
            if seconds > 0:
                self.ledger.settle_workspace_run(
                    db, workspace, seconds, workspace.started_at.isoformat()
                )
            db.commit()

    def destroy(self, db: Session, workspace: Workspace) -> None:
        try:
            self._settle_running_segment(db, workspace)
        except Exception:
            db.rollback()
        try:
            self.provider.destroy(workspace)
        finally:
            try:
                self.scheduler.release(db, workspace.id)
            except Exception:
                db.rollback()
            db.delete(workspace)
            db.commit()

    # ------------------------------------------------------------------
    def crash_recovery(self) -> None:
        """控制面重启后的恢复：
        - QUEUED/PROVISIONING → FAILED（provisioning 未完成，归还 GPU）
        - RUNNING → stop + 结算 + 归还 GPU（无法保证 provider 状态存活，最可靠）
        - 释放所有孤儿 GPU 分配（无 RUNNING workspace 的绑定）
        """
        with self.session_factory() as db:
            stuck_ids = recover_stuck_workspaces(db)
            for wid in stuck_ids:
                w = db.get(Workspace, wid)
                if w is not None:
                    self._fail(db, w, "recovered: control plane restarted during provisioning")
            running = db.scalars(
                select(Workspace).where(Workspace.status == WorkspaceStatus.RUNNING.value)
            ).all()
            for w in running:
                self.stop(db, w)
            recover_stuck_gpu_allocations(db)

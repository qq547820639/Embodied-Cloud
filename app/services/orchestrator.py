import contextlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..models import (
    OperationStatus,
    OperationType,
    Template,
    TemplateVersion,
    User,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from .ledger import CreditLedgerService
from .providers.base import ResourceReservation, RuntimeState, WorkspaceProvider
from .scheduler import GpuScheduler, recover_stuck_gpu_allocations
from .streaming import StreamingSessionService
from .worker import enqueue_operation


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
        streaming: StreamingSessionService | None = None,
        billing: "BillingPolicy | None" = None,
    ):
        self.session_factory = session_factory
        self.provider = provider
        self.workspace_root = workspace_root
        self.scheduler = scheduler or GpuScheduler(session_factory)
        self.ledger = ledger or CreditLedgerService(session_factory)
        self.streaming = streaming or StreamingSessionService(session_factory)
        self.billing = billing

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------
    def create(
        self,
        db: Session,
        template: Template,
        name: str | None = None,
        user_id: str | None = None,
        organization_id: str | None = None,
    ) -> Workspace:
        # 绑定具体不可变 TemplateVersion（runtime image 的单一事实来源）
        template_version = db.scalar(
            select(TemplateVersion)
            .where(
                TemplateVersion.template_id == template.id,
                TemplateVersion.released.is_(True),
            )
            .order_by(TemplateVersion.created_at.desc())
        )
        workspace_id = str(uuid.uuid4())
        workspace = Workspace(
            id=workspace_id,
            name=name or f"{template.name} · {workspace_id[:6]}",
            template_id=template.id,
            template_version_id=template_version.id if template_version else None,
            # 快照版本镜像：启动时 provider 使用该镜像（禁止 mutable latest）
            image=template_version.image if template_version else template.image,
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
    # durable operations（替换 threading.Thread 裸线程）
    # ------------------------------------------------------------------
    def start_async(self, workspace_id: str) -> WorkspaceOperation | None:
        """把 workspace 的 PROVISION 任务入队为 durable operation（PENDING）。"""
        return enqueue_operation(self.session_factory, workspace_id, OperationType.PROVISION)

    def execute_operation(self, db: Session, op: WorkspaceOperation) -> None:
        """worker 回调：执行一个 operation；抛错由 worker 按 attempts 决定重试/失败。"""
        workspace = db.get(Workspace, op.workspace_id)
        if workspace is None:
            raise RuntimeError(f"workspace {op.workspace_id[:8]} not found for operation")
        if op.operation_type in {OperationType.PROVISION.value, OperationType.START.value}:
            self._execute_provision(db, workspace)
        elif op.operation_type == OperationType.RECONCILE.value:
            self.reconcile_all()
        else:
            raise RuntimeError(f"unsupported operation type: {op.operation_type}")

    def _start(self, workspace_id: str) -> None:
        """同步执行 provision（测试与内部同步路径；生产异步路径走 operation worker）。

        失败不向上抛：workspace 状态已由 _execute_provision 置为 FAILED（ADR 0002）。
        """
        with self.session_factory() as db:
            workspace = db.get(Workspace, workspace_id)
            if workspace is None:
                return
            # 失败不向上抛：workspace 状态已由 _execute_provision 置为 FAILED（ADR 0002）
            with contextlib.suppress(Exception):
                self._execute_provision(db, workspace)
            db.commit()

    # ------------------------------------------------------------------
    # provision（同步核心；由 operation 驱动）
    # ------------------------------------------------------------------
    def _execute_provision(self, db: Session, workspace: Workspace) -> None:
        """将 workspace 带到 RUNNING；幂等（已 RUNNING/PROVISIONING 直接返回）。"""
        if workspace.status in {WorkspaceStatus.RUNNING.value, WorkspaceStatus.PROVISIONING.value}:
            return
        template = db.get(Template, workspace.template_id)
        if template is None or not template.enabled:
            raise RuntimeError("Template not found or disabled")
        # BillingPolicy 兜底门禁（API 层已检查；直接调用/重试路径再确认一次）
        if self.billing is not None and workspace.user_id is not None:
            user = db.get(User, workspace.user_id)
            if user is not None:
                self.billing.check_launch_eligible(db, user, template)
        # 失败重试前将状态置回 QUEUED（上次失败已置 FAILED）
        workspace.status = WorkspaceStatus.QUEUED.value
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
            workspace.status = WorkspaceStatus.PROVISIONING.value
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
            raise
        db.commit()

    def _fail(self, db: Session, workspace: Workspace, message: str) -> None:
        workspace.status = WorkspaceStatus.FAILED.value
        workspace.error_message = message
        try:
            self.scheduler.release(db, workspace.id)
        except Exception:
            db.rollback()

    # ------------------------------------------------------------------
    # stop / destroy（同步 API 语义）
    # ------------------------------------------------------------------
    def stop(self, db: Session, workspace: Workspace) -> Workspace:
        """STOP 生命周期：streaming.stop → runtime.stop → billing settle → GPU release。

        全部步骤幂等；中途失败置 FAILED，重试（再次 stop）可继续完成 cleanup。
        """
        if workspace.status in {WorkspaceStatus.STOPPED.value, WorkspaceStatus.FAILED.value}:
            # 幂等收尾：若上次 stop 失败残留未完成清理（流会话/端口/GPU），补做
            self._finalize_stop(db, workspace)
            db.commit()
            db.refresh(workspace)
            return workspace
        workspace.status = WorkspaceStatus.STOPPING.value
        db.commit()
        try:
            # 1) 先终结流媒体会话并释放流端口
            self.streaming.terminate_for_workspace(db, workspace.id)
            # 2) runtime stop
            self.provider.stop(workspace)
            # 3) 结算 + 释放 GPU
            self._finalize_stop(db, workspace)
        except Exception as exc:
            workspace.status = WorkspaceStatus.FAILED.value
            workspace.error_message = str(exc)
        db.commit()
        db.refresh(workspace)
        return workspace

    def _finalize_stop(self, db: Session, workspace: Workspace) -> None:
        """结算运行段 + 释放 GPU + STOPPED（幂等；reconcile 与 stop 共用）。"""
        now = utcnow()
        if workspace.started_at:
            started = workspace.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            run_seconds = max(0, int((now - started).total_seconds()))
            workspace.accumulated_seconds += run_seconds
            # 幂等结算该运行段 GPU 秒数（同一运行段重复结算不会重复扣款）
            self.ledger.settle_workspace_run(
                db, workspace, run_seconds, workspace.started_at.isoformat()
            )
        # 释放 GPU（stop 后释放；幂等）
        self.scheduler.release(db, workspace.id)
        workspace.status = WorkspaceStatus.STOPPED.value
        workspace.stopped_at = now
        workspace.started_at = None

    def _settle_running_segment(self, db: Session, workspace: Workspace) -> None:
        """结算当前 RUNNING 段并累计秒数（供 destroy/delete 复用）。"""
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
        """DESTROY 生命周期（soft delete / tombstone 语义）：
        streaming.stop → runtime.destroy → billing settle → GPU release → DELETED。

        workspace 行保留（deleted_at tombstone）用于 billing/audit/deployment
        history/安全调查；API 默认不返回 deleted workspace。全部步骤幂等。
        """
        if workspace.deleted_at is not None:
            return  # 已 tombstone，幂等
        try:
            # 1) 先终结流媒体会话（幂等）
            self.streaming.terminate_for_workspace(db, workspace.id)
        except Exception:
            db.rollback()
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
            # tombstone：状态 DELETED + deleted_at，行保留
            workspace.status = WorkspaceStatus.DELETED.value
            workspace.deleted_at = utcnow()
            workspace.started_at = None
            db.commit()

    # ------------------------------------------------------------------
    # reconciliation（替代 crash_recovery 的"停掉所有 RUNNING"）
    # ------------------------------------------------------------------
    def reconcile_all(self) -> dict[str, int]:
        """把 DB 状态收敛到 runtime 事实。幂等：连续执行多次结果一致。

        规则：
        - DB RUNNING + runtime ALIVE  → 保持（adopt）
        - DB RUNNING + runtime MISSING → 结算确认的 usage → 释放 GPU → FAILED
        - DB PROVISIONING + runtime ALIVE → RUNNING（adopt）
        - DB PROVISIONING + runtime MISSING → 无 active operation 则重新入队（retry）
        - DB STOPPING + runtime 停止/缺失 → 结算 → 释放 → STOPPED
        - QUEUED/CREATED + runtime MISSING → 重新入队 PROVISION
        - UNKNOWN（mock 无真实 runtime）→ 不动，避免误杀
        """
        stats = {"adopted": 0, "failed": 0, "stopped": 0, "requeued": 0, "kept": 0}
        with self.session_factory() as db:
            for w in db.scalars(select(Workspace).order_by(Workspace.created_at)):
                if w.deleted_at is not None:
                    continue  # tombstone：不参与 reconcile
                if w.status in {WorkspaceStatus.STOPPED.value, WorkspaceStatus.FAILED.value}:
                    continue
                state = self.provider.reconcile(w)
                if w.status == WorkspaceStatus.RUNNING.value:
                    if state == RuntimeState.ALIVE:
                        stats["kept"] += 1  # adopt：继续运行
                    elif state == RuntimeState.MISSING:
                        self._settle_running_segment(db, w)
                        self.scheduler.release(db, w.id)
                        w.status = WorkspaceStatus.FAILED.value
                        w.error_message = "reconciled: runtime missing"
                        w.stopped_at = utcnow()
                        w.started_at = None
                        stats["failed"] += 1
                    # UNKNOWN：无真实 runtime 可判定，保守不动
                elif w.status == WorkspaceStatus.PROVISIONING.value:
                    if state == RuntimeState.ALIVE:
                        # 实际已就绪（如容器先于 DB 提交）：adopt
                        w.status = WorkspaceStatus.RUNNING.value
                        w.started_at = w.started_at or utcnow()
                        w.stopped_at = None
                        stats["adopted"] += 1
                    elif state == RuntimeState.MISSING:
                        if not self._has_active_operation(db, w.id):
                            enqueue_operation(db, w.id, OperationType.PROVISION)
                            stats["requeued"] += 1
                elif w.status == WorkspaceStatus.STOPPING.value:
                    if state == RuntimeState.ALIVE:
                        # runtime 仍存活：再次尝试停止
                        with contextlib.suppress(Exception):
                            self.provider.stop(w)
                        self._finalize_stop(db, w)
                    elif state == RuntimeState.MISSING:
                        # runtime 已停（或从未起来）：结算 + 释放
                        self._finalize_stop(db, w)
                    stats["stopped"] += 1
                elif w.status in {
                    WorkspaceStatus.QUEUED.value,
                    WorkspaceStatus.CREATED.value,
                } and not self._has_active_operation(db, w.id):
                    enqueue_operation(db, w.id, OperationType.PROVISION)
                    stats["requeued"] += 1
            db.commit()
            recover_stuck_gpu_allocations(db)
        return stats

    def _has_active_operation(self, db: Session, workspace_id: str) -> bool:
        return (
            db.scalar(
                select(WorkspaceOperation.id).where(
                    WorkspaceOperation.workspace_id == workspace_id,
                    WorkspaceOperation.status.in_(
                        [
                            OperationStatus.PENDING.value,
                            OperationStatus.RUNNING.value,
                            OperationStatus.RETRYING.value,
                        ]
                    ),
                )
            )
            is not None
        )

    def crash_recovery(self) -> dict[str, int]:
        """启动恢复：基于 runtime 事实的 reconciliation（幂等、不误杀存活 runtime）。"""
        return self.reconcile_all()

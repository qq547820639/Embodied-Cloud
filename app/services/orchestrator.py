import contextlib
import logging
import time
import uuid
from datetime import UTC
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..models import (
    Gpu,
    GpuHost,
    Lab,
    OperationStatus,
    OperationType,
    Template,
    TemplateVersion,
    User,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from ..utils import utcnow
from .billing import BillingPolicy
from .image_ref import pinned_ref
from .ledger import CreditLedgerService
from .providers.base import ResourceReservation, RuntimeState, WorkspaceProvider
from .scheduler import GpuScheduler, recover_stuck_gpu_allocations
from .streaming import StreamingSessionService
from .worker import OperationWorker, enqueue_operation

logger = logging.getLogger("embodiedcloud.orchestrator")


def _failure_is_terminal(operation: "WorkspaceOperation | None") -> bool:
    """这一次失败是不是最后一次：判据从 worker 那一侧来，不在这里重抄一遍。

    `operation is None` = 同步路径（`WorkspaceOrchestrator._start`）：没有 worker 接管，
    这一次就是最后一次，维持既有的 FAILED 语义（ADR 0002 修订）。
    """
    return operation is None or not OperationWorker.will_retry(operation)


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
        credential_cipher=None,
        ready_timeout_seconds: int = 120,
    ):
        self.session_factory = session_factory
        self.provider = provider
        self.workspace_root = workspace_root
        self.scheduler = scheduler or GpuScheduler(session_factory)
        self.ledger = ledger or CreditLedgerService(session_factory)
        self.streaming = streaming or StreamingSessionService(session_factory)
        self.billing = billing
        # §20：控制面 DB 不保存明文密码（None 时=测试/无加密环境，直接存明文）
        self.credential_cipher = credential_cipher
        self.ready_timeout_seconds = ready_timeout_seconds

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
        # §11：绑定具体不可变 TemplateVersion（runtime image 的单一事实来源）。
        # 确定性 active-version 指针（template.current_version_id，发布策略决定）；
        # 旧数据无指针时按 released + created_at 兜底（迁移期）。
        template_version = None
        if template.current_version_id is not None:
            template_version = db.get(TemplateVersion, template.current_version_id)
        if template_version is None:
            template_version = db.scalar(
                select(TemplateVersion)
                .where(
                    TemplateVersion.template_id == template.id,
                    TemplateVersion.released.is_(True),
                )
                # 必须全序：同一次提交里发布的两个版本 created_at 可能完全相同，
                # 只按 created_at 排会让"新工作区用哪个镜像"变成随机结果。
                # 唯一确定的 active 指针仍是 template.current_version_id；
                # 这里的次级键只为给无指针的历史数据一个稳定答案。
                .order_by(TemplateVersion.created_at.desc(), TemplateVersion.id.desc())
            )
        workspace_id = str(uuid.uuid4())
        workspace = Workspace(
            id=workspace_id,
            name=name or f"{template.name} · {workspace_id[:6]}",
            template_id=template.id,
            template_version_id=template_version.id if template_version else None,
            # 快照版本镜像：启动时 provider 使用该镜像（禁止 mutable latest）。
            # 版本若声明了 image_digest，快照那一刻就钉成不可变引用——provider
            # 两条路径都只读 workspace.image，不需要各自再判一次"要不要钉"。
            image=pinned_ref(
                (template_version.image if template_version else template.image),
                template_version.image_digest if template_version else None,
            ),
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
            self._execute_provision(db, workspace, operation=op)
        elif op.operation_type == OperationType.STOP.value:
            # §12：durable STOP —— 幂等（重复 stop 不重复结算/释放）
            self.stop(db, workspace)
        elif op.operation_type == OperationType.DESTROY.value:
            # §12：durable DESTROY —— 幂等 tombstone；控制面重启不丢清理
            self.destroy(db, workspace)
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
    def _execute_provision(
        self, db: Session, workspace: Workspace, operation: "WorkspaceOperation | None" = None
    ) -> None:
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
                # §S-3：provision 重试路径补 course quota 门禁（无 lab 上下文，
                # 按 template 匹配所有 lab 检查；首次执行后配额耗尽 → 重试被拦截）
                self.billing.check_course_quota(db, user, template)
                # §18：预授权 —— 真把这笔额度圈住（幂等：重试路径命中同一 pending hold）
                self.billing.reserve_launch(db, user, workspace.id)
        # 失败重试前将状态置回 QUEUED（上次失败已置 FAILED）
        workspace.status = WorkspaceStatus.QUEUED.value
        workspace.error_message = None
        db.commit()
        # §25：launch 指标（含时长观测）
        from ..metrics import (
            record_workspace_launch_duration,
            record_workspace_launch_failure,
            record_workspace_launch_start,
        )

        record_workspace_launch_start(template.id, workspace.provider)
        launch_started = time.monotonic()
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
            # §11：operation/fencing 上下文注入 reservation →
            # provider 外部资源打 label/annotation（worker 外部副作用可审计/可 adopt）
            op_meta: dict = {}
            if operation is not None:
                op_meta = {
                    "operation_id": operation.id,
                    "fencing_token": operation.fencing_token or "",
                }
            # §10：node identity 明确字段（K8s nodeSelector/affinity 依据）
            host = db.get(GpuHost, gpu.host_id)
            reservation = ResourceReservation(
                host_id=gpu.host_id,
                gpu_id=gpu.id,
                gpu_uuid=gpu.gpu_uuid,
                gpu_index=gpu.gpu_index or 0,
                memory_mb=gpu.memory_total,
                node_name=host.name if host is not None else gpu.host_id,
                metadata=op_meta,
            )
            # 2) provider 严格按 reservation 绑定资源，禁止二次决策；
            #    §9 readiness gate 也在同一补偿域内（未就绪 → destroy + release）
            try:
                result = self.provider.provision(
                    workspace,
                    template,
                    self.workspace_root / workspace.id,
                    reservation,
                )
                ready = self.provider.wait_ready(
                    workspace, template, timeout_seconds=self.ready_timeout_seconds
                )
                if not ready:
                    raise RuntimeError(
                        f"runtime readiness timeout after {self.ready_timeout_seconds}s"
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
            # §20：DB 只保存加密凭据（runtime 侧仍有一份必要副本）
            if result.password is not None:
                workspace.password = (
                    self.credential_cipher.encrypt(result.password)
                    if self.credential_cipher is not None
                    else result.password
                )
            workspace.container_name = result.container_name
            workspace.status = WorkspaceStatus.RUNNING.value
            workspace.started_at = utcnow()
            workspace.stopped_at = None
            record_workspace_launch_duration(template.id, time.monotonic() - launch_started)
        except Exception as exc:
            # 盲捕获是有意设计：provider/scheduler 边界任意异常不得泄漏到 API（ADR 0002）
            record_workspace_launch_failure(template.id, workspace.provider)
            self._fail(db, workspace, str(exc), terminal=_failure_is_terminal(operation))
            raise
        db.commit()

    def _fail(
        self, db: Session, workspace: Workspace, message: str, *, terminal: bool = True
    ) -> None:
        """记录这一次尝试的失败原因，并尽力释放 GPU；只有终态才写 FAILED。

        `terminal=False`（worker 还会按 attempts 重试）时，status 停在 QUEUED：
        把"还要再试"的失败写成 FAILED，等于对读者宣布一个还没下的结论——
        `workspace_operations` 在 API 层零读者，status 是"还在重试"这件事的唯一出口。
        实测形状：共享卡池被借走的那 1s 里第 1 次尝试报 `No GPU available` → 旧写法
        当场 FAILED → 第 2 次尝试成功 → 同一个 workspace 又回到 RUNNING（FAILED→RUNNING
        的复活），期间 GET /api/workspaces/{id} 读到的是假死。常驻对照见
        tests/test_worker.py::test_retryable_provision_failure_is_not_published_as_terminal。

        终态那一路的持久化要求不变：release 异常时回滚只撤销 release 的局部修改，
        随后重新置位 + error_message + 清 GPU 字段；GPU 残留由 reconcile 补做。
        """
        settled = WorkspaceStatus.FAILED.value if terminal else WorkspaceStatus.QUEUED.value
        # §18：启动失败 → 圈住的额度原样退回（不产生任何账本条目；重试那一轮会重新圈）
        if self.billing is not None:
            try:
                self.billing.release_hold(db, workspace.id, reason="provision failed")
            except Exception as exc:  # 释放失败不得掩盖状态置位（盲捕获有意，见 ADR 0002）
                logger.warning("hold release failed for %s: %s", workspace.id[:8], exc)
        workspace.status = settled
        workspace.error_message = message
        # 先 flush：release 成功时其内部 commit 会连同状态置位一并落库
        db.flush()
        try:
            self.scheduler.release(db, workspace.id)
        except Exception as exc:
            db.rollback()
            # 不得因 release 失败回滚状态置位：重新置位（release 失败可被 reconcile 补做）
            logger.warning(
                "workspace %s GPU release failed during fail: %s (will be reconciled)",
                workspace.id[:8], exc,
            )
            workspace.status = settled
            workspace.error_message = message
        # 这一次尝试没有留下运行时：workspace 不得继续声称占有 GPU（字段一并清除）
        workspace.gpu_id = None
        workspace.gpu_index = None
        workspace.gpu_name = None
        db.flush()

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

    def _settle_run(self, db: Session, workspace: Workspace) -> int:
        """结算当前运行段并累计秒数（幂等）；返回结算的 GPU 秒数。

        idempotency_key = usage:{workspace.id}:{started_at.isoformat()}，
        与 settle_workspace_run 内部一致：同一运行段重复结算不会重复扣款。
        无 started_at（尚未开始运行）时返回 0，且不产生账本/累计副作用。
        """
        if not workspace.started_at:
            return 0
        started = workspace.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        run_seconds = max(0, int((utcnow() - started).total_seconds()))
        workspace.accumulated_seconds += run_seconds
        # 幂等结算该运行段 GPU 秒数（同一运行段重复结算不会重复扣款）
        entry = self.ledger.settle_workspace_run(
            db, workspace, run_seconds, workspace.started_at.isoformat()
        )
        # §18：hold 随结算转正。**无条件**收口：不足 1 秒的运行段不产生 usage 条目
        # （settle_workspace_run 对 0 秒返回 None），若只在有账本条目时 capture，
        # 这种段会把 pending hold 一直留到超时扫描才回收。
        if self.billing is not None:
            self.billing.capture_hold(
                db,
                workspace.id,
                usage_seconds=run_seconds,
                ledger_usage_key=entry.idempotency_key if entry is not None else None,
            )
        return run_seconds

    def _finalize_stop(self, db: Session, workspace: Workspace) -> None:
        """结算运行段 + 释放 GPU + STOPPED（幂等；reconcile 与 stop 共用）。"""
        now = utcnow()
        had_start = workspace.started_at is not None
        run_seconds = self._settle_run(db, workspace)
        if had_start:
            # §25：实际计费 GPU 秒指标（仅存在运行段时记录，语义与原实现一致）
            from ..metrics import record_gpu_seconds

            record_gpu_seconds(run_seconds)
        # 释放 GPU（stop 后释放；幂等）
        self.scheduler.release(db, workspace.id)
        workspace.status = WorkspaceStatus.STOPPED.value
        workspace.stopped_at = now
        workspace.started_at = None

    def _settle_running_segment(self, db: Session, workspace: Workspace) -> None:
        """结算当前 RUNNING 段并累计秒数（供 destroy/delete 复用）。"""
        if workspace.status == WorkspaceStatus.RUNNING.value and workspace.started_at:
            self._settle_run(db, workspace)
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
        except Exception as exc:
            db.rollback()
            # 结算失败不得静默丢账：留下可审计、可补偿的痕迹（error_message + ERROR 日志），
            # 但不阻断资源释放与 DELETED 置位（修复方向是「可补偿」，不是「让 destroy 失败」）。
            workspace.error_message = f"destroy settle failed: {exc}"
            logger.error("workspace %s destroy settle failed: %s", workspace.id[:8], exc)
        try:
            self.provider.destroy(workspace)
        except Exception:
            # provider 清理失败（如 docker daemon 不可用）→ 不得释放 GPU / 置 DELETED：
            # 否则孤儿容器仍持有 --gpus device=N，却被释放 GPU → 一卡双跑。
            # 上抛让 DESTROY operation 重试（ADR 0002 边界），重试时再补做清理。
            db.rollback()
            raise
        try:
            self.scheduler.release(db, workspace.id)
        except Exception:
            db.rollback()
        # tombstone：状态 DELETED + deleted_at，行保留（仅 provider 清理成功后置位）
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
                # §10：K8s 路径校验 Pod 实际 nodeName == reservation.node_name
                node_mismatch = False
                if (
                    w.status == WorkspaceStatus.RUNNING.value
                    and w.provider in {"k8s", "kubernetes"}
                    and w.gpu_id is not None
                ):
                    gpu = db.get(Gpu, w.gpu_id)
                    host = db.get(GpuHost, gpu.host_id) if gpu is not None else None
                    reserved_node = host.name if host is not None else None
                    if reserved_node:
                        try:
                            runtime = self.provider.inspect(w)
                        except Exception:
                            runtime = {}
                        actual_node = (
                            runtime.get("node_name") or runtime.get("node")
                        )
                        if actual_node and actual_node != reserved_node:
                            node_mismatch = True
                            logger.warning(
                                "workspace %s: pod on node %r but reserved %r",
                                w.id[:8], actual_node, reserved_node,
                            )
                if w.status == WorkspaceStatus.RUNNING.value:
                    if node_mismatch:
                        # 分配节点与实际运行节点不一致 → 不得保持 RUNNING
                        self._settle_running_segment(db, w)
                        self.scheduler.release(db, w.id)
                        w.status = WorkspaceStatus.FAILED.value
                        w.error_message = (
                            f"reconciled: pod node mismatch (actual={actual_node}, reserved={reserved_node})"
                        )
                        w.stopped_at = utcnow()
                        w.started_at = None
                        stats["failed"] += 1
                    elif state == RuntimeState.ALIVE:
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

    def release_expired_holds(self) -> int:
        """周期回收超时 pending hold（§18 崩溃残留兜底；RUNNING 的段不回收）。"""
        with self.session_factory() as db:
            if self.billing is None:
                return 0
            return self.billing.release_expired_holds(db)

    def monitor_runtime_quotas(self) -> dict[str, int]:
        """§12 active-runtime quota monitor：防止 workspace 无限跑成大额负数。

        对每个 RUNNING workspace：
        - 投影余额 = 当前余额 - 本运行段 live 消耗（预估）
          投影余额 < 0（即将透支）→ 优雅停止（settle + release + STOPPED）
        - course quota：模板对应 lab 配额用尽 → 同样停止
        幂等：停止走 _finalize_stop（幂等结算/释放）；重复 monitor 不重复扣费。
        """
        stats = {"stopped": 0, "scanned": 0, "notified": 0}
        with self.session_factory() as db:
            # 复用注入的 BillingPolicy（构造属性），无 policy 时整段跳过（如测试/无计费环境）
            if self.billing is None:
                logger.debug("quota monitor skipped: no billing policy configured")
                return stats
            running = db.scalars(
                select(Workspace).where(
                    Workspace.status == WorkspaceStatus.RUNNING.value,
                    Workspace.deleted_at.is_(None),
                )
            ).all()
            for w in running:
                if w.user_id is None:
                    continue  # warm pool（无归属）不监控计费
                stats["scanned"] += 1
                user = db.get(User, w.user_id)
                if user is None:
                    continue
                # 投影余额：当前余额 - 本运行段已消耗
                live = 0
                if w.started_at:
                    started = w.started_at
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=UTC)
                    live = max(0, int((utcnow() - started).total_seconds()))
                # 口径对齐 billing.check_launch_eligible：个人 + 组织 合并余额
                org_balance = (
                    self.ledger.organization_balance(db, user.organization_id)
                    if user.organization_id
                    else 0
                )
                projected = self.ledger.balance(db, user.id) + org_balance - live
                # course quota 检查
                labs = db.scalars(
                    select(Lab).where(Lab.template_id == w.template_id)
                ).all()
                quota_exceeded = any(
                    self.billing.course_usage_seconds(db, user.id, lab) >= lab.quota_seconds
                    for lab in labs
                )
                if projected < 0 or quota_exceeded:
                    self.stop(db, w)  # 幂等：streaming→runtime→settle→release→STOPPED
                    w.error_message = (
                        "quota monitor: credits exhausted" if projected < 0
                        else "quota monitor: course quota reached"
                    )
                    db.commit()
                    stats["stopped"] += 1
        return stats

    def crash_recovery(self) -> dict[str, int]:
        """启动恢复：基于 runtime 事实的 reconciliation（幂等、不误杀存活 runtime）。"""
        return self.reconcile_all()

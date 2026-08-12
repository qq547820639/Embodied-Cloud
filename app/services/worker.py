"""DB-backed Workspace Operation worker（替代裸 threading.Thread 异步启动）。

设计（第一阶段允许 DB-backed，不要求 Redis/Celery）：
- claim：PENDING → RUNNING（记录 lease_expires_at）；RUNNING lease 过期可重新 claim
- 串行：同一 workspace 同一时刻至多一个 active（PENDING/RUNNING/RETRYING）operation
- 重试：失败 attempts < MAX_ATTEMPTS → 回到 PENDING（带 backoff）；否则 FAILED
- durable：operation 落在 DB；控制面重启后 PENDING/RETRYING 可被新 worker 继续执行
"""

import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..models import OperationStatus, OperationType, WorkspaceOperation

logger = logging.getLogger("embodiedcloud.worker")


def utcnow() -> datetime:
    return datetime.now(UTC)


def enqueue_operation(
    session_factory: sessionmaker[Session] | Session,
    workspace_id: str,
    operation_type: OperationType,
) -> WorkspaceOperation | None:
    """创建 PENDING operation；同 workspace 已有 active operation 时拒绝（串行）。

    session_factory 可为 sessionmaker，也可为已打开的 Session（reconcile 复用事务）。
    """
    if isinstance(session_factory, Session):
        db = session_factory
        owns = False
    else:
        db = session_factory()
        owns = True
    try:
        active = db.scalar(
            select(WorkspaceOperation).where(
                WorkspaceOperation.workspace_id == workspace_id,
                WorkspaceOperation.status.in_(
                    [OperationStatus.PENDING.value, OperationStatus.RUNNING.value, OperationStatus.RETRYING.value]
                ),
            )
        )
        if active is not None:
            logger.info(
                "skip enqueue %s for workspace %s: operation %s already active",
                operation_type.value, workspace_id[:8], active.operation_type,
            )
            return None
        op = WorkspaceOperation(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            operation_type=operation_type.value,
            status=OperationStatus.PENDING.value,
            attempts=0,
        )
        db.add(op)
        db.commit()
        db.refresh(op)
        return op
    finally:
        if owns:
            db.close()


class OperationWorker:
    LEASE_SECONDS = 60
    MAX_ATTEMPTS = 3
    TICK_INTERVAL = 1.0
    # 失败重试的指数 backoff（秒）：attempt 1 → 1s, 2 → 2s ...
    RETRY_BASE_DELAY = 1.0

    def __init__(self, session_factory: sessionmaker[Session], executor):
        """executor: 拥有 execute_operation(db, op) 方法的对象（WorkspaceOrchestrator）。"""
        self.session_factory = session_factory
        self.executor = executor
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_forever, name="operation-worker", daemon=True)
        self._thread.start()
        logger.info("operation worker started (lease=%ss max_attempts=%d)", self.LEASE_SECONDS, self.MAX_ATTEMPTS)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as exc:  # worker 循环永不退出；异常记录后继续
                logger.exception("worker tick failed: %s", exc)
            self._stop.wait(self.TICK_INTERVAL)

    # ------------------------------------------------------------------
    # queue
    # ------------------------------------------------------------------
    def enqueue(self, workspace_id: str, operation_type: OperationType) -> WorkspaceOperation | None:
        """创建 PENDING operation；同 workspace 已有 active operation 时拒绝（串行）。"""
        return enqueue_operation(self.session_factory, workspace_id, operation_type)

    # ------------------------------------------------------------------
    # tick / claim / execute
    # ------------------------------------------------------------------
    def tick_once(self) -> int:
        """处理一轮：claim + 执行一个可执行 operation。返回处理数（0/1）。"""
        with self.session_factory() as db:
            op = self._claim_next(db)
            if op is None:
                return 0
            try:
                self.executor.execute_operation(db, op)
            except Exception as exc:  # 操作失败：按 attempts 决定 RETRYING / FAILED
                self.finish_failure(db, op, str(exc))
            else:
                self.finish_success(db, op)
            db.commit()
            return 1

    def _claim_next(self, db: Session) -> WorkspaceOperation | None:
        """按序 claim：先 RUNNING 过期（lease 已到期），再 PENDING/RETRYING 到期的。"""
        now = utcnow()
        # 1) 可 reclaim 的过期 RUNNING（崩溃/断线残留）
        stale = db.scalar(
            select(WorkspaceOperation)
            .where(
                WorkspaceOperation.status == OperationStatus.RUNNING.value,
                WorkspaceOperation.lease_expires_at.is_not(None),
                WorkspaceOperation.lease_expires_at < now,
            )
            .order_by(WorkspaceOperation.created_at)
        )
        if stale is not None:
            return self._do_claim(db, stale, now)

        # 2) 可执行的 PENDING / 到期 RETRYING
        candidates = db.scalars(
            select(WorkspaceOperation)
            .where(
                WorkspaceOperation.status.in_(
                    [OperationStatus.PENDING.value, OperationStatus.RETRYING.value]
                ),
                (WorkspaceOperation.lease_expires_at.is_(None))
                | (WorkspaceOperation.lease_expires_at <= now),
            )
            .order_by(WorkspaceOperation.created_at)
        ).all()
        active_workspaces = set(
            db.scalars(
                select(WorkspaceOperation.workspace_id).where(
                    WorkspaceOperation.status.in_(
                        [OperationStatus.RUNNING.value]
                    )
                )
            )
        )
        for op in candidates:
            if op.workspace_id in active_workspaces:
                continue  # 同 workspace 已有 RUNNING → 串行等待
            return self._do_claim(db, op, now)
        return None

    def _do_claim(self, db: Session, op: WorkspaceOperation, now: datetime) -> WorkspaceOperation:
        op.status = OperationStatus.RUNNING.value
        op.attempts += 1
        op.started_at = now
        op.lease_expires_at = now + timedelta(seconds=self.LEASE_SECONDS)
        op.last_error = None
        db.commit()
        return op

    def finish_success(self, db: Session, op: WorkspaceOperation) -> None:
        op.status = OperationStatus.SUCCEEDED.value
        op.completed_at = utcnow()
        op.lease_expires_at = None
        db.commit()

    def finish_failure(self, db: Session, op: WorkspaceOperation, error: str) -> None:
        """失败：attempts 未达上限 → RETRYING（backoff 后自动回到可执行）；否则 FAILED。"""
        op.last_error = error
        if op.attempts >= self.MAX_ATTEMPTS:
            op.status = OperationStatus.FAILED.value
            op.completed_at = utcnow()
            op.lease_expires_at = None
            logger.error("operation %s(%s) failed permanently: %s",
                         op.operation_type, op.workspace_id[:8], error)
        else:
            op.status = OperationStatus.RETRYING.value
            delay = self.RETRY_BASE_DELAY * (2 ** (op.attempts - 1))
            op.lease_expires_at = utcnow() + timedelta(seconds=delay)
            logger.warning("operation %s(%s) failed, retrying in %.1fs: %s",
                           op.operation_type, op.workspace_id[:8], delay, error)
        db.commit()

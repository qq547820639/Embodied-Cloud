"""DB-backed Workspace Operation worker（替代裸 threading.Thread 异步启动）。

设计（§5 lease/fencing 正确性）：
- claim 原子：UPDATE ... WHERE id=? AND status=<读到状态> AND lease 可执行；
  rowcount==1 才算 claim 成功（并发下至多一个 worker 持有）
- lease：claim 写入 lease_owner + fencing_token + heartbeat_at + lease_expires_at
- heartbeat：执行期间周期 renew（UPDATE ... WHERE id AND fencing_token AND lease 未过期）
- fencing：finish_success/finish_failure 必须通过带 token 的原子 UPDATE 才能写终态；
  token 失效（被其他 worker reclaim）→ LeaseLostError，禁止写终态
- 串行：同一 workspace 同一时刻至多一个 active（PENDING/RUNNING/RETRYING）operation
- durable：operation 落在 DB；控制面重启后 PENDING/RETRYING 可被新 worker 继续执行
"""

import logging
import secrets
import threading
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from ..models import OperationStatus, OperationType, WorkspaceOperation

logger = logging.getLogger("embodiedcloud.worker")


def utcnow() -> datetime:
    return datetime.now(UTC)


class LeaseLostError(RuntimeError):
    """fencing 失效：本 worker 的 lease 已被其他 worker reclaim，禁止写终态。"""


def enqueue_operation(
    session_factory: sessionmaker[Session] | Session,
    workspace_id: str,
    operation_type: OperationType,
) -> WorkspaceOperation | None:
    """创建 PENDING operation；同 workspace 已有 active operation 时拒绝（串行）。"""
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
            operation_type=getattr(operation_type, "value", operation_type),
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
    # 心跳间隔：lease 的 1/4（60s lease → 15s heartbeat）
    HEARTBEAT_INTERVAL = LEASE_SECONDS / 4
    MAX_ATTEMPTS = 3
    TICK_INTERVAL = 1.0
    # 失败重试的指数 backoff（秒）：attempt 1 → 1s, 2 → 2s ...
    RETRY_BASE_DELAY = 1.0

    def __init__(self, session_factory: sessionmaker[Session], executor):
        """executor: 拥有 execute_operation(db, op) 方法的对象（WorkspaceOrchestrator）。"""
        self.session_factory = session_factory
        self.executor = executor
        # 唯一 worker id：lease_owner 标识（多实例/重启后可区分）
        self.worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def enqueue(self, workspace_id: str, operation_type: OperationType) -> WorkspaceOperation | None:
        """创建 PENDING operation；同 workspace 已有 active operation 时拒绝（串行）。"""
        return enqueue_operation(self.session_factory, workspace_id, operation_type)

    def start(self) -> None:
        if self._thread is not None:
            return
        # 重置停止标记：worker 可在 stop() 后重新启动（TestClient/热重启场景）
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_forever, name="operation-worker", daemon=True)
        self._thread.start()
        logger.info(
            "operation worker %s started (lease=%ss heartbeat=%.1fs max_attempts=%d)",
            self.worker_id, self.LEASE_SECONDS, self.HEARTBEAT_INTERVAL, self.MAX_ATTEMPTS,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            processed = 0
            try:
                # 排空可执行队列（每次循环处理全部积压，避免长队列排队延迟）
                while not self._stop.is_set():
                    processed = self.tick_once()
                    if processed == 0:
                        break
            except Exception as exc:  # worker 循环永不退出；异常记录后继续
                logger.exception("worker tick failed: %s", exc)
            if processed == 0:
                self._stop.wait(self.TICK_INTERVAL)

    # ------------------------------------------------------------------
    # tick / claim / heartbeat / finish（全部原子 + fencing）
    # ------------------------------------------------------------------
    def tick_once(self) -> int:
        """处理一轮：claim + 执行一个可执行 operation。返回处理数（0/1）。"""
        with self.session_factory() as db:
            op = self._claim_next(db)
            if op is None:
                return 0
            # 执行期间周期 heartbeat（renew lease）；被 reclaim 时停止
            stop_heartbeat = threading.Event()

            def _heartbeat_loop():
                while not stop_heartbeat.is_set() and not self._stop.is_set():
                    with self.session_factory() as hb_db:
                        try:
                            self.renew_lease(hb_db, op.id, op.fencing_token or "")
                        except Exception as exc:
                            logger.warning("heartbeat failed: %s", exc)
                    stop_heartbeat.wait(self.HEARTBEAT_INTERVAL)

            hb_thread = threading.Thread(target=_heartbeat_loop, name=f"hb-{op.id[:8]}", daemon=True)
            hb_thread.start()
            try:
                try:
                    self.executor.execute_operation(db, op)
                except Exception as exc:
                    self.finish_failure(db, op, str(exc))
                else:
                    self.finish_success(db, op)
            except LeaseLostError:
                logger.warning(
                    "lease lost for operation %s(%s): 被其他 worker reclaim，放弃写终态",
                    op.operation_type, op.workspace_id[:8],
                )
            finally:
                stop_heartbeat.set()
                hb_thread.join(timeout=2)
            db.commit()
            return 1

    def _claim_next(self, db: Session) -> WorkspaceOperation | None:
        """按序原子 claim：先过期 RUNNING，再 PENDING/到期 RETRYING。"""
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
        if stale is not None and self._try_claim(db, stale, now):
            return stale

        # 2) 可执行的 PENDING / 到期 RETRYING（同 workspace 已有 RUNNING 则跳过）
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
        running_workspaces = set(
            db.scalars(
                select(WorkspaceOperation.workspace_id).where(
                    WorkspaceOperation.status == OperationStatus.RUNNING.value
                )
            )
        )
        for op in candidates:
            if op.workspace_id in running_workspaces:
                continue  # 同 workspace 已有 RUNNING → 串行等待
            if self._try_claim(db, op, now):
                return op
        return None

    def _try_claim(self, db: Session, op: WorkspaceOperation, now: datetime) -> bool:
        """原子 claim：仅当状态仍匹配（未被并发修改）时生效。

        UPDATE ... WHERE id AND status=<读到的状态>（+ lease 条件）→ rowcount==1。
        PostgreSQL/SQLite 下并发事务至多一个成功（行锁/单写者）。
        """
        from typing import Any, cast

        from sqlalchemy.engine import CursorResult

        token = secrets.token_hex(16)
        conditions = [
            WorkspaceOperation.id == op.id,
            WorkspaceOperation.status == op.status,
        ]
        if op.status == OperationStatus.RUNNING.value:
            conditions.append(WorkspaceOperation.lease_expires_at < now)
        elif op.status == OperationStatus.RETRYING.value:
            conditions.append(
                (WorkspaceOperation.lease_expires_at.is_(None))
                | (WorkspaceOperation.lease_expires_at <= now)
            )
        result = db.execute(
            update(WorkspaceOperation)
            .where(*conditions)
            .values(
                status=OperationStatus.RUNNING.value,
                attempts=op.attempts + 1,
                started_at=now,
                lease_owner=self.worker_id,
                fencing_token=token,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=self.LEASE_SECONDS),
                last_error=None,
            )
            # SQL 层比较（SQLite 读回为 naive/字符串，避免 ORM in-Python evaluator 的
            # naive-vs-aware TypeError；PostgreSQL 原生 timestamp 语义不受影响）
            .execution_options(synchronize_session=False)
        )
        db.commit()
        rowcount = cast("CursorResult[Any]", result).rowcount
        if rowcount is None or int(rowcount) != 1:
            return False
        op.status = OperationStatus.RUNNING.value
        op.attempts += 1
        op.lease_owner = self.worker_id
        op.fencing_token = token
        op.heartbeat_at = now
        op.lease_expires_at = now + timedelta(seconds=self.LEASE_SECONDS)
        return True

    def renew_lease(self, db: Session, op_id: str, token: str) -> bool:
        """heartbeat：原子 renew（必须 token 匹配且 lease 未过期）。返回是否成功。"""
        from typing import Any, cast

        from sqlalchemy.engine import CursorResult

        now = utcnow()
        result = db.execute(
            update(WorkspaceOperation)
            .where(
                WorkspaceOperation.id == op_id,
                WorkspaceOperation.fencing_token == token,
                WorkspaceOperation.lease_expires_at > now,
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=self.LEASE_SECONDS),
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        rowcount = cast("CursorResult[Any]", result).rowcount
        return rowcount is not None and int(rowcount) == 1

    def _fenced_update(self, db: Session, op: WorkspaceOperation, values: dict) -> bool:
        """带 fencing 的原子终态写入：token 匹配 + lease 未过期才生效。"""
        from typing import Any, cast

        from sqlalchemy.engine import CursorResult

        result = db.execute(
            update(WorkspaceOperation)
            .where(
                WorkspaceOperation.id == op.id,
                WorkspaceOperation.fencing_token == op.fencing_token,
                WorkspaceOperation.lease_expires_at > utcnow(),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        rowcount = cast("CursorResult[Any]", result).rowcount
        return rowcount is not None and int(rowcount) == 1

    def finish_success(self, db: Session, op: WorkspaceOperation) -> None:
        if not self._fenced_update(
            db, op, {"status": OperationStatus.SUCCEEDED.value, "completed_at": utcnow(), "lease_expires_at": None}
        ):
            raise LeaseLostError(f"operation {op.id[:8]} lease lost")

    def finish_failure(self, db: Session, op: WorkspaceOperation, error: str) -> None:
        """失败：attempts 未达上限 → RETRYING（backoff 后自动回到可执行）；否则 FAILED。"""
        if op.attempts >= self.MAX_ATTEMPTS:
            values = {
                "status": OperationStatus.FAILED.value,
                "completed_at": utcnow(),
                "lease_expires_at": None,
                "last_error": error,
            }
            if not self._fenced_update(db, op, values):
                raise LeaseLostError(f"operation {op.id[:8]} lease lost")
            logger.error("operation %s(%s) failed permanently: %s",
                         op.operation_type, op.workspace_id[:8], error)
        else:
            delay = self.RETRY_BASE_DELAY * (2 ** (op.attempts - 1))
            values = {
                "status": OperationStatus.RETRYING.value,
                "lease_expires_at": utcnow() + timedelta(seconds=delay),
                "last_error": error,
            }
            if not self._fenced_update(db, op, values):
                raise LeaseLostError(f"operation {op.id[:8]} lease lost")
            logger.warning("operation %s(%s) failed, retrying in %.1fs: %s",
                           op.operation_type, op.workspace_id[:8], delay, error)

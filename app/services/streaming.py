"""Streaming 会话状态机服务。

状态机 (见 docs/adr/0001-webrtc-streaming-path.md):
    starting -> ready -> connected -> disconnected -> ready (重连) / any -> failed

connect 同时允许 starting/disconnected 直达 connected (连接建立成功即视为已连接)。
owner 隔离: 所有操作先校验 workspace / session 归属, 越权抛 PermissionError (路由层映射为 404)。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..metrics import STREAM_FAILURE_TOTAL, STREAM_SESSION_TOTAL
from ..models import Role, StreamingSession, StreamingStatus, User, Workspace, WorkspaceStatus

DEFAULT_SIGNAL_PORT = 49100
DEFAULT_MEDIA_PORT = 47998


def utcnow() -> datetime:
    return datetime.now(UTC)


# 合法状态迁移表：{当前状态: {允许的目标状态}}。任何状态均可迁移到 failed。
_STREAM_TRANSITIONS: dict[str, set[str]] = {
    StreamingStatus.STARTING.value: {
        StreamingStatus.READY.value,
        StreamingStatus.CONNECTED.value,
        StreamingStatus.FAILED.value,
    },
    StreamingStatus.READY.value: {StreamingStatus.CONNECTED.value, StreamingStatus.FAILED.value},
    StreamingStatus.CONNECTED.value: {
        StreamingStatus.DISCONNECTED.value,
        StreamingStatus.FAILED.value,
    },
    StreamingStatus.DISCONNECTED.value: {
        StreamingStatus.READY.value,
        StreamingStatus.CONNECTED.value,
        StreamingStatus.FAILED.value,
    },
    StreamingStatus.FAILED.value: set(),
}


class StreamingSessionService:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self, db: Session, workspace_id: str, user: User) -> StreamingSession:
        """为 RUNNING 的 workspace 创建流式会话 (starting -> ready)。"""
        workspace = self._get_owned_workspace(db, workspace_id, user)
        if workspace.status != WorkspaceStatus.RUNNING.value:
            raise ValueError("workspace 未运行，无法启动流式会话")
        session = StreamingSession(
            id=str(uuid.uuid4()),
            workspace_id=workspace.id,
            status=StreamingStatus.STARTING.value,
            signal_port=workspace.signal_port or DEFAULT_SIGNAL_PORT,
            media_port=workspace.media_port or DEFAULT_MEDIA_PORT,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        self._transition(db, session, StreamingStatus.READY.value)
        db.commit()
        db.refresh(session)
        STREAM_SESSION_TOTAL.labels(workspace_id=workspace.id).inc()
        return session

    def connect(self, db: Session, session_id: str, user: User) -> StreamingSession:
        """建立连接: starting / ready / disconnected -> connected。"""
        session = self._get_owned_session(db, session_id, user)
        self._transition(db, session, StreamingStatus.CONNECTED.value)
        db.commit()
        db.refresh(session)
        return session

    def disconnect(self, db: Session, session_id: str, user: User) -> StreamingSession:
        """断开连接: connected -> disconnected。"""
        session = self._get_owned_session(db, session_id, user)
        self._transition(db, session, StreamingStatus.DISCONNECTED.value)
        db.commit()
        db.refresh(session)
        return session

    def reconnect(self, db: Session, session_id: str, user: User) -> StreamingSession:
        """重连: disconnected -> ready (等待客户端重新建立连接)。"""
        session = self._get_owned_session(db, session_id, user)
        self._transition(db, session, StreamingStatus.READY.value)
        db.commit()
        db.refresh(session)
        return session

    def stop(self, db: Session, workspace_id: str, user: User) -> int:
        """workspace 停止/删除时调用: 活动会话全部置 failed 并释放端口。

        返回受影响 (被置为 failed) 的会话数量。
        """
        self._get_owned_workspace(db, workspace_id, user)
        return self.terminate_for_workspace(db, workspace_id)

    def terminate_for_workspace(self, db: Session, workspace_id: str) -> int:
        """内部调用（orchestrator 生命周期集成）：终结该 workspace 全部活动会话。

        幂等：无活动会话时返回 0 且不报错；重复调用结果一致。
        会话置 FAILED + 释放会话与 workspace 的 stream 端口（端口可复用）。
        """
        workspace = db.get(Workspace, workspace_id)
        if workspace is None:
            return 0
        sessions = db.scalars(
            select(StreamingSession).where(
                StreamingSession.workspace_id == workspace.id,
                StreamingSession.status != StreamingStatus.FAILED.value,
            )
        ).all()
        affected = 0
        for session in sessions:
            self._transition(db, session, StreamingStatus.FAILED.value, error="workspace stopped")
            session.signal_port = None
            session.media_port = None
            affected += 1
        # workspace 自身端口一并释放（streaming 端口可复用）
        workspace.signal_port = None
        workspace.media_port = None
        if affected > 0:
            STREAM_FAILURE_TOTAL.labels(workspace_id=workspace.id).inc(affected)
        db.commit()
        return affected

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_for_workspace(self, db: Session, workspace_id: str, user: User) -> list[StreamingSession]:
        self._get_owned_workspace(db, workspace_id, user)
        return list(
            db.scalars(
                select(StreamingSession)
                .where(StreamingSession.workspace_id == workspace_id)
                .order_by(StreamingSession.created_at.desc())
            )
        )

    def get(self, db: Session, session_id: str, user: User) -> StreamingSession:
        return self._get_owned_session(db, session_id, user)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _transition(
        self,
        db: Session,
        session: StreamingSession,
        new_status: str,
        error: str | None = None,
    ) -> StreamingSession:
        """执行状态迁移; 非法迁移抛 ValueError。"""
        allowed = _STREAM_TRANSITIONS.get(session.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"非法状态迁移：{session.status} → {new_status}（会话 {session.id[:8]}）"
            )
        session.status = new_status
        session.error_message = error
        session.updated_at = utcnow()
        return session

    def _get_owned_workspace(self, db: Session, workspace_id: str, user: User) -> Workspace:
        workspace = db.get(Workspace, workspace_id)
        if workspace is None:
            raise PermissionError("workspace 不存在")
        if user.role != Role.ADMIN.value and workspace.user_id != user.id:
            raise PermissionError("无权访问该 workspace")
        return workspace

    def _get_owned_session(self, db: Session, session_id: str, user: User) -> StreamingSession:
        session = db.get(StreamingSession, session_id)
        if session is None:
            raise PermissionError("流式会话不存在")
        workspace = db.get(Workspace, session.workspace_id)
        if workspace is None:
            raise PermissionError("流式会话不存在")
        if user.role != Role.ADMIN.value and workspace.user_id != user.id:
            raise PermissionError("无权访问该会话")
        return session

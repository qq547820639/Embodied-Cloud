"""StreamingSessionService 状态机测试。"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import StreamingSession, StreamingStatus, User, Workspace, WorkspaceStatus
from app.services.streaming import StreamingSessionService


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture()
def service(db_factory):
    return StreamingSessionService(db_factory)


def _make_user(db, user_id: str = "u1", role: str = "user") -> User:
    user = User(
        id=user_id,
        email=f"{user_id}@a.b.c",
        username=user_id,
        password_hash="x",  # noqa: S106 测试占位哈希
        role=role,
    )
    db.add(user)
    db.commit()
    return user


def _make_workspace(db, user, *, running: bool = True, signal_port=None, media_port=None) -> Workspace:
    workspace = Workspace(
        id=str(uuid.uuid4()),
        name="ws",
        template_id="cartpole",
        user_id=user.id,
        provider="mock",
        status=WorkspaceStatus.RUNNING.value if running else WorkspaceStatus.CREATED.value,
        signal_port=signal_port,
        media_port=media_port,
    )
    db.add(workspace)
    db.commit()
    return workspace


def test_start_creates_ready_session_with_workspace_ports(db_factory, service):
    with db_factory() as db:
        user = _make_user(db)
        workspace = _make_workspace(db, user, signal_port=49101, media_port=47999)
        session = service.start(db, workspace.id, user)
        assert session.status == StreamingStatus.READY.value
        assert session.signal_port == 49101
        assert session.media_port == 47999
        assert session.error_message is None
        assert session.workspace_id == workspace.id


def test_start_uses_default_ports_when_missing(db_factory, service):
    with db_factory() as db:
        user = _make_user(db)
        workspace = _make_workspace(db, user)
        session = service.start(db, workspace.id, user)
        assert session.signal_port == 49100
        assert session.media_port == 47998


def test_connect_disconnect_reconnect_cycle(db_factory, service):
    with db_factory() as db:
        user = _make_user(db)
        workspace = _make_workspace(db, user)
        session = service.start(db, workspace.id, user)  # ready
        session = service.connect(db, session.id, user)
        assert session.status == StreamingStatus.CONNECTED.value
        session = service.disconnect(db, session.id, user)
        assert session.status == StreamingStatus.DISCONNECTED.value
        session = service.reconnect(db, session.id, user)
        assert session.status == StreamingStatus.READY.value
        # disconnected 可直达 connected
        session = service.connect(db, session.id, user)
        assert session.status == StreamingStatus.CONNECTED.value


def test_connect_from_starting_is_allowed(db_factory, service):
    with db_factory() as db:
        user = _make_user(db)
        workspace = _make_workspace(db, user)
        session = StreamingSession(
            id=str(uuid.uuid4()),
            workspace_id=workspace.id,
            status=StreamingStatus.STARTING.value,
            signal_port=49100,
            media_port=47998,
        )
        db.add(session)
        db.commit()
        session = service.connect(db, session.id, user)
        assert session.status == StreamingStatus.CONNECTED.value


def test_invalid_transitions_raise_value_error(db_factory, service):
    with db_factory() as db:
        user = _make_user(db)
        workspace = _make_workspace(db, user)
        session = service.start(db, workspace.id, user)  # ready
        with pytest.raises(ValueError):
            service.disconnect(db, session.id, user)  # ready → disconnected 非法
        session = service.connect(db, session.id, user)  # connected
        with pytest.raises(ValueError):
            service.connect(db, session.id, user)  # connected → connected 非法
        with pytest.raises(ValueError):
            service.reconnect(db, session.id, user)  # connected → ready 非法
        session = service.disconnect(db, session.id, user)  # disconnected
        with pytest.raises(ValueError):
            service.disconnect(db, session.id, user)  # disconnected → disconnected 非法
        # 失败态不可再迁移（真实验证见 test_failed_session_is_terminal）
        session = service.reconnect(db, session.id, user)
        session = service.connect(db, session.id, user)
        session = service.disconnect(db, session.id, user)
        session = service.reconnect(db, session.id, user)
        assert session.status == StreamingStatus.READY.value


def test_failed_session_is_terminal(db_factory, service):
    """FAILED 是终态（_STREAM_TRANSITIONS[FAILED]=∅）：connect/disconnect/reconnect
    全部非法迁移。此前「失败态不可再迁移」只有注释、从未构造 FAILED 会话。"""
    with db_factory() as db:
        user = _make_user(db)
        workspace = _make_workspace(db, user)
        session = StreamingSession(
            id=str(uuid.uuid4()),
            workspace_id=workspace.id,
            status=StreamingStatus.FAILED.value,
        )
        db.add(session)
        db.commit()
        for op in ("connect", "disconnect", "reconnect"):
            with pytest.raises(ValueError, match="非法状态迁移"):
                getattr(service, op)(db, session.id, user)


def test_start_requires_running_workspace(db_factory, service):
    with db_factory() as db:
        user = _make_user(db)
        workspace = _make_workspace(db, user, running=False)
        with pytest.raises(ValueError):
            service.start(db, workspace.id, user)


def test_stop_fails_sessions_and_clears_ports(db_factory, service):
    with db_factory() as db:
        user = _make_user(db)
        workspace = _make_workspace(db, user, signal_port=49101, media_port=47999)
        s1 = service.start(db, workspace.id, user)
        s2 = service.start(db, workspace.id, user)
        service.connect(db, s1.id, user)

        affected = service.stop(db, workspace.id, user)
        assert affected == 2

        db.expire_all()
        for session_id in (s1.id, s2.id):
            session = db.get(StreamingSession, session_id)
            assert session.status == StreamingStatus.FAILED.value
            assert session.error_message == "workspace stopped"
            assert session.signal_port is None
            assert session.media_port is None
        # workspace 端口一并释放
        assert db.get(Workspace, workspace.id).signal_port is None
        assert db.get(Workspace, workspace.id).media_port is None
        # 幂等：再次 stop 无活动会话
        assert service.stop(db, workspace.id, user) == 0


def test_owner_isolation_raises_permission_error(db_factory, service):
    with db_factory() as db:
        alice = _make_user(db, "alice")
        bob = _make_user(db, "bob")
        workspace = _make_workspace(db, alice)
        session = service.start(db, workspace.id, alice)
        with pytest.raises(PermissionError):
            service.start(db, workspace.id, bob)
        with pytest.raises(PermissionError):
            service.connect(db, session.id, bob)
        with pytest.raises(PermissionError):
            service.disconnect(db, session.id, bob)
        with pytest.raises(PermissionError):
            service.reconnect(db, session.id, bob)
        with pytest.raises(PermissionError):
            service.stop(db, workspace.id, bob)
        with pytest.raises(PermissionError):
            service.list_for_workspace(db, workspace.id, bob)
        with pytest.raises(PermissionError):
            service.get(db, session.id, bob)
        # 不存在即越权（同 404 语义）
        with pytest.raises(PermissionError):
            service.get(db, "no-such-session", alice)
        # alice 正常
        assert len(service.list_for_workspace(db, workspace.id, alice)) == 1
        assert service.get(db, session.id, alice).id == session.id


def test_list_for_workspace_returns_all_sessions(db_factory, service):
    with db_factory() as db:
        user = _make_user(db)
        workspace = _make_workspace(db, user)
        s1 = service.start(db, workspace.id, user)
        s2 = service.start(db, workspace.id, user)
        sessions = service.list_for_workspace(db, workspace.id, user)
        assert {s.id for s in sessions} == {s1.id, s2.id}


def test_admin_can_access_any_session(db_factory, service):
    with db_factory() as db:
        alice = _make_user(db, "alice")
        admin = _make_user(db, "admin", role="admin")
        workspace = _make_workspace(db, alice)
        session = service.start(db, workspace.id, alice)
        assert service.get(db, session.id, admin).id == session.id

"""EdgeService 与边缘认证依赖测试 (临时 SQLite, 不 import app.main)."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.db import Base
from app.models import EdgeAgent, TelemetryEvent
from app.security import hash_token
from app.services.edge import EdgeService, get_agent_from_header


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    yield sf
    engine.dispose()


@pytest.fixture()
def db(factory):
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _request_with_token(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/edge/agents/test/heartbeat",
            "headers": [(b"x-agent-token", token.encode())],
            "query_string": b"",
            "server": ("testserver", 80),
        }
    )


def test_register_returns_token_and_stores_hash(factory, db):
    svc = EdgeService(factory)
    agent, token = svc.register(db, "edge-01", {"arch": "x86_64", "os": "linux"})
    assert token
    assert agent.status == "registered"
    assert agent.token_hash == hash_token(token)
    found = db.scalar(select(EdgeAgent).where(EdgeAgent.token_hash == hash_token(token)))
    assert found is not None
    assert found.id == agent.id


def test_heartbeat_marks_online_and_updates_device_info(factory, db):
    svc = EdgeService(factory)
    agent, _ = svc.register(db, "edge-01", {"arch": "x86_64"})
    svc.heartbeat(db, agent, {"gpu": "rtx-4090", "arch": "aarch64"})
    assert agent.status == "online"
    assert agent.last_heartbeat is not None
    assert agent.device_info["gpu"] == "rtx-4090"
    assert agent.device_info["arch"] == "aarch64"  # 新值覆盖旧值


def test_report_telemetry_writes_event(factory, db):
    svc = EdgeService(factory)
    agent, _ = svc.register(db, "edge-01", {})
    event = svc.report_telemetry(db, agent, "joint_state", {"q": [0.1, 0.2]})
    assert event.kind == "joint_state"
    rows = db.scalars(select(TelemetryEvent).where(TelemetryEvent.edge_agent_id == agent.id)).all()
    assert len(rows) == 1
    assert rows[0].payload["q"] == [0.1, 0.2]


def test_list_and_get_agents(factory, db):
    svc = EdgeService(factory)
    a1, _ = svc.register(db, "edge-01", {})
    a2, _ = svc.register(db, "edge-02", {})
    assert {a.id for a in svc.list_agents(db)} == {a1.id, a2.id}
    assert svc.get_agent(db, a1.id) is not None
    assert svc.get_agent(db, a1.id).id == a1.id
    assert svc.get_agent(db, "missing") is None


def test_agent_header_auth_success(factory, db):
    svc = EdgeService(factory)
    agent, token = svc.register(db, "edge-01", {})
    assert get_agent_from_header(_request_with_token(token), db).id == agent.id


def test_agent_header_auth_wrong_token_401(factory, db):
    svc = EdgeService(factory)
    svc.register(db, "edge-01", {})
    with pytest.raises(HTTPException) as exc:
        get_agent_from_header(_request_with_token("wrong-token"), db)
    assert exc.value.status_code == 401


def test_agent_header_auth_missing_token_401(factory, db):
    svc = EdgeService(factory)
    svc.register(db, "edge-01", {})
    with pytest.raises(HTTPException) as exc:
        get_agent_from_header(_request_with_token(""), db)
    assert exc.value.status_code == 401

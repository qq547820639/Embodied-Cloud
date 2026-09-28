"""EdgeService 与边缘认证依赖测试 (临时 SQLite, 不 import app.main)."""

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.db import Base
from app.models import AgentStatus, EdgeAgent, TelemetryEvent
from app.security import hash_token
from app.services.edge import EdgeService, get_agent_from_header
from app.utils import utcnow


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
    from app.models import User

    owner = User(id="u1", email="u1@example.com", username="u1", password_hash="x")  # noqa: S106
    db.add(owner)
    db.commit()
    a1, _ = svc.register(db, "edge-01", {}, owner=owner)
    a2, _ = svc.register(db, "edge-02", {}, owner=owner)
    assert {a.id for a in svc.list_agents(db, owner)} == {a1.id, a2.id}
    assert svc.get_agent(db, a1.id, owner) is not None
    assert svc.get_agent(db, a1.id, owner).id == a1.id
    assert svc.get_agent(db, "missing", owner) is None


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


# ---------------------------------------------------------------------------
# §25/N-108：`online` 必须由最近一次心跳背书（此前 `AgentStatus.OFFLINE` 零写入者）
#
# 牙齿（六臂变异电池实测，2026-09-28；基线 0 红，各臂恢复后 `cmp` 逐字节相同、末跑 25 passed）：
# K1 摘掉 WHERE 里的状态条件 ⇒ 红在幂等那支与阈值那支（**不是**"从没通过话"那支——
#    REGISTERED 的保护来自 `last_heartbeat IS NULL`，状态列负责的是"别反复改口"）；
# K2 把 `<` 换成 `>` ⇒ 四支一起红（判反了方向）；K3 把阈值写死成 90 不读参数 ⇒ 只红阈值那支；
# K4 忘了 `db.commit()` ⇒ 红在幂等、阈值与"心跳回来"三支（都靠重读库）；
# K5 让组合根传裸数字 ⇒ 只红接线那支。
# K5 第一次跑是**存活**的：尺子当时用 `ast.dump(fn)` 找设置名，而闭包的 docstring 里就写着那个名字——
# 注释把代码的洞填平了。改成只看 `ast.Attribute` 的访问名之后 K5 才真红。这条教训写在尺子函数里。
# ---------------------------------------------------------------------------

THRESHOLD_SECONDS = 90


def _online_agent(factory, db, name: str, seconds_ago: int):
    """真走 register + heartbeat，再把 `last_heartbeat` 回拨——不靠手搓状态列。"""
    svc = EdgeService(factory)
    agent, _ = svc.register(db, name, {})
    svc.heartbeat(db, agent, {})
    agent.last_heartbeat = utcnow() - timedelta(seconds=seconds_ago)
    db.commit()
    return svc, agent


def test_stale_heartbeat_flips_online_to_offline_and_is_idempotent(factory, db) -> None:
    svc, agent = _online_agent(factory, db, "edge-old", THRESHOLD_SECONDS + 1)

    assert svc.expire_stale_agents(db, offline_after_seconds=THRESHOLD_SECONDS, now=utcnow()) == 1
    db.refresh(agent)
    assert agent.status == AgentStatus.OFFLINE.value, agent.status
    assert svc.expire_stale_agents(db, offline_after_seconds=THRESHOLD_SECONDS, now=utcnow()) == 0, (
        "第二趟又改了一次口（幂等塌了），或者第一次根本没落库"
    )


def test_a_fresh_heartbeat_is_not_offlined(factory, db) -> None:
    """不得开火那一极：阈值以内的心跳仍算在线——把活设备判死会让运维看不见它。"""
    svc, agent = _online_agent(factory, db, "edge-fresh", THRESHOLD_SECONDS - 1)

    assert svc.expire_stale_agents(db, offline_after_seconds=THRESHOLD_SECONDS, now=utcnow()) == 0
    db.refresh(agent)
    assert agent.status == AgentStatus.ONLINE.value


def test_the_threshold_is_the_argument_not_a_baked_in_number(factory, db) -> None:
    """两格分别在 89 s／91 s：阈值 90 只翻一格，阈值 60 翻两格。

    只测一档的话"读的是传进来的秒数"这句话证不了——写死 90 也能过第一遍。
    """
    svc, old = _online_agent(factory, db, "edge-91", THRESHOLD_SECONDS + 1)
    _fresh = _online_agent(factory, db, "edge-89", THRESHOLD_SECONDS - 1)[1]

    assert svc.expire_stale_agents(db, offline_after_seconds=THRESHOLD_SECONDS, now=utcnow()) == 1
    db.refresh(old)
    db.refresh(_fresh)
    assert (old.status, _fresh.status) == (AgentStatus.OFFLINE.value, AgentStatus.ONLINE.value)

    # 换一档参数 ⇒ 结果必须跟着动（否则上面那个 1 是常量不是判据）。
    # 此刻仍在线的是 edge-89（89 s）与刚造的 edge-70（70 s）：阈值降到 60 时两格都该翻。
    svc2, another = _online_agent(factory, db, "edge-70", 70)
    assert svc2.expire_stale_agents(db, offline_after_seconds=60, now=utcnow()) == 2
    db.refresh(_fresh)
    db.refresh(another)
    assert (_fresh.status, another.status) == (
        AgentStatus.OFFLINE.value,
        AgentStatus.OFFLINE.value,
    )


def test_an_agent_that_never_beat_is_not_flipped(factory, db) -> None:
    """REGISTERED（从没通过话）不许被写成 offline：那是一句"曾经在线后掉线"的假陈述。

    保护它的其实是 `last_heartbeat IS NULL`（NULL 不满足 `< threshold`），不是状态列那一条；
    状态列那一条负责的是"别把已经 OFFLINE 的行反复改口"——K1 臂把状态条件摘掉之后，
    红的是幂等与阈值那两支，而不是这一支（实测）。
    """
    svc = EdgeService(factory)
    agent, _ = svc.register(db, "edge-new", {})

    assert svc.expire_stale_agents(db, offline_after_seconds=1, now=utcnow()) == 0
    db.refresh(agent)
    assert agent.status == AgentStatus.REGISTERED.value


def test_a_later_heartbeat_brings_the_agent_back(factory, db) -> None:
    """掉线不是墓碑：心跳回来就必须重新是在线（否则这一列只是单向棘轮）。"""
    svc, agent = _online_agent(factory, db, "edge-back", THRESHOLD_SECONDS + 1)
    svc.expire_stale_agents(db, offline_after_seconds=THRESHOLD_SECONDS, now=utcnow())
    db.refresh(agent)
    assert agent.status == AgentStatus.OFFLINE.value

    svc.heartbeat(db, agent, {})
    db.refresh(agent)
    assert agent.status == AgentStatus.ONLINE.value
    assert agent.last_heartbeat is not None


# ---------------------------------------------------------------------------
# 接线：阈值只有一个来源（Settings），且扫描真的挂在周期表上
# ---------------------------------------------------------------------------

DEPS_SRC = Path(__file__).resolve().parents[1] / "app" / "deps.py"


def edge_sweep_wiring_offenders(source: str) -> list[str]:
    """`_expire_stale_edge_agents` 是否把配置键传进去、注册元组是否真的在册。"""
    offenders: list[str] = []
    tree = ast.parse(source)
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_expire_stale_edge_agents"),
        None,
    )
    if fn is None:
        return ["_expire_stale_edge_agents 不存在"]
    # 只看代码里的属性访问，不看 docstring：把设置名写在注释里曾让这把尺子
    # 在"参数被写死成 90"的 deps 上照样判 0 项（K5 臂实测就是这样存活的）。
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    if "edge_agent_offline_after_seconds" not in attrs:
        offenders.append("闭包没把 settings.edge_agent_offline_after_seconds 传进去")
    if "PERIODIC_EDGE_SWEEP_EVERY, _expire_stale_edge_agents" not in source:
        offenders.append("周期表里没有这一档注册")
    return offenders


def test_the_composition_root_wires_the_configured_threshold() -> None:
    assert edge_sweep_wiring_offenders(DEPS_SRC.read_text(encoding="utf-8")) == []


def test_the_wiring_ruler_fires_on_each_broken_shape() -> None:
    """尺子自己要有牙：两条判据各自能被单独打破，且合规形状判 0 项。

    注册那一半读的是整个文件，所以每个样本都自带一行注册（否则两条臂会一起红，
    读起来像"任何改动都同时打破两条"）。
    """
    reg = "tasks = [(OperationWorker.PERIODIC_EDGE_SWEEP_EVERY, _expire_stale_edge_agents)]\n"
    good = (
        "def _expire_stale_edge_agents():\n"
        "    return edge_service.expire_stale_agents(\n"
        "        db, offline_after_seconds=settings.edge_agent_offline_after_seconds\n"
        "    )\n"
    )
    assert edge_sweep_wiring_offenders(good + reg) == []
    assert edge_sweep_wiring_offenders(good) == ["周期表里没有这一档注册"]
    dropped_arg = "def _expire_stale_edge_agents():\n    return edge_service.expire_stale_agents(db)\n"
    assert edge_sweep_wiring_offenders(dropped_arg + reg) == [
        "闭包没把 settings.edge_agent_offline_after_seconds 传进去"
    ]
    hardcoded = (
        "def _expire_stale_edge_agents():\n"
        "    return edge_service.expire_stale_agents(db, offline_after_seconds=90)\n"
    )
    assert edge_sweep_wiring_offenders(hardcoded + reg) == [
        "闭包没把 settings.edge_agent_offline_after_seconds 传进去"
    ]
    assert edge_sweep_wiring_offenders("def other():\n    pass\n") == ["_expire_stale_edge_agents 不存在"]

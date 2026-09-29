"""掉线的设备要把它名下还挂着的运行一起收口（N-133，闭登记项 N-115）。

改前的形状：`running` 的唯一出口是设备回报（N-113 那条 `edge-run` 收口）与用户手工
`POST /deployments/{id}/complete`。设备被判离线之后两条都没了——那一行永远停在 `running`，
运维看到的是一次还在跑的机器人任务，而控制面早就再没听到过这台设备的任何声音。
N-115 当时列了三种形状：设备跑完即被掐（N-109 的 `reported=false` 档）、设备掉了而部署没重下、
以及人工在库外把部署推到 running。

修法把判决挂在**已有的**"最后一次被看见"证据上：`expire_stale_agents` 把 `online` 判成
`offline` 的那一趟，同一事务里用一条条件 UPDATE 收掉这些设备名下还 `running` 的部署
（WHERE 同时钉着归属与来源态，授权不在 Python 里判，与 `complete_from_agent_report` 同形）。
判成 `failed` 而不是退回 `pending`：借 K8s Job 的 `activeDeadlineSeconds`（超时 ⇒ `Failed`，
reason `DeadlineExceeded`）与 SLURM `--time` 那一档——超时的运行是一次已结束的失败，
重跑要人重新下部署，不由控制面私自重来一遍。

前提一律由真实生产者造：设备经 HTTP 端点注册、心跳、begin、报摘要，运行由用户侧 `run` 绑定；
只有"库外写进来的那条 running"（第五支）例外——那一档本来就没有 API 生产者，
手工插行正是在模拟运维/脚本直接改库。
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import deps
from app.deps import SessionFactory, edge_service
from app.main import app
from app.models import AgentStatus, DeploymentRecord, EdgeAgent, TelemetryEvent
from app.services.edge import RUN_TELEMETRY_KIND
from tests.test_edge_run_closes_deployment import _deploy, _report, _setup, _status, _to_running


def _beat(client: TestClient, st: dict) -> None:
    """真实心跳：把设备推到 `online` 并留下 `last_heartbeat` 这一格证据。"""
    r = client.post(
        f"/api/edge/agents/{st['agent_id']}/heartbeat",
        json={"device_info": {"os": "ubuntu"}},
        headers={"X-Agent-Token": st["agent_token"]},
    )
    assert r.status_code == 200, r.text


def _expire() -> None:
    """超时判决的驱动者：阈值取 0 秒，此刻的每一台 online 设备都已过期。"""
    with SessionFactory() as db:
        assert edge_service.expire_stale_agents(db, offline_after_seconds=0) >= 1


def _agent_status(agent_id: str) -> str:
    with SessionFactory() as db:
        return str(db.get(EdgeAgent, agent_id).status)


def test_the_offline_verdict_closes_the_run_bound_to_that_device():
    """一次超时判决之后，设备与其名下挂着的运行必须同时改口（同一趟 sweep）。"""
    with TestClient(app) as client:
        st = _setup(client, "offline-close-a@example.com")
        dep = _deploy(client, st)
        _beat(client, st)
        _to_running(client, st, dep)
        assert _status(dep["id"]) == ("running", None)

        _expire()

        assert _agent_status(st["agent_id"]) == AgentStatus.OFFLINE.value
        status, error = _status(dep["id"])
        assert status == "failed", status
        assert error == "device went offline before reporting the run", error


def test_a_late_success_report_does_not_resurrect_a_closed_run():
    """终态不被后到的读数改写：设备上线补报 ok=true，那一行还是 failed，遥测照旧留档。"""
    with TestClient(app) as client:
        st = _setup(client, "offline-close-b@example.com")
        dep = _deploy(client, st)
        _beat(client, st)
        _to_running(client, st, dep)
        _expire()
        assert _status(dep["id"])[0] == "failed"

        _report(client, st, dep["id"], ok=True, detail="franka finished after reconnect")

        status, error = _status(dep["id"])
        assert status == "failed" and error == "device went offline before reporting the run", (status, error)
        with SessionFactory() as db:
            kinds = [
                str(event.kind)
                for event in db.scalars(
                    select(TelemetryEvent).where(TelemetryEvent.edge_agent_id == st["agent_id"])
                )
            ]
        assert kinds == [RUN_TELEMETRY_KIND], kinds


def test_another_devices_running_deployment_is_not_touched():
    """归属半边要有极性：只收掉被判离线那台设备名下的运行，别的一动不动。"""
    with TestClient(app) as client:
        a = _setup(client, "offline-close-c@example.com")
        b = _setup(client, "offline-close-d@example.com")
        dep_a = _deploy(client, a)
        dep_b = _deploy(client, b)
        _beat(client, a)
        _beat(client, b)
        _to_running(client, a, dep_a)
        _to_running(client, b, dep_b)

        # 只让 A 过期：B 的心跳刚打过，阈值取"A 的旧于 30 秒"这一档。
        with SessionFactory() as db:
            row = db.get(EdgeAgent, a["agent_id"])
            row.last_heartbeat = row.last_heartbeat - timedelta(seconds=120)
            db.commit()
            closed = edge_service.expire_stale_agents(db, offline_after_seconds=60)
        assert closed == 1, closed

        assert _status(dep_a["id"])[0] == "failed"
        assert _status(dep_b["id"]) == ("running", None), "把别人的运行一起判掉＝越权收口"


def test_the_periodic_driver_reaches_the_closing_sweep(monkeypatch: pytest.MonkeyPatch):
    """没有周期驱动者，这条收口就只在有人手工叫 sweep 时才发生（N-93／N-99／N-110 的规矩）。"""
    with TestClient(app) as client:
        st = _setup(client, "offline-close-e@example.com")
        dep = _deploy(client, st)
        _beat(client, st)
        _to_running(client, st, dep)
        monkeypatch.setattr(deps.settings, "edge_agent_offline_after_seconds", 0, raising=False)

        assert deps._expire_stale_edge_agents() >= 1
        assert _status(dep["id"])[0] == "failed"


def test_a_running_row_bound_to_nothing_is_not_judged():
    """未知不等于缺席：没绑定设备的 running（库外写进来的那一档）留给那条登记项，不私判。"""
    with TestClient(app) as client:
        st = _setup(client, "offline-close-f@example.com")
        dep = _deploy(client, st)
        _beat(client, st)
        _to_running(client, st, dep)
        with SessionFactory() as db:
            row = db.get(DeploymentRecord, dep["id"])
            row.edge_agent_id = None  # 运维/脚本直接改库的形状：这一档没有 API 生产者
            row.status = "running"
            db.commit()

        _expire()

        assert _status(dep["id"]) == ("running", None), (
            "没有'最后一次被看见'的证据列可依据，把'没人报'读成'设备没了'就是 ADR 0008 禁止的那一步"
        )


def test_a_closed_success_run_is_not_rewritten_when_the_device_later_goes_stale():
    """收口只碰还挂着的运行：已经报过成功的行，设备之后掉线也不该被改写成失败。

    这条是那条 UPDATE 的来源态谓词的极性——少了 `status == running`，同一趟 sweep
    会把终态一起翻掉（正是本仓反复禁止的"后到的判决改写已结束的判决"）。
    """
    with TestClient(app) as client:
        st = _setup(client, "offline-close-g@example.com")
        dep = _deploy(client, st)
        _beat(client, st)
        _to_running(client, st, dep)
        _report(client, st, dep["id"], ok=True, detail="franka done")
        assert _status(dep["id"]) == ("success", None)

        _expire()

        assert _agent_status(st["agent_id"]) == AgentStatus.OFFLINE.value
        assert _status(dep["id"]) == ("success", None), "设备之后的掉线不许把已成功的运行翻成失败"


def test_the_run_telemetry_name_is_still_the_shared_wire_constant():
    """跨包线协议常量：本文件用的 kind 必须还是服务侧那一个（防有人把字符串抄成第二份事实）。"""
    from app.services import edge as edge_mod

    assert RUN_TELEMETRY_KIND == edge_mod.RUN_TELEMETRY_KIND == "edge-run"

"""ADR 0007 的后半句终于有了实现者：设备回报 → 控制面收口（N-113）。

ADR 0007 的后果段写着"设备把运行结果作为 `kind=edge-run` 的遥测回传，控制面据此收口"，
而 app/ 里从头到尾没有任何一处读这条遥测：`DeploymentStatus.RUNNING` 的唯一出口是
用户手工 `POST /api/deployments/{id}/complete`（`app/routers/deployments.py:129`）。
设备能做的三件事（begin → 取件 → 报摘要 → 上机 → 回报）里，最后一报落在
`telemetry_events` 表里就到此为止——一条真跑完的部署会永远挂在 RUNNING，
而页面上那句"运行结果运维看 `GET /api/edge/agents/{id}/telemetry`"只是看得见，没人据此改判。

这一轮把收口接在遥测写入的同一路径上，判据分两组：
- **会收口的**：RUNNING 且绑定给这台设备、payload 点名它 ⇒ 写终态；
- **不许收口的四种**：别的设备的部署（越权）、还没 run（状态不对）、后到的第二条
  （终态不被改写）、别的 kind（与部署无关）。
授权与幂等都不在 Python 里判，而是同一条条件 UPDATE 的 WHERE
（`id + status=running + edge_agent_id`），与本文件用的 `begin_agent_download` 同一形状。

全部走真 HTTP 端点 + 真产物，不手工塞库——越权那一支尤其要真拿 B 设备的 token 去打。
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.deps import SessionFactory
from app.main import app
from app.models import DeploymentRecord, TelemetryEvent
from app.services.edge import RUN_TELEMETRY_KIND
from tests.http_auth import auth_headers, register_body

ROBOT = "franka"


def _setup(client: TestClient, email: str) -> dict:
    """用户 + 设备 + workspace + 真产物（与 tests/test_edge_agent_api.py 同一手法）。"""
    body = register_body(client, email, email.split("@")[0])
    headers = auth_headers(body["token"])
    agent = client.post("/api/edge/agents/register", json={"name": f"arm-{email}"}, headers=headers)
    assert agent.status_code == 201, agent.text
    ws = client.post(
        "/api/workspaces", json={"template_id": "cartpole", "auto_start": False}, headers=headers
    )
    assert ws.status_code == 201, ws.text
    wid = ws.json()["id"]
    checkpoint = client.post(f"/api/workspaces/{wid}/demo-checkpoint", headers=headers)
    assert checkpoint.status_code == 200, checkpoint.text
    return {
        "headers": headers,
        "agent_id": agent.json()["agent"]["id"],
        "agent_token": agent.json()["token"],
        "workspace_id": wid,
        "path": checkpoint.json()["path"],
        "sha256": checkpoint.json()["sha256"],
    }


def _deploy(client: TestClient, st: dict) -> dict:
    resp = client.post(
        "/api/deployments",
        json={
            "workspace_id": st["workspace_id"],
            "artifact_path": st["path"],
            "robot_type": ROBOT,
            "edge_agent_id": st["agent_id"],
        },
        headers=st["headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _to_running(client: TestClient, st: dict, dep: dict) -> None:
    """pending → downloading → verified（设备侧）→ running（用户侧 run 绑定执行设备）。"""
    agent = {"X-Agent-Token": st["agent_token"]}
    begun = client.post(
        f"/api/edge/agents/{st['agent_id']}/deployments/{dep['id']}/begin", json={}, headers=agent
    )
    assert begun.status_code == 200, begun.text
    reported = client.post(
        f"/api/deployments/{dep['id']}/report-checksum",
        json={"actual_sha256": st["sha256"]},
        headers=agent,
    )
    assert reported.status_code == 200, reported.text
    run = client.post(
        f"/api/deployments/{dep['id']}/run",
        json={"edge_agent_id": st["agent_id"]},
        headers=st["headers"],
    )
    assert run.status_code == 200, run.text
    assert run.json()["status"] == "running", run.text


def _report(client: TestClient, st: dict, dep_id: str, *, ok: bool = True, detail: str = "") -> dict:
    resp = client.post(
        f"/api/edge/agents/{st['agent_id']}/telemetry",
        json={"kind": RUN_TELEMETRY_KIND, "payload": {"deployment_id": dep_id, "ok": ok, "detail": detail}},
        headers={"X-Agent-Token": st["agent_token"]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _status(dep_id: str) -> tuple[str, str | None]:
    with SessionFactory() as db:
        row = db.get(DeploymentRecord, dep_id)
        return str(row.status), row.error_message if row.error_message is None else str(row.error_message)


def test_a_run_report_closes_the_deployment_it_names():
    """ADR 的那半句话：ok=true 的回报把 RUNNING 写成 SUCCESS，且遥测事件照旧留档。"""
    with TestClient(app) as client:
        st = _setup(client, "run-close-a@example.com")
        dep = _deploy(client, st)
        _to_running(client, st, dep)

        event = _report(client, st, dep["id"], detail="franka ran model.pt")

        assert _status(dep["id"]) == ("success", None)
        seen = client.get(
            f"/api/edge/agents/{st['agent_id']}/telemetry", headers=st["headers"]
        ).json()
        assert [t["kind"] for t in seen].count(RUN_TELEMETRY_KIND) == 1, seen
        assert event["payload"]["deployment_id"] == dep["id"]


def test_a_failed_run_report_writes_failed_with_the_devices_own_words():
    with TestClient(app) as client:
        st = _setup(client, "run-close-b@example.com")
        dep = _deploy(client, st)
        _to_running(client, st, dep)

        _report(client, st, dep["id"], ok=False, detail="joint torque out of range")

        status, error = _status(dep["id"])
        assert status == "failed", status
        assert error == "joint torque out of range", error


def test_a_report_naming_another_devices_deployment_changes_nothing():
    """越权：payload 里的 id 是设备可写的，授权只能看**行**上的归属列。"""
    with TestClient(app) as client:
        mine = _setup(client, "run-victim@example.com")
        attacker = _setup(client, "run-attacker@example.com")
        dep = _deploy(client, mine)
        _to_running(client, mine, dep)

        _report(client, attacker, dep["id"])  # 拿自己的 token 报别人的部署

        # 拒绝的是"改别人的状态"，不是"不许上报"：攻击者那一条照样进遥测表，
        # 而受害者的那一行原样不动。
        assert _status(dep["id"]) == ("running", None), "别的设备的回报不许改这一行"


def test_a_second_report_cannot_rewrite_a_terminal_state():
    """幂等：终态一旦写下，后到的相反结论改不动它（改判的门在 status=running 那一列上）。"""
    with TestClient(app) as client:
        st = _setup(client, "run-twice@example.com")
        dep = _deploy(client, st)
        _to_running(client, st, dep)

        _report(client, st, dep["id"], ok=True)
        _report(client, st, dep["id"], ok=False, detail="later contradiction")

        assert _status(dep["id"]) == ("success", None)
        with SessionFactory() as db:
            kept = db.scalars(
                select(TelemetryEvent).where(TelemetryEvent.edge_agent_id == st["agent_id"])
            ).all()
        assert len([e for e in kept if e.kind == RUN_TELEMETRY_KIND]) == 2, "两条都该留档"


def test_a_report_before_run_is_not_a_way_to_fake_success():
    """没 run 过就没有 RUNNING 可收：verified 状态的原行不许被凭空判成 success。"""
    with TestClient(app) as client:
        st = _setup(client, "run-premature@example.com")
        dep = _deploy(client, st)
        agent = {"X-Agent-Token": st["agent_token"]}
        client.post(f"/api/edge/agents/{st['agent_id']}/deployments/{dep['id']}/begin", json={}, headers=agent)
        client.post(
            f"/api/deployments/{dep['id']}/report-checksum",
            json={"actual_sha256": st["sha256"]},
            headers=agent,
        )
        assert _status(dep["id"])[0] == "verified"

        _report(client, st, dep["id"])

        assert _status(dep["id"]) == ("verified", None)


def test_other_telemetry_kinds_leave_deployments_alone():
    with TestClient(app) as client:
        st = _setup(client, "run-other-kind@example.com")
        dep = _deploy(client, st)
        _to_running(client, st, dep)

        posted = client.post(
            f"/api/edge/agents/{st['agent_id']}/telemetry",
            json={"kind": "joint_state", "payload": {"deployment_id": dep["id"], "ok": True}},
            headers={"X-Agent-Token": st["agent_token"]},
        )
        assert posted.status_code == 200, posted.text
        assert _status(dep["id"]) == ("running", None)


def test_run_kind_wire_name_matches_the_device_spelling():
    """两个包刻意不共享常量（设备包不许 import 服务端），那相等就得有判据。"""
    import re
    from pathlib import Path

    device = Path("edge_agent/agent.py").read_text(encoding="utf-8")
    found = re.search(r'^RUN_TELEMETRY_KIND = "([^"]+)"', device, re.MULTILINE)
    assert found, "设备侧那份常量不见了（改名的话这里的判据要一起改）"
    assert found.group(1) == RUN_TELEMETRY_KIND, (found.group(1), RUN_TELEMETRY_KIND)

"""控制面要答得出「这台设备此刻在跑哪一条」（N-140，闭登记项 N-139）。

改前的形状：设备只有两种话可说——`edge-run`（结果，顺带把 `running` 收成终态）与
无差别的心跳（N-138 之后连物理运行期间都在发，但它只说"我在"，不说"我在做什么"）。
于是 `GET /api/edge/agents` 上「在线」与「在线且正在跑一条 20 分钟的巡检」同形，
运维要反推只能去 `deployments` 表按 `edge_agent_id` 猜。

形状选的是**投影而不是新列**：设备在按下驱动之前多写一条 append-only 的
`edge-run-started` 事实声明，控制面用「开跑过 − 已经报完」这个集合差把答案投影出来。
为什么不加 `edge_agents.current_deployment_id` 那一列：它会变成第二个「运行进展」的权威，
还得靠设备记得清它（设备崩在半路就永远挂着一条不存在的运行）；而投影读的是设备
确实写进 `telemetry_events` 的东西，`deployments.status` 这一个字都没动。

三条判据级细节各有用例钉着：
- 声明**与人为闸无关**：`_handle` 在报完摘要后直接上机，那时人还没按 `/run`，
  所以投影绝不能要求 `status == running`（要求了就常年读 null，那是假读数）；
- 但**权威的收口要作废声明**：那行一旦是 `success`／`failed`（设备自报、掉线收口 N-133、
  超时判决 N-136 三条路都算），这条投影就不该再指着它；
- 不可判一律答 null：两条未闭合并存、声明的行不归这台设备（越权）、翻过扫描窗——
  宁可不答，不替一台单线程设备猜一个。

前提全部由真实生产者造：注册、心跳、部署、`/run`、遥测都走 HTTP 端点；
设备侧那两支用真 `EdgeAgentRuntime`＋真 `MockRobotDriver`，只换网络那一层。
"""

import re
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.deps import SessionFactory, deployment_service
from app.main import app
from app.services.edge import RUN_START_TELEMETRY_KIND, RUN_TELEMETRY_KIND
from app.utils import utcnow
from edge_agent import agent as device_mod
from tests.test_edge_agent_unreported_run import SHA, _FakeControlPlane, _runtime
from tests.test_edge_run_closes_deployment import _deploy, _report, _setup, _status, _to_running
from tests.test_gpu_drain_provenance import row_template
from tests.test_run_deadline_closes_runs import _deploy_with_budget

ROOT = Path(__file__).resolve().parents[1]


def _announce(client: TestClient, st: dict, dep_id: str, extra: dict | None = None) -> None:
    """设备侧那一条"我要开始跑这一格了"，走真端点。"""
    payload = {"deployment_id": dep_id, "driver": "mock"}
    payload.update(extra or {})
    resp = client.post(
        f"/api/edge/agents/{st['agent_id']}/telemetry",
        json={"kind": RUN_START_TELEMETRY_KIND, "payload": payload},
        headers={"X-Agent-Token": st["agent_token"]},
    )
    assert resp.status_code == 200, resp.text


def _agent_face(client: TestClient, st: dict, agent_id: str) -> str | None:
    """列表面上这台设备的 `current_deployment_id`（读端拿到的形状，不直连服务）。"""
    rows = client.get("/api/edge/agents", headers=st["headers"]).json()
    mine = [row for row in rows if row["id"] == agent_id]
    assert len(mine) == 1, rows
    return mine[0]["current_deployment_id"]


def test_the_two_packages_agree_on_both_wire_names() -> None:
    """跨包线协议常量：开跑与回报两条都必须同源（设备包刻意不 import 服务端）。"""
    assert device_mod.RUN_START_TELEMETRY_KIND == RUN_START_TELEMETRY_KIND == "edge-run-started"
    assert device_mod.RUN_TELEMETRY_KIND == RUN_TELEMETRY_KIND == "edge-run"


def test_an_announced_run_is_readable_before_anyone_presses_run():
    """投影读的是设备的声明，不是人为闸：那时 `deployments.status` 还是 verified。

    这一支是本件的正身——若实现去要求 `status == running`，这里会读到 null，
    而 null 在管理台上与"这台设备没在跑东西"同形。
    """
    with TestClient(app) as client:
        st = _setup(client, "current-run-a@example.com")
        dep = _deploy(client, st)
        _announce(client, st, dep["id"])

        # 前提：这行既没被设备取件也没被人 `/run`——它还是 pending。
        # 投影答的是"设备说过什么"，与那道人为闸无关（见 services.edge.announced_run_id 的说明段）。
        assert _status(dep["id"])[0] == "pending"
        assert _agent_face(client, st, st["agent_id"]) == dep["id"]


def test_the_completion_report_retires_the_claim():
    """报完就撤：`edge-run` 一到，这一格回到 null（同一条记录已被权威收口）。"""
    with TestClient(app) as client:
        st = _setup(client, "current-run-b@example.com")
        dep = _deploy(client, st)
        _announce(client, st, dep["id"])
        _to_running(client, st, dep)
        _report(client, st, dep["id"], ok=True, detail="franka done")

        assert _status(dep["id"]) == ("success", None)
        assert _agent_face(client, st, st["agent_id"]) is None


def test_a_claim_pointing_at_a_closed_row_is_not_shown_as_current():
    """权威先收的口（这里是 N-136 的超时判决），要让还留在遥测里的那条声明作废。"""
    with TestClient(app) as client:
        st = _setup(client, "current-run-c@example.com")
        dep = _deploy_with_budget(client, st, 1)  # 带预算，才让 N-136 那条 sweep 收得了它
        _announce(client, st, dep["id"])
        _to_running(client, st, dep)
        assert _agent_face(client, st, st["agent_id"]) == dep["id"]

        with SessionFactory() as db:
            deployment_service.fail_overdue_runs(db, now=utcnow() + timedelta(hours=2))
        assert _status(dep["id"])[0] == "failed"

        assert _agent_face(client, st, st["agent_id"]) is None, (
            "记录已经终态了还在说『在跑』＝把一台空闲设备读成忙碌设备"
        )


def test_two_open_claims_are_not_resolved_by_guessing():
    """单线程设备出现两条未闭合声明＝状态本身可疑，这里答 null 而不是挑一条。"""
    with TestClient(app) as client:
        st = _setup(client, "current-run-d@example.com")
        first = _deploy(client, st)
        second = client.post(
            "/api/deployments",
            json={
                "workspace_id": st["workspace_id"],
                "artifact_path": st["path"],
                "robot_type": "ur5",  # 幂等键含 robot_type ⇒ 同一件产物、第二条记录
                "edge_agent_id": st["agent_id"],
            },
            headers=st["headers"],
        )
        assert second.status_code == 201, second.text
        _announce(client, st, first["id"])
        _announce(client, st, second.json()["id"])

        assert _agent_face(client, st, st["agent_id"]) is None


def test_another_devices_claim_neither_shows_nor_blanks_its_own():
    """越权半边：B 声明 A 的那条，既不该显示在 B 的格上，也不该把 B 自己的声明顶掉。"""
    with TestClient(app) as client:
        a = _setup(client, "current-run-e@example.com")
        b = _setup(client, "current-run-f@example.com")
        dep_a = _deploy(client, a)
        dep_b = _deploy(client, b)
        _announce(client, b, dep_a["id"])  # B 冒名声明 A 的记录

        assert _agent_face(client, b, b["agent_id"]) is None, "别人的记录不能算在 B 头上"

        _announce(client, b, dep_b["id"])
        assert _agent_face(client, b, b["agent_id"]) == dep_b["id"], (
            "一条越权声明不该把 B 自己那条真声明挤成 null"
        )
        assert _agent_face(client, a, a["agent_id"]) is None, (
            "A 没说过自己在跑，冒名者替它说也不算"
        )


def test_every_face_reports_the_same_answer():
    """四个面必须同源：心跳、列表、详情在同一次声明下给出同一个值。

    漏算任何一个面，那一面的 null 就会被读成"没在跑"——`agent_out` 是唯一生产者，
    这一支是它真的被四个面共用的证据。
    """
    with TestClient(app) as client:
        st = _setup(client, "current-run-g@example.com")
        dep = _deploy(client, st)
        beat = client.post(
            f"/api/edge/agents/{st['agent_id']}/heartbeat",
            json={"device_info": {"os": "ubuntu"}},
            headers={"X-Agent-Token": st["agent_token"]},
        )
        assert beat.status_code == 200, beat.text
        assert beat.json()["current_deployment_id"] is None

        _announce(client, st, dep["id"])
        heartbeat = client.post(
            f"/api/edge/agents/{st['agent_id']}/heartbeat",
            json={"device_info": {"os": "ubuntu"}},
            headers={"X-Agent-Token": st["agent_token"]},
        ).json()
        detail = client.get(
            f"/api/edge/agents/{st['agent_id']}", headers=st["headers"]
        ).json()["current_deployment_id"]
        assert heartbeat["current_deployment_id"] == dep["id"], heartbeat
        assert detail == dep["id"], detail
        assert _agent_face(client, st, st["agent_id"]) == dep["id"]


def test_no_route_validates_the_agent_model_by_itself() -> None:
    """静态尺：`EdgeAgentOut.model_validate` 只许出现在唯一生产者里。

    派生值不在 ORM 上，任何一处"顺手 model_validate 一下"都会让那一面的这一格永远是 null，
    而且绿得看不出问题——所以这条按**源码面**判，不按行为判。
    """
    sites = {}
    for path in list((ROOT / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        hits = len(re.findall(r"EdgeAgentOut\.model_validate", text))
        if hits:
            sites[str(path.relative_to(ROOT))] = hits
    assert sites == {"app/services/edge.py": 1}, f"EdgeAgentOut 的生产者不唯一：{sites}"
    service = (ROOT / "app" / "services" / "edge.py").read_text(encoding="utf-8")
    producer = service[service.index("def agent_out") : service.index("def agent_out") + 900]
    assert "EdgeAgentOut.model_validate(agent)" in producer
    assert "announced_run_id" in producer, "生产者必须自己算那一格，别指望 model_validate 带出来"
    faces = (ROOT / "app" / "routers" / "edge.py").read_text(encoding="utf-8")
    assert faces.count("edge_service.agent_out(") == 4, "register／heartbeat／list／detail 四处都要走生产者"


def test_the_device_announces_before_it_moves_and_reports_once(tmp_path: Path) -> None:
    """设备侧顺序与形状：先声明开跑，再装载，再跑；两种 kind 各一条。"""
    ctrl = _FakeControlPlane()
    driver = device_mod.MockRobotDriver()
    ctrl.list_assigned = lambda agent_id: [  # type: ignore[method-assign]
        {
            "id": "d-1",
            "status": "pending",
            "checksum": SHA,
            "robot_type": "franka",
            "run_deadline_seconds": 900,
        }
    ]
    outcome = _runtime(ctrl, tmp_path, driver).run_once()

    assert [p["kind"] for p in ctrl.payloads] == [
        RUN_START_TELEMETRY_KIND,
        RUN_TELEMETRY_KIND,
    ], ctrl.payloads
    started, done = ctrl.payloads
    assert started["deployment_id"] == "d-1" and done["deployment_id"] == "d-1"
    assert started["driver"] == "mock" and started["robot_type"] == "franka"
    assert started["run_deadline_seconds"] == 900, "设备把它认领的预算一起报上去，读的人才知道它在守哪个数"
    assert outcome[0].start_reported is True and outcome[0].action == "ran"


def test_a_lost_announcement_does_not_stop_the_robot_but_speaks_up(tmp_path: Path) -> None:
    """开跑声明送不出去也照跑：一次物理运行的价值高于一条遥测，但这件事必须被记录。"""
    ctrl = _FakeControlPlane()

    original = ctrl.telemetry

    def telemetry(agent_id: str, kind: str, payload: dict) -> dict:
        if kind == RUN_START_TELEMETRY_KIND:
            raise device_mod.AgentClientError(503, "http://control.example/api/edge/telemetry", "down")
        return original(agent_id, kind, payload)

    ctrl.telemetry = telemetry  # type: ignore[method-assign]
    driver = device_mod.MockRobotDriver()
    outcome = _runtime(ctrl, tmp_path, driver).run_once()[0]

    assert outcome.action == "ran" and driver.runs == 1, "遥测坏了不该让机器人停下"
    assert outcome.start_reported is False
    assert "开跑声明未送达" in outcome.detail, outcome.detail
    assert outcome.as_json()["start_reported"] is False

    import edge_agent.__main__ as agent_main

    assert agent_main._exit_code([outcome]) == 1, "控制面这段时间读不到它在跑什么，冒烟跑不许绿"


def test_the_agent_row_shows_the_current_run_or_says_there_is_no_claim() -> None:
    """管理台那一格：读得到 `current_deployment_id`，且 null 有独立说法。

    「无开跑声明」而不是「未在运行」：投影答 null 的原因有好几种（没声明、越权、
    两条并存、翻出窗），任何一种都不等于"这台设备肯定闲着"——措辞不能替它下那个结论。
    """
    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    _, body = row_template(js, "agents")
    assert "current_deployment_id" in body, "设备行没渲染这一格，N-139 要的读端还是缺"
    assert "无开跑声明" in body, "null 没有自己的说法：不可判会被读成『确实没在跑』"
    head = next(line for line in js[js.index("async function loadAgents") :].splitlines() if "<thead>" in line)
    assert "<th>在跑哪一条</th>" in head, "表头没这一列，单元格数会比表头多一个"
    assert body.count("<td") == head.count("<th>"), (
        f"表头 {head.count('<th>')} 格而行里 {body.count('<td')} 格：整表会错位"
    )

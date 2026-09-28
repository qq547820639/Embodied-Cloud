"""设备端「机器人动过了，但没有任何人知道」的两条形状（N-109）。

教义与服务端那条同形：**一次动作的后果要由事实背书**，而"没报错"既不能背书
"它发生了"，也不能背书"它没发生"。这里钉两处：

1. `edge_agent/agent.py` 里 `telemetry` 失败：机器人已经真跑过，异常若一路抛到
   `run_once` 的通用分支，那一格会被折成 `("error", status_before, status_before)`
   ——读起来跟"取件之前就失败了"一模一样，观察结果连同"跑过"这件事一起消失。
2. `edge_agent/__main__.py` 的退出码：一次没人知道的物理运行必须让冒烟跑变红，
   而不是往 stderr 喊一嗓子之后退 0。

夹具用真 `MockRobotDriver`（它自带 `runs` 计数）和一个 `AgentClient` 子类当假控制面
——**假在网络那一层，不假在编排上**：`_handle` 的 begin → 取件 → 报摘要 → 上机整条
推进都真走一遍。发现响应那两支反过来：真 `AgentClient` 真解析，只有 `urlopen` 是假的。
"""

from pathlib import Path

import pytest

import edge_agent.__main__ as agent_main
import edge_agent.client as client_mod
from edge_agent.agent import EdgeAgentRuntime
from edge_agent.client import AgentClient, AgentClientError, ArtifactFetch
from edge_agent.drivers import MockRobotDriver
from tests.test_edge_agent_client import TOKEN, _Resp

ARTIFACT = b"weights-" + b"y" * 64
SHA = "sha-of-the-artifact"
RECORD = {"id": "d-1", "status": "pending", "checksum": SHA, "robot_type": "franka"}


class _FakeControlPlane(AgentClient):
    """每个方法返回真端点会返回的那个形状，并记下被调用的顺序。"""

    def __init__(self, *, telemetry_error: Exception | None = None) -> None:
        super().__init__("http://control.example", TOKEN)
        self.telemetry_error = telemetry_error
        self.calls: list[str] = []
        self.payloads: list[dict] = []

    def heartbeat(self, agent_id: str, device_info: dict | None = None) -> dict:
        self.calls.append("heartbeat")
        return {"agent": {"id": agent_id, "status": "online"}}

    def list_assigned(self, agent_id: str) -> list[dict]:
        self.calls.append("list_assigned")
        return [dict(RECORD)]

    def begin(self, agent_id: str, deployment_id: str) -> dict:
        self.calls.append("begin")
        return {"id": deployment_id, "status": "downloading"}

    def fetch_artifact(
        self, deployment_id: str, dest: Path, expected_sha256: str | None = None
    ) -> ArtifactFetch:
        self.calls.append("fetch_artifact")
        dest.write_bytes(ARTIFACT)
        return ArtifactFetch(path=dest, sha256=SHA, size_bytes=len(ARTIFACT))

    def report_checksum(self, deployment_id: str, actual_sha256: str) -> dict:
        self.calls.append("report_checksum")
        return {"id": deployment_id, "status": "verified", "robot_type": "franka"}

    def telemetry(self, agent_id: str, kind: str, payload: dict) -> dict:
        self.calls.append("telemetry")
        self.payloads.append({"kind": kind, **payload})
        if self.telemetry_error is not None:
            raise self.telemetry_error
        return {"id": "evt-1"}


def _runtime(client: AgentClient, workdir: Path, driver: MockRobotDriver) -> EdgeAgentRuntime:
    return EdgeAgentRuntime(
        server="http://control.example",
        token=TOKEN,
        agent_id="a-1",
        workdir=workdir,
        driver=driver,
        client=client,
    )


LOST = AgentClientError(503, "http://control.example/api/edge/agents/a-1/telemetry", "upstream unavailable")


# ----------------------------------------------------------------------
# 1) 上报丢了，但运行发生过
# ----------------------------------------------------------------------
def test_a_lost_report_does_not_erase_that_the_robot_ran(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    ctrl = _FakeControlPlane(telemetry_error=LOST)
    driver = MockRobotDriver()

    outcomes = _runtime(ctrl, tmp_path, driver).run_once()

    assert len(outcomes) == 1, outcomes
    outcome = outcomes[0]
    # 关键极性：这一格**不是** ("error", "pending", "pending")——那是"什么都没发生"的读数。
    assert (outcome.action, outcome.status_before, outcome.status_after) == ("ran", "pending", "verified")
    assert outcome.reported is False
    assert outcome.observation is not None and outcome.observation.ok is True
    assert driver.runs == 1, "驱动只该动一次，上报失败不该让它重跑"
    assert "上报失败" in outcome.detail and "upstream unavailable" in outcome.detail
    assert outcome.as_json()["reported"] is False

    err = capsys.readouterr().err
    assert "已运行，但结果未能上报" in err and "d-1" in err, err
    assert TOKEN not in err, "脱敏底线：token 不进任何输出面"


def test_a_delivered_report_stays_quiet_and_reports_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """不开火对照：上报成功时既没有 stderr，也没有 reported=False。"""
    ctrl = _FakeControlPlane()
    driver = MockRobotDriver()

    outcome = _runtime(ctrl, tmp_path, driver).run_once()[0]

    assert (outcome.action, outcome.status_after, outcome.reported) == ("ran", "verified", True)
    assert "上报失败" not in outcome.detail
    assert ctrl.payloads[0]["deployment_id"] == "d-1" and ctrl.payloads[0]["ok"] is True
    assert capsys.readouterr().err == ""


def test_the_run_is_not_repeated_when_the_report_failed(tmp_path: Path):
    """下一轮不得补跑：`fetched` 为空 ⇒ 走 skipped，遥测失败不引入重复上机。"""
    ctrl = _FakeControlPlane(telemetry_error=LOST)
    ctrl.list_assigned = lambda agent_id: [{"id": "d-1", "status": "verified", "checksum": SHA}]  # type: ignore[method-assign]
    driver = MockRobotDriver()

    outcome = _runtime(ctrl, tmp_path, driver).run_once()[0]

    assert (outcome.action, outcome.status_after) == ("skipped", "verified")
    assert driver.runs == 0
    assert ctrl.calls == ["heartbeat"], "上一格失败过的部署，下一轮不得再上机"


# ----------------------------------------------------------------------
# 2) 退出码
# ----------------------------------------------------------------------
def _main(monkeypatch: pytest.MonkeyPatch, outcomes: list[object]) -> int:
    """把编排换成"这一轮就产出这几格"，只为了量 `main()` 自己的退出码判据。"""

    def fake_loop(self: EdgeAgentRuntime, **_kwargs: object) -> list[object]:
        return outcomes

    monkeypatch.setattr(EdgeAgentRuntime, "loop", fake_loop)
    return agent_main.main(
        ["--server", "http://control.example", "run", "--agent-id", "a-1", "--token", "t"]
    )


def test_smoke_exit_code_is_non_zero_when_a_run_was_unreported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """运行过而没人知道 ⇒ 冒烟必须红。退 0 等于把这一格混进绿灯里。"""
    ctrl = _FakeControlPlane(telemetry_error=LOST)
    outcome = _runtime(ctrl, tmp_path, MockRobotDriver()).run_once()[0]

    assert _main(monkeypatch, [outcome]) == 1


def test_smoke_exit_code_stays_zero_on_a_reported_round(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    ctrl = _FakeControlPlane()
    outcome = _runtime(ctrl, tmp_path, MockRobotDriver()).run_once()[0]

    assert outcome.reported is True
    assert _main(monkeypatch, [outcome]) == 0


# ----------------------------------------------------------------------
# 3) 发现响应的形状要能顶到编排层（真客户端 + 假 urlopen）
# ----------------------------------------------------------------------
def _serve_each(monkeypatch: pytest.MonkeyPatch, bodies: list[bytes]) -> None:
    """每次请求换一个响应体：`run_once` 会先心跳再发现，共用一个体会被读空。"""
    pages = iter(bodies)
    monkeypatch.setattr(client_mod, "urlopen", lambda req, timeout=None: _Resp(next(pages)))


def test_a_malformed_discovery_response_is_not_an_empty_round(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """修前形状：`{"detail": ...}` → `[]` → 一轮无事发生 → 退出码 0 → 页面显示在线空闲。"""
    _serve_each(monkeypatch, [b'{"agent": {"status": "online"}}', b'{"detail": "gateway"}'])
    runtime = _runtime(AgentClient("http://control.example", "t"), tmp_path, MockRobotDriver())

    with pytest.raises(AgentClientError, match="not a JSON array"):
        runtime.run_once()


def test_a_malformed_discovery_response_exits_non_zero_and_on_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """整条链：假 `urlopen` → 真客户端 → 真编排 → 真 `main()`，红要有退出码也要有话。"""
    _serve_each(monkeypatch, [b'{"agent": {"status": "online"}}', b'{"detail": "gateway"}'])

    rc = agent_main.main(
        [
            "--server", "http://control.example", "run",
            "--agent-id", "a-1", "--token", TOKEN, "--workdir", str(tmp_path), "--iterations", "1",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "无法与控制面通信" in err and "not a JSON array" in err, err
    assert TOKEN not in err
    assert capsys.readouterr().out == "", "错误不得走 stdout——`--json` 的消费方会把它当数据"


def test_an_empty_discovery_list_is_still_a_calm_idle_round(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """不开火对照：真的没活 ⇒ 空清单、不抛、什么都不做。"""
    _serve_each(monkeypatch, [b'{"agent": {"status": "online"}}', b"[]"])
    runtime = _runtime(AgentClient("http://control.example", "t"), tmp_path, MockRobotDriver())

    assert runtime.run_once() == []

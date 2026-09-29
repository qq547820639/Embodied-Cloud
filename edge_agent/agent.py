"""EdgeAgentRuntime：设备侧一轮工作的编排（心跳 → 发现 → 取件 → 核对 → 上报 → 驱动）。

状态机分工（ADR 0007 的裁决，写在这儿免得下次重新推一遍）：

| 迁移 | 由谁发起 | 端点 |
|---|---|---|
| pending → downloading | **设备** | `POST /edge/agents/{id}/deployments/{dep}/begin` |
| （取字节） | **设备** | `GET /deployments/{dep}/artifact` |
| downloading → verified / failed | **设备** | `POST /deployments/{dep}/report-checksum` |
| verified → running → success/failed | 控制面 | `POST /deployments/{dep}/run`、`/complete` |

后半段不给设备是有理由的：`run` 要绑 GPU 工作区、`complete` 要结算账本，属主不是
机器人；设备把运行结果作为遥测（`kind=edge-run`）回传，控制面据此收口。
"""

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .client import AgentClient, AgentClientError
from .drivers import MockRobotDriver, RobotDriver, RobotObservation
from .keepalive import RunKeepalive

RUN_TELEMETRY_KIND = "edge-run"
#: 设备侧还能推进的状态；其余（running/success/failed）归控制面。
DEVICE_ADVANCEABLE = ("pending", "downloading", "verified")
#: 运行期间的心跳间隔。判活阈值（`edge_agent_offline_after_seconds`，默认 90 s）除以它
#: 留 6 次机会，而单次请求自己的超时是 30 s——慢一次也还在窗口内。
#: 这个数不是"越小越好"：它要保证的是**判活窗口里至少能落进一次心跳**。
RUN_HEARTBEAT_SECONDS = 15.0


@dataclass(frozen=True)
class RoundOutcome:
    deployment_id: str
    action: str
    status_before: str
    status_after: str
    sha256: str = ""
    detail: str = ""
    observation: RobotObservation | None = None
    #: 控制面是否知道这一格的处置。`False` 只有一种来源：机器人已经动过而上报失败。
    reported: bool = True
    #: 这次运行期间替设备续命的心跳：发了几次、失败几次（N-137）。
    #: `misses > 0` 说的是"这段时间控制面没能被我告知我还活着"——
    #: 它不等于运行失败，但足以让那条 `running` 被判活 sweep 收走，所以必须能被看见。
    run_heartbeats: int = 0
    run_heartbeat_misses: int = 0

    def as_json(self) -> dict:
        return {
            "deployment_id": self.deployment_id,
            "action": self.action,
            "status_before": self.status_before,
            "status_after": self.status_after,
            "sha256": self.sha256,
            "detail": self.detail,
            "reported": self.reported,
            "run_heartbeats": self.run_heartbeats,
            "run_heartbeat_misses": self.run_heartbeat_misses,
            "observation": None
            if self.observation is None
            else {"ok": self.observation.ok, "steps": self.observation.steps, "detail": self.observation.detail},
        }


class EdgeAgentRuntime:
    def __init__(
        self,
        *,
        server: str,
        token: str,
        agent_id: str,
        workdir: Path,
        driver: RobotDriver | None = None,
        client: AgentClient | None = None,
        max_artifact_bytes: int | None = None,
        run_heartbeat_seconds: float = RUN_HEARTBEAT_SECONDS,
    ) -> None:
        self.agent_id = agent_id
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        kwargs = {} if max_artifact_bytes is None else {"max_artifact_bytes": max_artifact_bytes}
        self.client = client or AgentClient(server, token, **kwargs)
        self.driver: RobotDriver = driver or MockRobotDriver()
        self.run_heartbeat_seconds = run_heartbeat_seconds

    # ------------------------------------------------------------------
    def run_once(self, device_info: dict | None = None) -> list[RoundOutcome]:
        """一轮：心跳 → 发现 → 逐条处理。返回每条的处置结果（含跳过的）。"""
        self.client.heartbeat(self.agent_id, device_info or {"agent": "embodiedcloud-edge-agent"})
        outcomes: list[RoundOutcome] = []
        for record in self.client.list_assigned(self.agent_id):
            dep_id = str(record.get("id", ""))
            before = str(record.get("status", ""))
            try:
                outcomes.append(self._handle(record))
            except AgentClientError as exc:
                # 一条坏记录不能带走整轮：其余设备仍要取件。错误原文不含 token
                # （AgentClientError 只带状态码/URL/detail）。
                outcomes.append(
                    RoundOutcome(dep_id, "error", before, before, detail=str(exc))
                )
        return outcomes

    def loop(
        self,
        *,
        interval_seconds: float = 5.0,
        iterations: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> list[RoundOutcome]:
        """常驻模式。`iterations` 给用例和冒烟用（跑满即退），运维默认无限。"""
        seen: list[RoundOutcome] = []
        done = 0
        while iterations is None or done < iterations:
            seen.extend(self.run_once())
            done += 1
            if should_stop is not None and should_stop():
                break
            if iterations is not None and done >= iterations:
                break
            time.sleep(interval_seconds)
        return seen

    # ------------------------------------------------------------------
    def _handle(self, record: dict) -> RoundOutcome:
        dep_id = str(record["id"])
        before = str(record["status"])
        checksum = str(record.get("checksum") or "")
        dest = self.workdir / f"{dep_id}.pt"
        if before not in DEVICE_ADVANCEABLE:
            return RoundOutcome(dep_id, "skipped", before, before, detail="control-plane owned state")

        current = record
        fetched = ""
        unreported: str | None = None
        if before == "pending":
            current = self.client.begin(self.agent_id, dep_id)
        if str(current.get("status")) == "downloading":
            fetch = self.client.fetch_artifact(dep_id, dest, expected_sha256=checksum)
            fetched = fetch.sha256
            current = self.client.report_checksum(dep_id, fetch.sha256)

        after = str(current.get("status"))
        # 显式标注：赋值现在发生在 `with keepalive:` 的块里，靠隐式 None 推不出收窄，
        # mypy 会在下面读 observation.ok 时报 union-attr。
        observation: RobotObservation | None = None
        run_heartbeats = 0
        run_heartbeat_misses = 0
        action = "reported" if fetched else "no-op"
        # 只在**本轮自己把它推到 verified** 的那一次跑驱动：`assigned` 是轮询，
        # 若以"状态是 verified"为条件，一台常驻设备会把同一个模型无限重复上机。
        # 代价是"已 verified 但崩在跑之前"不会被自动补跑（没有运行游标），
        # 这条限制如实写进 ADR 0007 的后果段。
        if after == "verified" and fetched and dest.is_file():
            # 心跳通路必须与这段阻塞**并行**：一轮只发一次心跳时，一次 20 分钟的物理运行
            # 在控制面上与"设备没了"完全同形，而那把判活尺还会顺手把这条 running 收成 failed
            # （N-133 的连带判决）。发的是纯心跳，不带 device_info——`edge_service.heartbeat`
            # 只在 device_info 非空时合并它，所以这一段不会改写运维看到的设备画像。
            keepalive = RunKeepalive(
                lambda: self.client.heartbeat(self.agent_id),
                interval_seconds=self.run_heartbeat_seconds,
            )
            with keepalive:
                self.driver.load(dest, str(current.get("robot_type") or ""))
                observation = self.driver.run()
            run_heartbeats, run_heartbeat_misses = (
                keepalive.state.beats,
                keepalive.state.misses,
            )
            try:
                self.client.telemetry(
                    self.agent_id,
                    RUN_TELEMETRY_KIND,
                    {
                        "deployment_id": dep_id,
                        "driver": getattr(self.driver, "name", "unknown"),
                        "sha256": fetched or checksum,
                        "ok": observation.ok,
                        "steps": observation.steps,
                        "detail": observation.detail,
                    },
                )
            except AgentClientError as exc:
                # 机器人**已经动过**了。让异常继续往外抛会被 `run_once` 的通用分支折成
                # `("error", before, before)`——那一格读起来跟"取件之前就失败了"完全一样，
                # 而事实是一次真实的物理运行没人知道。这里反过来把它记全：action 仍是
                # `ran`、状态仍是 `verified`、观察结果保留，另加 `reported=False` 与一行
                # stderr；抬退出码是 `main()` 的责任，不在这里悄悄降级也不在这里退出。
                unreported = str(exc)
                print(f"[edge-agent] {dep_id} 已运行，但结果未能上报：{exc}", file=sys.stderr)
            action = "ran"
        elif after == "verified" and not fetched:
            action = "skipped"
        detail = "" if observation is None else observation.detail
        if unreported is not None:
            detail = f"{detail}；已运行但上报失败：{unreported}"
        if run_heartbeat_misses:
            # 上报本身可能成功了，但这段时间里控制面没收到过心跳——那条 running
            # 可能已经被判活 sweep 收走。这一格必须能被读出来，不能只活在线程的账本里。
            detail = f"{detail}；运行期间心跳失败 {run_heartbeat_misses} 次"
        return RoundOutcome(
            dep_id,
            action,
            before,
            after,
            sha256=fetched,
            detail=detail,
            observation=observation,
            reported=unreported is None,
            run_heartbeats=run_heartbeats,
            run_heartbeat_misses=run_heartbeat_misses,
        )

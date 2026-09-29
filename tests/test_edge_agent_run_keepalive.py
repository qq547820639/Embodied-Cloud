"""设备在干活的那段时间里，必须继续证明自己活着（N-137）。

改前的形状：`EdgeAgentRuntime.run_once()` 一轮只发一次心跳，而 `self.driver.run()` 是同步
阻塞的——一次物理巡检可以合法地跑几十分钟。那段阻塞里控制面收不到任何请求，于是
`expire_stale_agents`（阈值 `edge_agent_offline_after_seconds`，默认 90 s）把这台**正在干活**
的设备判成 `offline`，并顺带把它名下那条 `running` 的部署收成 `failed`（N-133 的连带判决）。
"忙"与"没了"在只有一条轮询通路的情况下同形，而 N-136 那条 per-deployment 预算治不了这一格：
预算量的是"这条运行开了多久"，判活量的是"最后一次听见你距今多久"，两根轴各修各的。

修法是一条独立于工作循环的心跳通路（形状借 Kubernetes 的 Node Lease 与 systemd 的
`WatchdogSec`：存活上报有它自己的节奏，不挂在工作体上）。实现是 `edge_agent/keepalive.py`
里的 `RunKeepalive`，包住 `driver.load()`＋`driver.run()` 那一段。

三条极性都是刻意的，用例逐条钉：
- **发了几次**要能被看见（`run_heartbeats`），否则"我以为我在报活"是一句无法核对的主张；
- **没送到的那些**也要被看见（`run_heartbeat_misses`）：心跳通路坏了如果静默停摆，
  读出来的恰好就是本件要修的那个形状，所以线程里一律捕获并计数，并抬退出码；
- **线程不能留下孤儿**：上一轮的心跳线程替下一轮报活，会把已经死掉的运行读成活的。
  退出时先置 stop 事件再 join，所以快驱动那一趟几乎不等就返回。

网络层用假件（`_FakeControlPlane` 的子类），编排与驱动用真的——
`_handle` 的 begin → 取件 → 报摘要 → 上机整条推进都真走一遍。
"""

import itertools
import threading
import time
from pathlib import Path

import pytest

import edge_agent.__main__ as agent_main
from edge_agent.agent import RUN_HEARTBEAT_SECONDS, RoundOutcome
from edge_agent.client import AgentClientError
from edge_agent.drivers import MockRobotDriver
from edge_agent.keepalive import THREAD_NAME, RunKeepalive
from tests.test_edge_agent_unreported_run import _FakeControlPlane, _runtime


class _RecordingControlPlane(_FakeControlPlane):
    """只替换网络那一层：心跳按"驱动此刻是否在跑"分类计数。

    `fail_from` 让"轮首那一次成功、跑起来之后通路坏了"这个形状可以被单独造出来——
    全坏的话整轮在第一次心跳就抛出，压根走不到驱动，测的就不是这一格了。
    """

    def __init__(self, *, fail_heartbeats: bool = False, fail_from: int = 1) -> None:
        super().__init__()
        self.fail_heartbeats = fail_heartbeats
        self.fail_from = fail_from
        self.beats = 0
        self.beats_during_run = 0
        self.beat_device_infos: list[dict | None] = []

    def heartbeat(self, agent_id: str, device_info: dict | None = None) -> dict:
        self.calls.append("heartbeat")
        self.beats += 1
        self.beat_device_infos.append(device_info)
        if self.fail_heartbeats and self.beats > self.fail_from:
            raise AgentClientError(503, f"http://control.example/api/edge/agents/{agent_id}/heartbeat", "nope")
        if getattr(self.driver, "in_run", False):
            self.beats_during_run += 1
        return {"agent": {"id": agent_id, "status": "online"}}


class _SlowDriver(MockRobotDriver):
    """把"我正在跑"这件事做成一个可被观察的窗口，而不是靠时间差去猜。

    `live_threads` 是从**被阻塞的那一侧**看到的线程快照：`daemon` 标志与"线程此刻活着"
    只有在这里才观察得到，而它们正是"驱动卡住时会不会拖住进程退出"的根据。
    """

    def __init__(self, seconds: float = 0.5) -> None:
        super().__init__()
        self.seconds = seconds
        self.in_run = False
        self.live_threads: list[tuple[str, bool]] = []

    def run(self):
        self.in_run = True
        self.live_threads = [
            (t.name, t.daemon) for t in threading.enumerate() if "keepalive" in t.name
        ]
        try:
            time.sleep(self.seconds)
            return super().run()
        finally:
            self.in_run = False


def _plane(driver: _SlowDriver) -> _RecordingControlPlane:
    ctrl = _RecordingControlPlane()
    ctrl.driver = driver  # 心跳回调按这个标志分类
    return ctrl


def test_a_long_run_keeps_beating_while_the_driver_is_blocked(tmp_path: Path) -> None:
    """开火主证：心跳必须落在**驱动进行中的那段窗口里**，而不是只在轮次首尾各一次。"""
    driver = _SlowDriver(seconds=0.6)
    ctrl = _plane(driver)

    outcome = _runtime(ctrl, tmp_path, driver, run_heartbeat_seconds=0.05).run_once()[0]

    assert outcome.action == "ran" and outcome.reported is True
    assert driver.runs == 1
    assert ctrl.beats_during_run >= 2, (
        f"运行进行中心跳只发了 {ctrl.beats_during_run} 次——阻塞期间控制面听不到设备，"
        "这正是 N-137 的病灶"
    )
    assert outcome.run_heartbeats == ctrl.beats_during_run, "读数要能与独立计数器对上"
    # 线程身份与 daemon 标志从被阻塞的那一侧看：非 daemon 的通路在驱动卡死时会拖住进程退出，
    # 而"恰好一条"排除了重复起线（每轮起一条不停）。
    assert driver.live_threads == [(THREAD_NAME, True)], driver.live_threads


def test_the_heartbeat_leaves_no_thread_behind(tmp_path: Path) -> None:
    """跑完必须没有孤儿线程：旧线程替新轮次报活，会把已死的运行读成活的。"""
    before = {t.name for t in threading.enumerate()}
    driver = _SlowDriver(seconds=0.1)
    ctrl = _plane(driver)

    _runtime(ctrl, tmp_path, driver, run_heartbeat_seconds=0.02).run_once()

    left = {t.name for t in threading.enumerate()} - before
    assert not any("keepalive" in name for name in left), left
    assert ctrl.beats >= 1, "整趟一个心跳都没发，那这条通路根本没接上"


def test_a_fast_run_pays_neither_a_beat_nor_a_full_interval(tmp_path: Path) -> None:
    """不开火对照：驱动立刻返回时不该有多余心跳，退出也不该等满一个间隔。"""
    driver = _SlowDriver(seconds=0.0)
    ctrl = _plane(driver)

    started = time.monotonic()
    outcome = _runtime(ctrl, tmp_path, driver, run_heartbeat_seconds=30.0).run_once()[0]
    elapsed = time.monotonic() - started

    assert (outcome.action, outcome.run_heartbeats, outcome.run_heartbeat_misses) == ("ran", 0, 0)
    assert ctrl.beats_during_run == 0, "30 s 的间隔内跑完，不该有运行期心跳"
    assert elapsed < 2.0, f"退出在等满一个间隔（{elapsed:.2f} s）——stop 事件没起作用"


def test_the_gap_between_liveness_signals_is_bounded_well_inside_the_threshold(tmp_path: Path) -> None:
    """这条判据量的是**保护本身**：判活窗口里必须落得下心跳，而不是"数到几次心跳"。

    上限取两个之中更严的一个：控制面的判活阈值（`edge_agent_offline_after_seconds`，
    真读数，不抄常数）与"把这段阻塞至少切成三段"。改前那一形（轮首发一次、整段阻塞期间
    什么都没有）的最大间隔等于整段运行时长，两个界都越过去 ⇒ 必红。
    """
    from app.deps import settings

    seconds = 0.6
    interval = 0.05
    driver = _SlowDriver(seconds=seconds)
    ctrl = _plane(driver)

    stamps: list[float] = []
    original_beat = ctrl.heartbeat

    def beat(agent_id: str, device_info: dict | None = None) -> dict:
        stamps.append(time.monotonic())
        return original_beat(agent_id, device_info)

    ctrl.heartbeat = beat  # type: ignore[method-assign]
    _runtime(ctrl, tmp_path, driver, run_heartbeat_seconds=interval).run_once()

    assert len(stamps) >= 2, "只有一个信号的话，这条判据没有分母"
    max_gap = max(later - earlier for earlier, later in itertools.pairwise(stamps))
    threshold = float(settings.edge_agent_offline_after_seconds)
    assert max_gap < threshold, f"最大间隔 {max_gap:.3f} s 已经越过判活阈值 {threshold:.0f} s"
    assert max_gap < seconds / 3, (
        f"最大间隔 {max_gap:.3f} s 没有把 {seconds:.1f} s 的阻塞切开——"
        "这段窗口里控制面仍然只能把'忙'读成'没了'"
    )


def test_a_broken_heartbeat_channel_is_counted_and_loud(tmp_path: Path) -> None:
    """心跳坏了必须被数出来、被说出来，且不能把这次运行带走。"""
    driver = _SlowDriver(seconds=0.2)
    ctrl = _RecordingControlPlane(fail_heartbeats=True, fail_from=1)
    ctrl.driver = driver

    outcome = _runtime(ctrl, tmp_path, driver, run_heartbeat_seconds=0.02).run_once()[0]

    assert outcome.action == "ran" and outcome.reported is True, "心跳失败不等于运行失败"
    assert outcome.run_heartbeats == 0 and outcome.run_heartbeat_misses >= 1, outcome
    assert "心跳失败" in outcome.detail, outcome.detail
    assert outcome.as_json()["run_heartbeat_misses"] == outcome.run_heartbeat_misses
    assert ctrl.beats >= 1, "假件的调用计数与线程的账要对得上，否则读数没被独立核过"


def test_the_liveness_beat_carries_no_device_info(tmp_path: Path) -> None:
    """这段心跳不许改写运维看到的设备画像：`heartbeat` 只在 device_info 非空时合并它。"""
    driver = _SlowDriver(seconds=0.12)
    ctrl = _plane(driver)

    _runtime(ctrl, tmp_path, driver, run_heartbeat_seconds=0.02).run_once()

    assert ctrl.beat_device_infos, "一个心跳都没发，这条判据没有分母"
    assert all(info is None for info in ctrl.beat_device_infos[1:]), ctrl.beat_device_infos
    assert ctrl.beat_device_infos[0] is not None, "轮首那次仍应带设备画像（原有行为）"


def test_the_run_heartbeat_cadence_stays_a_fraction_of_the_liveness_threshold() -> None:
    """两个数必须成比例，否则运维把间隔调大一点，这层保护就静消失了。

    下限取"判活窗口里至少落得下三次心跳"：MQTT 的口径是 1.5× keep-alive 才判死
    （`[MQTT-3.1.2-24]`，本轮亲开原文），Kubernetes 的租约续期是 lease 的 0.25 倍，
    systemd 建议超时的一半——三家都要求"至少好几拍"，而不是"刚好一拍"。
    阈值不抄常数：读 `settings.edge_agent_offline_after_seconds` 那唯一的源。
    """
    from app.deps import settings

    threshold = settings.edge_agent_offline_after_seconds
    assert threshold >= RUN_HEARTBEAT_SECONDS * 3, (
        f"运行期心跳 {RUN_HEARTBEAT_SECONDS} s 与判活阈值 {threshold} s 的比例不到 3:1，"
        "一次慢心跳或一次重试就会把这段窗口清空"
    )


@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_a_nonpositive_interval_is_refused_at_the_boundary(bad: float) -> None:
    """间隔必须是正数秒：0 会让线程变成一个不睡眠的 HTTP 风暴，负数则永远等不到。"""
    with pytest.raises(ValueError, match="正数秒"):
        RunKeepalive(lambda: None, interval_seconds=bad)


def test_the_keepalive_loop_survives_a_beating_that_raises() -> None:
    """线程里不许有未捕获异常：一崩就静默停摆，读出来的形状与本件要修的完全一样。"""
    calls: list[int] = []

    def beat() -> None:
        calls.append(len(calls))
        raise RuntimeError("控制面回了一句看不懂的")

    keeper = RunKeepalive(beat, interval_seconds=0.01)
    with keeper:
        time.sleep(0.08)
    assert keeper.state.beats == 0 and keeper.state.misses >= 1, keeper.state
    assert len(calls) >= 1 and "看不懂的" in keeper.state.last_error
    assert not any(t.name == keeper.thread_name for t in threading.enumerate())


def test_heartbeat_misses_move_the_exit_code() -> None:
    """退出码的第三型：这段窗口的存活证据没送达 ⇒ 冒烟跑不许绿。"""
    ran_clean = RoundOutcome("d-1", "ran", "verified", "verified", reported=True)
    ran_lost_heartbeats = RoundOutcome(
        "d-1", "ran", "verified", "verified", reported=True, run_heartbeats=1, run_heartbeat_misses=3
    )
    ran_but_unreported = RoundOutcome("d-1", "ran", "verified", "verified", reported=False)

    assert agent_main._exit_code([ran_clean]) == 0
    assert agent_main._exit_code([ran_lost_heartbeats]) == 1
    assert agent_main._exit_code([ran_but_unreported]) == 1, "原有两型不许被新规则挤掉"


def test_the_run_telemetry_is_still_the_single_closure_signal(tmp_path: Path) -> None:
    """补一条作用域断言：这段心跳不能顺带把遥测重复发出去（收口只认那一条 edge-run）。"""
    from app.services.edge import RUN_TELEMETRY_KIND

    driver = _SlowDriver(seconds=0.2)
    ctrl = _plane(driver)

    _runtime(ctrl, tmp_path, driver, run_heartbeat_seconds=0.05).run_once()

    kinds = [p["kind"] for p in ctrl.payloads]
    assert kinds == [RUN_TELEMETRY_KIND], kinds
    assert ctrl.calls.count("telemetry") == 1
    assert ctrl.calls[0] == "heartbeat", "轮首心跳仍在，运行期心跳是**额外**的那几次"

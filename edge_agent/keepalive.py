"""运行期间的心跳保持（N-137）：活干多久，都不该让控制面以为设备没了。

`EdgeAgentRuntime.run_once()` 一轮只发一次心跳，而一次物理运行可以合法地跑几十分钟。
那段阻塞里控制面收不到任何请求，于是它的判活 sweep（`edge_agent_offline_after_seconds`，
默认 90 s）把这台**正在干活**的设备判成 `offline`，并顺带把它名下那条 `running` 的部署
收成 `failed`（N-133 的连带判决）。"忙"与"没了"在只有一条轮询通路的情况下同形，
而这两件事必须分开——设备还在，任务还在跑。

形状借 Kubernetes 的 **Node Lease**：kubelet 的租约续期是一条独立于工作循环的通路，
节点状态更新与租约续期各有自己的频率，节点在跑容器的时候并不会停止续租；
systemd 的 `WatchdogSec` 也是同一形状——喂狗与干活是两件事，干活的那段必须自己腾出手喂狗。
本模块给的就是那条独立通路：一个只做心跳的 daemon 线程，包住 `driver.run()` 这一段。

三条边界，都是刻意的：
- **心跳失败不改变判决方向**：计数（`misses`）与最后一次错误留在对象上，由调用方决定要不要
  让冒烟跑变红。线程里抛出未捕获异常会让整条心跳通路静默停摆，而停摆的读数恰好就是
  本件要修的那个形状（没人再报活，看起来像设备没了），所以这里一律捕获并计数。
- **线程是 daemon，且退出时先置 stop 再 join**：驱动卡死时不该拖住进程退出，
  而 join 保证不会出现"上一轮的心跳线程还在替下一轮报活"——那会把已死的运行读成活的。
- **间隔必须显著小于判活阈值**：缺省 15 s，在 90 s 的窗口里给 6 次机会；
  一次请求自己的超时是 30 s（`client.DEFAULT_TIMEOUT_SECONDS`），所以最坏情况下
  一次慢心跳加一次重试仍落在窗口内。这个数不是"更快更好"，是给判活留出可证的余量。
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass

#: "发一次心跳"这个动作。返回值一律忽略：生产上是 `client.heartbeat`（返回一个 dict），
#: 用例里给的是只记数的闭包，两边都不该因为这条通路读了返回值而耦合。
Beat = Callable[[], object]
#: 线程名。用例要能在被阻塞的那一侧认出这条通路（daemon 标志只有从驱动内部观察才有意义：
#: 它的存在是为了回答"驱动卡住时会不会拖住进程退出"）。
THREAD_NAME = "edge-run-keepalive"


@dataclass
class KeepaliveState:
    """心跳线程自己那本账。`misses > 0` 是"这段时间控制面没能被我告知我还活着"。"""

    beats: int = 0
    misses: int = 0
    last_error: str = ""


class RunKeepalive:
    """上下文管理器：进入时起一条只做心跳的线程，退出时停掉它。

    `beat` 是"发一次心跳"这个动作（生产上是 `client.heartbeat`，用例里给假的），
    它自己抛出的任何异常都被折进 `state.misses`——心跳通路坏了要能被看见，
    但不能把正在跑的物理运行一起带走。
    """

    def __init__(
        self, beat: Beat, *, interval_seconds: float = 15.0, name: str = THREAD_NAME
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(f"心跳间隔必须是正数秒，收到 {interval_seconds!r}")
        self._beat = beat
        self._interval = float(interval_seconds)
        self._name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = KeepaliveState()

    @property
    def thread_name(self) -> str:
        return self._name

    def _loop(self) -> None:
        # `wait` 而不是 `sleep`：退出时置位能立刻返回，不等完一个间隔。
        while not self._stop.wait(self._interval):
            try:
                self._beat()
            except Exception as exc:  # 心跳坏在这里必须计数，不许静默停摆（BLE001 已在全仓忽略）
                self.state.misses += 1
                self.state.last_error = str(exc)
            else:
                self.state.beats += 1

    def __enter__(self) -> "RunKeepalive":
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            # 超时给一个间隔 + 一次请求上限：再慢就是驱动或网络本身的问题，
            # 不能在这里无限等——驱动异常时这条通路必须先让路。
            thread.join(timeout=self._interval + 35.0)
        self._thread = None
        # 不返回任何真值：驱动抛出的异常必须原样往上传，心跳通路的收尾没有"吞掉它"的权力。

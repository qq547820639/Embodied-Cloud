"""设备侧驱动接口 + mock 驱动（验收门 G5.1 的"mock 驱动回环"落点）。

服务端 `app/services/robot.py` 里也有一份 MockRobotDriver，但那是**控制面侧**的
仿真（没有真机时把 run/complete 走完）。这份不一样：它跑在设备进程里，收到的
是"已经核对过摘要的本地文件路径"。故意不 import 服务端那份——设备上装的是本包，
两边各自演进才不会伪装成"同一实现"（真驱动接入时替换 `build_driver` 的返回即可）。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class RobotObservation:
    ok: bool
    steps: int
    detail: str


class RobotDriver(Protocol):
    """一次部署的落地端：装载模型 → 跑一段 → 回报观测。"""

    name: str

    def load(self, model_path: Path, robot_type: str) -> None: ...

    def run(self) -> RobotObservation: ...


class MockRobotDriver:
    """把模型文件整份读一遍（证明这份字节确实交给了驱动）再回一条观测。

    `loaded` 是给用例和运维看的现场：路径、机器人型号、字节数。装载失败（文件不
    存在/不可读）按真实驱动的行为抛，不吞。
    """

    name = "mock"

    def __init__(self) -> None:
        self.loaded: tuple[Path, str, int] | None = None
        self.runs = 0

    def load(self, model_path: Path, robot_type: str) -> None:
        data = model_path.read_bytes()
        self.loaded = (model_path, robot_type, len(data))

    def run(self) -> RobotObservation:
        if self.loaded is None:
            raise RuntimeError("driver.run() before load(): 没有装载过的模型")
        self.runs += 1
        path, robot_type, size = self.loaded
        return RobotObservation(
            ok=True, steps=self.runs, detail=f"{robot_type} ran {path.name} ({size} bytes)"
        )


def build_driver(name: str) -> RobotDriver:
    """按名字取驱动。目前只有 mock；真机驱动（如 ROS 2 / USD）在那一轮回填。"""
    if name == MockRobotDriver.name:
        return MockRobotDriver()
    raise ValueError(f"unknown robot driver: {name!r}（可用：mock）")

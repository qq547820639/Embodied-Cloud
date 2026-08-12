"""RobotDriver 接口与 MockRobotDriver (控制面回环验证).

⚠️ PHYSICAL_ROBOT_VALIDATION_PENDING: 物理真机验证 (Gate G5.2) 未完成,
本模块仅用于开发/测试阶段的模拟回环, 不得据此宣称 Sim2Real 成功.
"""

import copy
from dataclasses import dataclass, field
from typing import Protocol

# 单步最大关节增量（rad）：模拟真实驱动的限速执行，避免一步到位
JOINT_STEP = 0.25


@dataclass
class RobotState:
    connected: bool = False
    position: dict = field(default_factory=dict)
    joint_states: dict = field(default_factory=dict)
    telemetry: dict = field(default_factory=dict)


class RobotDriver(Protocol):
    """真机驱动抽象; 真机适配器实现本协议以接入控制面."""

    name: str

    def connect(self) -> RobotState: ...

    def disconnect(self) -> None: ...

    def read_state(self) -> RobotState: ...

    def send_action(self, action: dict) -> RobotState: ...

    def emergency_stop(self) -> None: ...


class MockRobotDriver:
    """模拟机器人: 无硬件, 真机验证前用于回环验证.

    行为约定:
    - connect 后 read_state 可用; 未连接时 read_state/send_action 抛 RuntimeError.
    - send_action 记录 action, 并按 JOINT_STEP 限速逼近 joint_targets.
    - emergency_stop 断开连接并置 e_stop 标记.
    """

    name = "mock-robot"

    def __init__(self) -> None:
        self._connected = False
        self._state = RobotState()
        self._state.joint_states = {"joint_1": 0.0, "joint_2": 0.0}
        self.actions: list[dict] = []
        self.e_stop = False

    def connect(self) -> RobotState:
        self._connected = True
        self.e_stop = False
        self._state.connected = True
        self._state.position = {"x": 0.0, "y": 0.0, "z": 0.0}
        self._state.telemetry = {"battery": 100.0, "mode": "idle"}
        return self._snapshot()

    def disconnect(self) -> None:
        self._connected = False
        self._state.connected = False

    def read_state(self) -> RobotState:
        if not self._connected:
            raise RuntimeError("robot not connected")
        return self._snapshot()

    def send_action(self, action: dict) -> RobotState:
        if not self._connected:
            raise RuntimeError("robot not connected")
        self.actions.append(action)
        for joint, target in action.get("joint_targets", {}).items():
            current = float(self._state.joint_states.get(joint, 0.0))
            delta = max(-JOINT_STEP, min(JOINT_STEP, float(target) - current))
            self._state.joint_states[joint] = round(current + delta, 4)
        position = action.get("position")
        if isinstance(position, dict):
            self._state.position.update(position)
        return self._snapshot()

    def emergency_stop(self) -> None:
        self._connected = False
        self._state.connected = False
        self.e_stop = True

    def _snapshot(self) -> RobotState:
        return copy.deepcopy(self._state)

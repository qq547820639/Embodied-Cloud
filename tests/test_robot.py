"""MockRobotDriver 单元测试 (Gate G5.2 真机验证前的回环验证)."""

import pytest

from app.services.robot import MockRobotDriver


def test_connect_returns_connected_state():
    driver = MockRobotDriver()
    state = driver.connect()
    assert state.connected is True
    assert driver.read_state().connected is True


def test_read_state_before_connect_raises():
    driver = MockRobotDriver()
    with pytest.raises(RuntimeError):
        driver.read_state()


def test_send_action_records_action_and_moves_joints():
    driver = MockRobotDriver()
    driver.connect()
    action = {"joint_targets": {"joint_1": 1.0, "joint_2": -2.0}}
    state = driver.send_action(action)
    assert driver.actions == [action]
    # 限速单步：不会一步到位（JOINT_STEP = 0.25）
    assert state.joint_states["joint_1"] == pytest.approx(0.25)
    assert state.joint_states["joint_2"] == pytest.approx(-0.25)


def test_send_action_updates_position():
    driver = MockRobotDriver()
    driver.connect()
    state = driver.send_action({"position": {"x": 0.5}})
    assert state.position["x"] == 0.5


def test_send_action_before_connect_raises():
    driver = MockRobotDriver()
    with pytest.raises(RuntimeError):
        driver.send_action({"joint_targets": {}})


def test_emergency_stop_disconnects_and_flags():
    driver = MockRobotDriver()
    driver.connect()
    driver.emergency_stop()
    assert driver.e_stop is True
    with pytest.raises(RuntimeError):
        driver.read_state()


def test_disconnect_prevents_read():
    driver = MockRobotDriver()
    driver.connect()
    driver.disconnect()
    with pytest.raises(RuntimeError):
        driver.read_state()


def test_state_snapshot_isolation():
    driver = MockRobotDriver()
    state = driver.connect()
    state.joint_states["joint_1"] = 99.0
    assert driver.read_state().joint_states["joint_1"] == 0.0

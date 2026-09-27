"""四个 docker 档的可用性前置：daemon **卡住**时必须与"拒连"同样得到原因，而不是异常。

起因是一手事故：一次干净树复算里 `docker version` 超时 20 秒，
`tests/test_docker_provider_integration.py` 的 26 条用例全部以
`failed on setup with "subprocess.TimeoutExpired"` 收场——发布门禁把一次环境抖动读成
26 条代码失败。检查后发现这个不对称在四个 docker 档里各有一份（pg / s3 / k8s 控制面 / docker provider），
所以判据按"四类都过一遍"写，而不是只补我撞到的那一个。
"""

from __future__ import annotations

import subprocess

import pytest

# 每档的 gate_reason 与它用来跑 docker CLI 的模块内函数名
MODULES = [
    "tests.test_docker_provider_integration",
    "tests.pg_server",
    "tests.s3_server",
    "tests.k8s_server",
]


def _boom(*args, **kwargs):
    raise subprocess.TimeoutExpired(cmd=["docker", *[str(a) for a in args]], timeout=kwargs.get("timeout", 20))


@pytest.mark.parametrize("module_name", MODULES)
def test_hanging_daemon_yields_a_reason_not_an_exception(module_name, monkeypatch) -> None:
    import importlib

    mod = importlib.import_module(module_name)
    assert hasattr(mod, "gate_reason"), f"{module_name} 没有 gate_reason，本判据覆盖不到它"
    monkeypatch.setattr(mod, "_docker", _boom)
    reason = mod.gate_reason()
    assert isinstance(reason, str) and reason, f"{module_name}: 卡住的 daemon 没被判成不可用：{reason!r}"
    assert "超时" in reason or "timeout" in reason.lower(), f"{module_name}: 原因里没留下时间线索：{reason}"


@pytest.mark.parametrize("module_name", MODULES)
def test_healthy_daemon_is_not_reported_as_timed_out(module_name, monkeypatch) -> None:
    """反向对照：不许把"超时"写死成答案——健康时必须走正常判定。"""
    import importlib

    class _Ok:
        returncode = 0
        stdout = "aarch64\nName: x86_64\nServer Version: 27\n"
        stderr = ""

    mod = importlib.import_module(module_name)
    monkeypatch.setattr(mod, "_docker", lambda *a, **k: _Ok())
    reason = mod.gate_reason() or ""
    assert "超时" not in reason and "timeout" not in reason.lower(), f"{module_name}: {reason}"

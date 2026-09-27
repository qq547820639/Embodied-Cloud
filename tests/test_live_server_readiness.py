"""真进程夹具的"就绪等待"：外部依赖卡住/报错不许把整档变成硬错误。

两条一手事实推动了这份判据：
- `tests/live_server.py` 的就绪循环只 `contextlib.suppress(OSError)`，而 httpx 的连接/读超时是
  `httpx.TransportError`（不是 OSError）——健康检查一旦卡住，异常从循环里穿出去，既绕过了
  "未就绪 ⇒ 带服务日志的 RuntimeError"这条有诊断价值的出路，也把浏览器/边缘两档从
  "干净跳过或可读失败"变成 setup 错误。
- 同类形状在 k8s 档还有一处：`node_image_cached()` 直接 `_docker(...)`，而它挂在
  `gate_reason()` 的跳过判定链上。N-46 只把 version/inspect 两处换成 `docker_probe`，
  这一处被漏掉——因为 `gate_reason` 在更早一步就 return 了，按模块参数化的判据看不见它。
"""

from __future__ import annotations

import httpx


def test_a_hanging_health_check_is_not_ready_rather_than_an_exception() -> None:
    from tests.live_server import wait_until_ready

    calls = {"n": 0}

    def probe() -> str:
        calls["n"] += 1
        raise httpx.ConnectTimeout("timed out")

    ok, why = wait_until_ready(probe=probe, alive=lambda: True, deadline_seconds=3, sleep=lambda _s: None)
    assert ok is False, "卡住的健康检查被当成'就绪'了？"
    assert calls["n"] >= 3, f"循环只试了 {calls['n']} 次就放弃/抛出，没有走完退避"
    assert "超时" in why or "timeout" in why.lower() or "未就绪" in why, why


def test_readiness_wait_reports_the_service_log_and_never_escapes() -> None:
    from tests.live_server import wait_until_ready

    ok, why = wait_until_ready(
        probe=lambda: (_ for _ in ()).throw(OSError("connection refused")),
        alive=lambda: True,
        deadline_seconds=1,
        sleep=lambda _s: None,
        log_tail=lambda: "Traceback: boom",
    )
    assert ok is False
    assert "Traceback: boom" in why, f"失败原因里没带服务日志，排查又回到重跑撞运气：{why}"

    ok2, why2 = wait_until_ready(
        probe=lambda: "500",
        alive=lambda: False,
        exit_code=lambda: 3,
        deadline_seconds=5,
        sleep=lambda _s: None,
        log_tail=lambda: "Address already in use",
    )
    assert ok2 is False and "rc=3" in why2 and "Address already in use" in why2, why2


def test_readiness_returns_cleanly_once_the_probe_is_ok() -> None:
    from tests.live_server import wait_until_ready

    seen: list[int] = []

    def probe() -> str:
        seen.append(1)
        return "" if len(seen) >= 2 else "还没起来"

    ok, why = wait_until_ready(probe=probe, alive=lambda: True, deadline_seconds=5, sleep=lambda _s: None)
    assert ok is True and why == "", (ok, why)


def test_health_reason_helper_swallows_transport_errors() -> None:
    """`health_reason` 是那条被吞掉的异常的实际落点；换掉 client 就能验，不必起子进程。"""
    from tests.live_server import health_reason

    class _Boom:
        def get(self, *_a, **_k):
            raise httpx.ReadTimeout("read timed out")

    reason = health_reason("http://127.0.0.1:1/api/health", client=_Boom())
    assert isinstance(reason, str) and reason, reason
    assert "超时" in reason or "timeout" in reason.lower(), reason

    class _NotYet:
        def get(self, *_a, **_k):
            class R:
                status_code = 503

            return R()

    assert "503" in health_reason("http://x/api/health", client=_NotYet())

    class _Up:
        def get(self, *_a, **_k):
            class R:
                status_code = 200

            return R()

    assert health_reason("http://x/api/health", client=_Up()) == ""


def test_k8s_node_image_probe_absorbs_a_hanging_daemon(monkeypatch) -> None:
    """`node_image_cached()` 在跳过判定链上：daemon 卡住要答"没缓存"，不是抛。"""
    import subprocess

    from tests import k8s_server

    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["docker", *[str(a) for a in args]], timeout=kwargs.get("timeout", 30))

    monkeypatch.setattr(k8s_server, "_docker", _boom)
    assert k8s_server.node_image_cached() is False


def test_live_server_fixture_uses_the_shared_helper() -> None:
    """接线：`live_server()` 必须走这两个共享函数，否则判据护的是没人走的那份实现。"""
    import inspect

    import tests.live_server as ls

    src = inspect.getsource(ls.live_server)
    assert "wait_until_ready(" in src, "夹具没接上共享的就绪等待"
    assert "health_reason(" in src, "夹具里的健康检查还是自己吞异常的那份"

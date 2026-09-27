"""docker 档共用的小前置：把"CLI 卡住/起不来"折成一个 rc!=0 的结果，而不是抛异常。

四个 docker 档（docker provider、postgres、对象存储、k8s 控制面）各自有 `gate_reason()`，
它们回答的只是"这一档今天能不能跑"。此前只有"拒连"被认成不可用：daemon 卡住时
`subprocess.run(timeout=...)` 抛 `TimeoutExpired`，异常穿过前置检查，整档用例以
`failed on setup` 收场——一次干净树复算就是这样把环境抖动报成 26 条代码失败。
判据见 `tests/test_docker_gate_hardening.py`（四个模块各测一遍 + 健康时的反向对照）。

`runner` 由调用方传入（各模块自己的 `_docker`），这样判据可以就地替换它，也让
"异常吸收"这段逻辑只有一份实现。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable


def docker_probe(
    runner: Callable[..., subprocess.CompletedProcess[str]], *args: str, timeout: int = 20
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(*args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["docker", *args], returncode=124, stdout="", stderr=f"命令超时（{timeout}s）"
        )
    except OSError as exc:  # 含 FileNotFoundError：CLI 在 PATH 上却起不来
        return subprocess.CompletedProcess(
            ["docker", *args], returncode=127, stdout="", stderr=f"docker CLI 调用失败：{exc}"
        )


def detail_of(probe: subprocess.CompletedProcess[str], limit: int = 120) -> str:
    """probe 失败时给人看的短原因（stderr 优先，某些 CLI 只往 stdout 写错误）。"""
    return (probe.stderr or probe.stdout or "").strip()[:limit]

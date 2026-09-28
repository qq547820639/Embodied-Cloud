"""四个 docker 档的可用性前置：daemon **卡住**时必须与"拒连"同样得到原因，而不是异常。

起因是一手事故：一次干净树复算里 `docker version` 超时 20 秒，
`tests/test_docker_provider_integration.py` 的 26 条用例全部以
`failed on setup with "subprocess.TimeoutExpired"` 收场——发布门禁把一次环境抖动读成
26 条代码失败。检查后发现这个不对称在四个 docker 档里各有一份（pg / s3 / k8s 控制面 / docker provider），
所以判据按"四类都过一遍"写，而不是只补我撞到的那一个。

N-91 补的是同一条规矩的**第二个面**：前置检查（`gate_reason`）全部接了异常吸收，
但**断言本体**里那一条「钉进配方的摘要取不取得到」用的是裸 `_docker(..., timeout=300)`，
于是 2026-09-28 那次整轮认证里 registry 一慢就直接抛 `TimeoutExpired`——
`FAILED tests.test_docker_provider_integration::test_pinned_base_of_the_control_plane_recipe_is_fetchable`，
而同一条传输 7.4 秒回 rc=0、`docker manifest inspect` 10.3 秒回 1308 字节。
这一档本来有设计好的第三种结局（走不通就记 PENDING 并写原因），异常把它绕过去了。
所以这里的判据核三件事：卡住的 pull 必须落进那套三档分流（而不是抛出）、
坏摘要在任何 daemon 读数下仍必须红、健康的 pull 不许被误判成跳过。
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


def _recipe_pinned_refs() -> list[str]:
    """从控制面配方现取钉了 digest 的引用（判据不许写死镜像串）。"""
    from tests.test_supply_chain import REPO_ROOT, _base_refs, _is_pinned

    refs = [r for _, r in _base_refs([REPO_ROOT / "runtime" / "Dockerfile.control-plane"])]
    pinned = sorted({r for r in refs if _is_pinned(r)})
    assert pinned, "配方里没有钉 digest 的引用，本文件的接线判据无从谈起"
    return pinned


def _integ(monkeypatch, *, pull_outcome, second_channel):
    """把 docker provider 档的模块装好并按形状替换两个出口：CLI 与第二通道。"""
    import importlib

    mod = importlib.import_module("tests.test_docker_provider_integration")
    monkeypatch.setattr(mod, "_docker", pull_outcome)
    monkeypatch.setattr(mod, "_independent_digest_read", second_channel)
    return mod


def test_stalled_recipe_base_pull_becomes_a_pending_reason_not_a_case_failure(monkeypatch) -> None:
    """卡住的 pull 必须被吸收成读数，并落进那套三档分流里"定不了案 ⇒ 记 PENDING"那一档。

    改前这一支得到的是 `subprocess.TimeoutExpired`（异常穿出判据本体），
    认证台把它记成一条代码失败；吸收之后它才是它本来被设计成的样子。
    """
    from _pytest.outcomes import Skipped

    mod = _integ(
        monkeypatch,
        pull_outcome=_boom,
        second_channel=lambda ref, digest: ("unknown", "第二通道本轮也够不到"),
    )
    with pytest.raises(Skipped) as excinfo:
        mod._assert_pinned_ref_fetchable(_recipe_pinned_refs()[0])
    reason = str(excinfo.value.args[0] if excinfo.value.args else "")
    assert mod.GATE_SENTINEL in reason, f"没有记成 PENDING：{reason[:200]}"
    assert "unknown" in reason, f"原因里没留下第二通道的读数：{reason[:200]}"


def test_stalled_pull_does_not_wash_out_a_bad_digest(monkeypatch) -> None:
    """反向极性：daemon 卡住也不许把一条**坏摘要**洗成跳过——第二通道说没有就必须红。"""
    mod = _integ(
        monkeypatch,
        pull_outcome=_boom,
        second_channel=lambda ref, digest: ("absent", "逐字节比对：这条摘要不存在"),
    )
    with pytest.raises(AssertionError) as excinfo:
        mod._assert_pinned_ref_fetchable(_recipe_pinned_refs()[0])
    assert "两条独立传输都指认这份摘要不存在" in str(excinfo.value)


def test_healthy_recipe_base_pull_is_neither_skipped_nor_failed(monkeypatch) -> None:
    """合规侧对照：把"超时一律不红"写成无条件跳过，这条就会红（判据不许退化成橡皮章）。"""
    calls: list[tuple[str, ...]] = []

    def _ok(*args: str, **kwargs: object) -> object:
        calls.append(tuple(args))
        head = args[0]

        class _R:
            returncode = 0
            stderr = ""
            stdout = {
                "pull": "Digest: sha256:04d046b13e60\nStatus: Image is up to date\n",
                "image": (
                    "arm64|linux|sha256:04d046b13e60"
                    if "inspect" in args and "{{.Architecture}}|{{.Os}}|{{.Id}}" in args
                    else "ghcr.io/astral-sh/uv@sha256:04d046b13e60\n"
                ),
            }[head]

        return _R()

    mod = _integ(monkeypatch, pull_outcome=_ok, second_channel=lambda ref, digest: ("unknown", "没用到"))
    mod._assert_pinned_ref_fetchable(_recipe_pinned_refs()[0])  # 不抛、不跳
    assert calls[0][0] == "pull", f"第一条应当是 pull，实读 {calls[:2]}"
    assert any(c[0] == "image" for c in calls), f"没有回读镜像形状：{calls}"


def test_timeout_text_is_classified_as_no_answer_by_the_triage() -> None:
    """分流的第一根轴：吸收器留下的那句"命令超时（300s）"必须被认成"没走到 registry"。

    它若被判成 `absent`，坏摘要与慢通道就会被同一档处理；若被判成 `unreadable`，
    一次单纯的网络抖动会 red_undecided——两边都是把环境读数当存在性主张。
    """
    import importlib

    mod = importlib.import_module("tests.test_docker_provider_integration")
    reading = mod._daemon_reading_of("命令超时（300s）")
    assert mod.registry_reading(reading) == "no-answer", reading
    action, message = mod._pull_failure_action(reading, "unknown", "第二通道本轮也够不到")
    assert action == "skip" and mod.GATE_SENTINEL in message, (action, message[:160])

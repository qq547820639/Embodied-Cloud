"""`edge_agent.client` / `edge_agent.drivers` 的离线单测。

真 HTTP 那一半在 `test_edge_agent_e2e.py`（真 server + 真 agent 进程）；这里补的是
**只有喂坏响应才看得见**的那几支：摘要不符、声明体积过大、流中途超限、
错误文本里不得出现 token。用假 `urlopen` 而不是真服务，为的是能把"半路断流"
这种真实世界里靠运气的形状钉成确定输入。
"""

import hashlib
import io
from pathlib import Path

import pytest

import edge_agent.client as client_mod
from edge_agent.client import AgentClient, AgentClientError, ArtifactIntegrityError
from edge_agent.drivers import MockRobotDriver, build_driver

BODY = b"model-weights-" + b"x" * 4096
TOKEN = "super-secret-agent-token-abc"


class _Resp:
    """最小 http.client.HTTPResponse 形状：read(n)/headers/status/url/上下文管理。"""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None, status: int = 200) -> None:
        self._buf = io.BytesIO(body)
        self.headers = headers or {}
        self.status = status
        self.url = "http://control.example/api/deployments/d1/artifact"
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True


def _serve(monkeypatch, body: bytes, headers: dict[str, str] | None = None, status: int = 200) -> _Resp:
    resp = _Resp(body, headers, status)
    monkeypatch.setattr(client_mod, "urlopen", lambda req, timeout=None: resp)
    return resp


def _client(**kw: object) -> AgentClient:
    return AgentClient("http://control.example", TOKEN, **kw)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# 取件与完整性
# ----------------------------------------------------------------------
def test_fetch_writes_the_file_only_after_the_digest_matches(monkeypatch, tmp_path: Path):
    _serve(
        monkeypatch,
        BODY,
        {"X-Artifact-Sha256": hashlib.sha256(BODY).hexdigest(), "X-Artifact-Size": str(len(BODY))},
    )
    dest = tmp_path / "model.pt"
    got = _client().fetch_artifact("d1", dest, expected_sha256=hashlib.sha256(BODY).hexdigest())
    assert got.sha256 == hashlib.sha256(BODY).hexdigest()
    assert got.size_bytes == len(BODY)
    assert dest.read_bytes() == BODY
    assert not list(tmp_path.glob("*.part"))


def test_digest_mismatch_against_the_deployment_checksum_leaves_no_file(monkeypatch, tmp_path: Path):
    """开火对照：服务端送来的字节与部署记录不符 → 抛错且**什么都不留**。"""
    _serve(monkeypatch, BODY, {"X-Artifact-Sha256": hashlib.sha256(BODY).hexdigest()})
    dest = tmp_path / "model.pt"
    with pytest.raises(ArtifactIntegrityError):
        _client().fetch_artifact("d1", dest, expected_sha256="0" * 64)
    assert not dest.exists()
    assert not list(tmp_path.glob("*.part")), "半成品不得留在盘上给下一次运行捡到"


def test_body_not_matching_the_advertised_header_is_rejected(monkeypatch, tmp_path: Path):
    """服务端自己前后不一致（头说 A、体是 B）：不猜哪边对，直接拒收。"""
    _serve(monkeypatch, BODY, {"X-Artifact-Sha256": "a" * 64})
    dest = tmp_path / "model.pt"
    with pytest.raises(ArtifactIntegrityError):
        _client().fetch_artifact("d1", dest, expected_sha256=hashlib.sha256(BODY).hexdigest())
    assert not dest.exists()


def test_declared_size_over_the_cap_is_refused_before_writing(monkeypatch, tmp_path: Path):
    _serve(monkeypatch, BODY, {"X-Artifact-Size": str(len(BODY))})
    dest = tmp_path / "model.pt"
    with pytest.raises(AgentClientError, match="too large"):
        _client(max_artifact_bytes=16).fetch_artifact("d1", dest)  # type: ignore[arg-type]
    assert not dest.exists()
    assert not list(tmp_path.glob("*.part"))


def test_undclared_stream_beyond_the_cap_is_cut_and_cleaned(monkeypatch, tmp_path: Path):
    """没有 X-Artifact-Size 时按累计字节熔断，并且不留 .part。"""
    resp = _serve(monkeypatch, b"", {})
    chunks = iter([b"x" * 64, b"x" * 64, b"x" * 64, b""])
    resp.read = lambda size=-1: next(chunks)  # type: ignore[method-assign]
    dest = tmp_path / "model.pt"
    with pytest.raises(AgentClientError, match="exceeds"):
        _client(max_artifact_bytes=128).fetch_artifact("d1", dest)  # type: ignore[arg-type]
    assert not list(tmp_path.glob("*")), "熔断后工作目录必须是空的"


def test_non_http_base_url_is_refused(tmp_path: Path):
    """file:// 之类的 base_url 会让"取件客户端"变成任意文件读取器 → 构造期就拒。"""
    with pytest.raises(ValueError, match="http"):
        AgentClient("file:///etc/passwd", TOKEN)


# ----------------------------------------------------------------------
# 错误形状与凭据脱敏
# ----------------------------------------------------------------------
def test_http_error_carries_status_and_detail_but_never_the_token(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(client_mod, "urlopen", boom)
    with pytest.raises(AgentClientError) as seen:
        _client().list_assigned("agent-1")
    assert seen.value.status == 404
    assert TOKEN not in str(seen.value)  # SECURITY.md T2/T5：凭据不入异常/日志


def test_transport_error_has_no_status_and_still_no_token(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(client_mod, "urlopen", boom)
    with pytest.raises(AgentClientError) as seen:
        _client().heartbeat("agent-1")
    assert seen.value.status is None
    assert "connection refused" in str(seen.value)
    assert TOKEN not in str(seen.value)


def test_request_sends_the_agent_token_header(monkeypatch):
    """线格式：设备侧认证走 X-Agent-Token，不是 Bearer。"""
    captured: dict[str, str] = {}

    def boom(req, timeout=None):
        captured.update({k: str(v) for k, v in req.header_items()})
        raise AssertionError("stop after capture")

    monkeypatch.setattr(client_mod, "urlopen", boom)
    with pytest.raises(AssertionError, match="stop after capture"):
        _client().heartbeat("agent-1")
    # urllib 会把头名规范化成 X-agent-token
    lowered = {k.lower(): v for k, v in captured.items()}
    assert lowered.get("x-agent-token") == TOKEN
    assert "authorization" not in lowered


# ----------------------------------------------------------------------
# 驱动
# ----------------------------------------------------------------------
def test_mock_driver_refuses_to_run_before_load(tmp_path: Path):
    driver = MockRobotDriver()
    with pytest.raises(RuntimeError, match="before load"):
        driver.run()


def test_mock_driver_reports_the_bytes_it_actually_loaded(tmp_path: Path):
    model = tmp_path / "m.pt"
    model.write_bytes(BODY)
    driver = MockRobotDriver()
    driver.load(model, "franka")
    obs = driver.run()
    assert obs.ok and obs.steps == 1
    assert f"{len(BODY)} bytes" in obs.detail
    assert driver.loaded is not None and driver.loaded[1] == "franka"


def test_unknown_driver_is_a_hard_error():
    with pytest.raises(ValueError, match="mock"):
        build_driver("ros2")

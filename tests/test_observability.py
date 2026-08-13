"""Observability：request_id 中间件 + 结构化日志脱敏 + /metrics 指标。

要求（SECURITY.md T5 / PRODUCT_SPEC Observability）：
- 日志中不得出现 password/token/secret（脱敏验证）。
- 响应头携带 X-Request-Id。
"""

import io
import json
import logging

from fastapi.testclient import TestClient

from app.logging_setup import RedactingFormatter, _redact_message
from app.main import app


def test_log_redaction():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter())
    logger = logging.getLogger("embodiedcloud.test-redact")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("login attempt", extra={"password": "supersecret123", "token": "abc", "user_id": "u1"})
    out = stream.getvalue()
    parsed = json.loads(out)
    assert parsed["password"] == "[REDACTED]"
    assert parsed["token"] == "[REDACTED]"
    assert "supersecret123" not in out
    assert parsed["user_id"] == "u1"


def test_log_message_redaction():
    """message 文本中的敏感字段值也要脱敏（password/token/authorization）。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter())
    logger = logging.getLogger("embodiedcloud.test-msg-redact")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("login failed password=supersecret123 token:abc123")
    out = stream.getvalue()
    parsed = json.loads(out)
    assert parsed["message"] == "login failed password=[REDACTED] token:[REDACTED]"
    assert "supersecret123" not in out
    assert "abc123" not in out


def test_log_message_redaction_preserves_benign_message():
    """不含敏感字段的 message 原样保留。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter())
    logger = logging.getLogger("embodiedcloud.test-msg-ok")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("user u1 created workspace ws-1")
    parsed = json.loads(stream.getvalue())
    assert parsed["message"] == "user u1 created workspace ws-1"


def test_authorization_header_redaction():
    """Authorization 双词形态（Bearer/Basic）整体脱敏，token 明文不残留。"""
    out = _redact_message("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc")
    assert out == "Authorization: [REDACTED]"
    assert "eyJhbGciOi" not in out

    out2 = _redact_message("authorization: Basic dXNlcjpwYXNz")
    assert out2 == "authorization: [REDACTED]"
    assert "dXNlcjpwYXNz" not in out2


def test_redaction_does_not_overredact_benign():
    """普通消息（keyboard、无 =/: 形态）不误伤。"""
    assert _redact_message("user typed on keyboard") == "user typed on keyboard"
    assert _redact_message("monkey banana") == "monkey banana"
    assert _redact_message("no secrets here") == "no secrets here"


def test_metrics_expose_required_families():
    with TestClient(app) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        body = resp.text
        required = [
            "workspace_launch_total",
            "workspace_launch_failed_total",
            "workspace_launch_seconds",
            "workspace_running",
            "gpu_allocated",
            "gpu_seconds_total",
            "template_launch_total",
            "template_failure_total",
            "stream_session_total",
            "stream_failure_total",
        ]
        for m in required:
            assert m in body, f"missing metric family: {m}"


def test_request_id_header():
    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert "X-Request-Id" in resp.headers
        assert resp.headers["X-Request-Id"]

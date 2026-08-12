"""Observability：request_id 中间件 + 结构化日志脱敏 + /metrics 指标。

要求（SECURITY.md T5 / PRODUCT_SPEC Observability）：
- 日志中不得出现 password/token/secret（脱敏验证）。
- 响应头携带 X-Request-Id。
"""

import io
import json
import logging

from fastapi.testclient import TestClient

from app.logging_setup import RedactingFormatter
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

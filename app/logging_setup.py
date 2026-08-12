"""结构化日志 + request_id 中间件。

- JSON 日志（EMBODIEDCLOUD_LOG_JSON=true）便于采集；默认文本日志。
- RequestIDMiddleware：每个请求注入 request_id；响应头 X-Request-Id。
- 敏感字段脱敏：结构化字段若包含 password/token/secret/key/authorization 一律置 "[REDACTED]"。
"""

import json
import logging
import secrets
import sys
import time
import uuid

from fastapi import Request

from .config import Settings
from .models import Workspace

REDACT_KEYS = {"password", "token", "secret", "key", "authorization", "ide_password"}


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = dict(record.__dict__)
        for key in list(data):
            if any(rk in key.lower() for rk in REDACT_KEYS):
                data[key] = "[REDACTED]"
        data.pop("message", None)
        data.pop("asctime", None)
        msg = record.getMessage()
        skip = {
            "msg", "args", "exc_text", "stack_info", "created", "relativeCreated",
            "msecs", "levelno", "processName", "module", "funcName", "filename",
            "lineno", "threadName", "name", "levelname",
        }
        payload = {k: v for k, v in data.items() if k not in skip}
        payload["level"] = record.levelname
        payload["logger"] = record.name
        payload["message"] = msg
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_json:
        handler.setFormatter(RedactingFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    logging.getLogger("uvicorn.access").handlers = [handler]


class RequestIDMiddleware:
    """为每个请求生成 request_id 并注入上下文日志字段。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        start = time.monotonic()
        status_holder = {"status": 500}

        async def _send(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        logger = logging.getLogger("embodiedcloud.request")
        context: dict[str, object] = {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
        }
        logger.info("request start", extra={"request": context})
        try:
            await self.app(scope, receive, _send)
        finally:
            elapsed = time.monotonic() - start
            context["status"] = status_holder["status"]
            context["duration_ms"] = round(elapsed * 1000, 2)
            logger.info("request end", extra={"request": context})


def new_request_id() -> str:
    return secrets.token_hex(8)


def workspace_log_context(workspace: Workspace) -> dict:
    return {
        "workspace_id": workspace.id,
        "template_id": workspace.template_id,
        "user_id": workspace.user_id,
        "provider": workspace.provider,
        "gpu_id": workspace.gpu_id,
    }

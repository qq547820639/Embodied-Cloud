"""结构化日志 + request_id 中间件。

- JSON 日志（EMBODIEDCLOUD_LOG_JSON=true）便于采集；默认文本日志。
- RequestIDMiddleware：每个请求注入 request_id；响应头 X-Request-Id。
- 敏感字段脱敏：结构化字段若包含 password/token/secret/key/authorization 一律置 "[REDACTED]"。
"""

import json
import logging
import re
import sys
import time
import uuid

from fastapi import Request

from .config import Settings

REDACT_KEYS = {"password", "token", "secret", "key", "authorization", "ide_password"}

# message 脱敏正则：匹配「敏感字段名 = / : 值」，把值替换为 [REDACTED]。
# 复用 REDACT_KEYS 作为公共常量（长度降序保证 alternation 稳定），覆盖
# password=xxx / token: xxx 等常见单 token 日志形态。
# authorization 单独处理（见 _AUTH_HEADER_REDACT_RE），因为 HTTP 头通常是
# 「scheme + credential」双词形态（Authorization: Bearer <jwt> / Basic <base64>），
# 单 token 正则会漏掉后半段明文。
_MESSAGE_REDACT_KEYS = REDACT_KEYS - {"authorization"}
_MESSAGE_REDACT_RE = re.compile(
    rf"(?i)(\b(?:{'|'.join(sorted(_MESSAGE_REDACT_KEYS, key=len, reverse=True))})\b\s*[=:]\s*)([^\s,]+)"
)
# authorization 值整体脱敏：scheme + credential（可多 token，逗号截止，避免误伤后续字段）
_AUTH_HEADER_REDACT_RE = re.compile(
    r"(?i)(\bauthorization\b\s*[=:]\s*)[^\s,]+(?:\s+[^\s,]+)*"
)


def _redact_message(message: str) -> str:
    """对日志 message 文本应用与结构化字段一致的敏感字段脱敏。"""
    # 先处理 authorization 双词形态（Bearer/Basic + credential），再处理其余单 token 字段
    message = _AUTH_HEADER_REDACT_RE.sub(r"\1[REDACTED]", message)
    return _MESSAGE_REDACT_RE.sub(r"\1[REDACTED]", message)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = dict(record.__dict__)
        for key in list(data):
            if any(rk in key.lower() for rk in REDACT_KEYS):
                data[key] = "[REDACTED]"
        data.pop("message", None)
        data.pop("asctime", None)
        msg = _redact_message(record.getMessage())
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

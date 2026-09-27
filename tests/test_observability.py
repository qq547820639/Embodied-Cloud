"""Observability：request_id 中间件 + 结构化日志脱敏 + /metrics 指标。

要求（SECURITY.md T5 / PRODUCT_SPEC Observability）：
- 日志中不得出现 password/token/secret（脱敏验证）。
- 响应头携带 X-Request-Id。
"""

import ast
import io
import json
import logging
from pathlib import Path

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


def test_every_prometheus_metric_family_is_documented() -> None:
    """`app/metrics.py` 里声明的每一个指标族，都必须写在架构文档 §8 的清单里。

    §8 原先写的是缩写（"workspace_launch_total/failed/seconds"），这种写法既机器查不了、
    也会在新增一族时悄悄漏掉 —— 本轮加 warm_pool_prewarm_* 三族时就是这样：
    代码有了、文档与告警口径没有。改成逐个全名核对。
    """
    tree = ast.parse(Path("app/metrics.py").read_text(encoding="utf-8"))
    names = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"Counter", "Gauge", "Histogram"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert len(names) >= 12, f"从代码里只解析出 {len(names)} 个指标族，判据的分母不可信：{sorted(names)}"
    doc = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    section = doc.split("## 8. 可观测性", 1)[1].split("\n## ", 1)[0]
    missing = sorted(name for name in names if name not in section)
    assert not missing, f"这些指标族没写进 §8：{missing}"


def _declared_metrics() -> dict[str, list[str]]:
    """`{族名: 标签}`；解析实现与 test_api 的暴露对账共用 `tests/metrics_spec.py` 一份。"""
    from tests.metrics_spec import declared_metrics

    return {name: list(spec["labels"]) for name, spec in declared_metrics().items()}


def test_metrics_table_documents_the_label_set_too() -> None:
    """§8 的指标表要连**标签维度**一起写，并与代码声明逐项双向对账。

    只核族名不够：告警与查询都按标签写（`workspace_launch_total{provider=...}`），
    标签一变，文档就又在描述一个不存在的指标。N-39 加三族时就是把"族名"补上了，
    标签还没人核 —— 这条补上那一半。
    """
    from pathlib import Path as _P

    declared = _declared_metrics()
    assert len(declared) >= 17, f"从代码只解析出 {len(declared)} 族，分母不可信"
    doc = _P("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    section = doc.split("## 8. 可观测性", 1)[1].split("\n## ", 1)[0]
    documented: dict[str, list[str]] = {}
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        name = cells[0].strip("`")
        labels = [s.strip() for s in cells[1].strip("`").split(",")] if cells[1] not in ("", "—") else []
        documented[name] = [s for s in labels if s]
    assert documented, "§8 里读不到指标表（判据会恒真）"
    only_doc = sorted(set(documented) - set(declared))
    only_code = sorted(set(declared) - set(documented))
    assert not only_doc, f"文档写了代码里没有的族：{only_doc}"
    assert not only_code, f"这些族没进文档表：{only_code}"
    wrong = {n: (documented[n], declared[n]) for n in documented if documented[n] != declared[n]}
    assert not wrong, f"标签维度与声明不符：{wrong}"


def test_alert_table_only_references_declared_metrics() -> None:
    """运维文档表格**首列**里的 snake_case 名字必须是真声明过的指标族。

    第一版扫全文，被 `wait_ready`（provider 的方法名，不是指标）假阳性打红 ——
    收紧到"只看表格首列"：写错指标名照样红，正文里的方法名不再参与，
    免得把判据退化成"文档不许出现下划线词"。
    """
    import re
    from pathlib import Path as _P

    text = _P("docs/OPERATIONS.md").read_text(encoding="utf-8")
    first_cells = [
        ln.strip("|").split("|")[0]
        for ln in text.splitlines()
        if ln.startswith("|") and set(ln) - set("|-: ")
    ]
    tokens: set[str] = set()
    for cell in first_cells:
        tokens.update(re.findall(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b", cell))
    assert tokens, "OPERATIONS 的表格首列里没有任何指标名：本判据恒真"
    declared = set(_declared_metrics())
    unknown = sorted(tokens - declared)
    assert not unknown, f"表格首列引用了不存在的指标族：{unknown}"

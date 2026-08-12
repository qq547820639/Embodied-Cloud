"""Prometheus 指标（/metrics）。

必须指标（见 PRODUCT_SPEC Observability）：
workspace_launch_total / workspace_launch_failed_total / workspace_launch_seconds /
workspace_running / gpu_allocated / gpu_seconds / template_launch_total /
template_failure_total / stream_session_total / stream_failure_total
"""

from prometheus_client import Counter, Gauge, Histogram

WORKSPACE_LAUNCH_TOTAL = Counter(
    "workspace_launch_total",
    "Total workspace launch attempts",
    ["template_id", "provider"],
)
WORKSPACE_LAUNCH_FAILED_TOTAL = Counter(
    "workspace_launch_failed_total",
    "Total failed workspace launches",
    ["template_id", "provider"],
)
WORKSPACE_LAUNCH_SECONDS = Histogram(
    "workspace_launch_seconds",
    "Workspace provisioning duration in seconds",
    ["template_id"],
    buckets=(1, 5, 10, 15, 30, 60, 120, 300),
)
WORKSPACE_RUNNING = Gauge(
    "workspace_running",
    "Number of RUNNING workspaces",
)
GPU_ALLOCATED = Gauge(
    "gpu_allocated",
    "Number of ALLOCATED GPUs",
)
GPU_SECONDS = Counter(
    "gpu_seconds_total",
    "Accumulated GPU seconds billed",
)
TEMPLATE_LAUNCH_TOTAL = Counter(
    "template_launch_total",
    "Total launches per template",
    ["template_id"],
)
TEMPLATE_FAILURE_TOTAL = Counter(
    "template_failure_total",
    "Total failures per template",
    ["template_id"],
)
STREAM_SESSION_TOTAL = Counter(
    "stream_session_total",
    "Total streaming sessions created",
    ["workspace_id"],
)
STREAM_FAILURE_TOTAL = Counter(
    "stream_failure_total",
    "Total streaming session failures",
    ["workspace_id"],
)
WARM_POOL_READY = Gauge(
    "warm_pool_ready",
    "Number of READY warm runtime workspaces",
)
WARM_POOL_CLAIM_TOTAL = Counter(
    "warm_pool_claim_total",
    "Total warm pool claim attempts",
)
WARM_POOL_CLAIM_FAILED = Counter(
    "warm_pool_claim_failed",
    "Total failed warm pool claims (race/empty)",
)


def record_workspace_launch_start(template_id: str, provider: str) -> None:
    WORKSPACE_LAUNCH_TOTAL.labels(template_id=template_id, provider=provider).inc()
    TEMPLATE_LAUNCH_TOTAL.labels(template_id=template_id).inc()


def record_workspace_launch_failure(template_id: str, provider: str) -> None:
    WORKSPACE_LAUNCH_FAILED_TOTAL.labels(template_id=template_id, provider=provider).inc()
    TEMPLATE_FAILURE_TOTAL.labels(template_id=template_id).inc()


def record_workspace_launch_duration(template_id: str, seconds: float) -> None:
    WORKSPACE_LAUNCH_SECONDS.labels(template_id=template_id).observe(seconds)


def record_gpu_seconds(seconds: int) -> None:
    GPU_SECONDS.inc(seconds)

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
# claim 路径自己的耗时。workspace_launch_seconds 只在 provision 路径上 observe，而 warm pool
# 的全部意义是不走 provision —— 没有这一条，"P50<15s／P95<30s" 在产品最快的那条路径上无法回答。
# bucket 上界仍留 15/30 两个刻度，与 PRODUCT_SPEC 的 SLA 目标同值，便于直接查分位。
WARM_POOL_CLAIM_SECONDS = Histogram(
    "warm_pool_claim_seconds",
    "Warm pool claim duration in seconds (successful claims only)",
    ["template_id"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15, 30, 60),
)


def record_workspace_launch_start(template_id: str, provider: str) -> None:
    WORKSPACE_LAUNCH_TOTAL.labels(template_id=template_id, provider=provider).inc()
    TEMPLATE_LAUNCH_TOTAL.labels(template_id=template_id).inc()


def record_workspace_launch_failure(template_id: str, provider: str) -> None:
    WORKSPACE_LAUNCH_FAILED_TOTAL.labels(template_id=template_id, provider=provider).inc()
    TEMPLATE_FAILURE_TOTAL.labels(template_id=template_id).inc()


def record_workspace_launch_duration(template_id: str, seconds: float) -> None:
    WORKSPACE_LAUNCH_SECONDS.labels(template_id=template_id).observe(seconds)


def record_warm_pool_claim_duration(template_id: str, seconds: float) -> None:
    WARM_POOL_CLAIM_SECONDS.labels(template_id=template_id).observe(seconds)


def record_gpu_seconds(seconds: int) -> None:
    GPU_SECONDS.inc(seconds)

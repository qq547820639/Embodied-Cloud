"""Prometheus 指标（/metrics）。

必须指标（见 PRODUCT_SPEC Observability）：
workspace_launch_total / workspace_launch_failed_total / workspace_launch_seconds /
workspace_running / gpu_allocated / gpu_seconds / template_launch_total /
template_failure_total / stream_session_total / stream_failure_total
"""

from typing import Any

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
# 池内那一格自己的启动族（N-39）。warm pool 的补位是内部行为，它也会失败——与交互式
# 请求抢同一个原子分配器时谁先到谁得，输掉那格只是浪费一次尝试。把这些记进
# workspace_launch_* 会同时算错三件事：用户启动量、启动失败率（docs/OPERATIONS.md
# 对 workspace_launch_failed_total 的告警口径是"增量 >0 持续 10min"）、
# 以及 workspace_launch_seconds 的 P50/P95（PRODUCT_SPEC 的 SLA 判据用的就是它）。
WARM_POOL_PREWARM_TOTAL = Counter(
    "warm_pool_prewarm_total",
    "Total provisioning attempts made by the warm pool itself",
    ["template_id"],
)
WARM_POOL_PREWARM_FAILED_TOTAL = Counter(
    "warm_pool_prewarm_failed_total",
    "Total warm pool provisioning attempts that failed",
    ["template_id"],
)
WARM_POOL_PREWARM_SECONDS = Histogram(
    "warm_pool_prewarm_seconds",
    "Duration of warm pool provisioning attempts",
    ["template_id"],
    buckets=(0.1, 1, 5, 10, 15, 30, 60, 120, 300),
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


def record_gpu_seconds(delta_seconds: int) -> None:
    """将账本次新增的 GPU 秒数累进 `gpu_seconds_total`（Counter）。

    入参必须是**账本的增量**（`_settle_run_delta` 的第二个读数），不是"这一段值多少秒"，
    也不是重算出来的 elapsed（N-75）：counter 不幂等，同一个运行段在 stop 重试或
    reconcile 再来一趟时那个段值仍是同一个数，加两次就让暴露量与账本分叉。
    增量恒 ≥ 0（账本 append-only）；真拿到负数时 `Counter.inc` 自己抛 ValueError，
    这里不夹平也不改类型——宁可红，不静默。
    """
    GPU_SECONDS.inc(delta_seconds)


def is_pool_runtime(workspace: Any) -> bool:
    """这一格是不是 warm pool 自己开的（而不是用户按下的启动）。

    只认一个字段：池内 runtime 一定带 `warm_pool_state`（PREWARMING/READY/DRAINING…）。
    不用 `user_id is None` 判 —— 交互式请求也可以无归属（取证台的 probe 就是），
    那样会把用户的启动错分进池的族，两族同时算错。
    """
    return getattr(workspace, "warm_pool_state", None) is not None


def record_launch_start(workspace: Any, template_id: str, provider: str) -> None:
    """一次 provision 开始：按"谁发起的"分族计数。"""
    if is_pool_runtime(workspace):
        WARM_POOL_PREWARM_TOTAL.labels(template_id=template_id).inc()
        return
    record_workspace_launch_start(template_id, provider)


def record_launch_failure(workspace: Any, template_id: str, provider: str) -> None:
    if is_pool_runtime(workspace):
        WARM_POOL_PREWARM_FAILED_TOTAL.labels(template_id=template_id).inc()
        return
    record_workspace_launch_failure(template_id, provider)


def record_launch_duration(workspace: Any, template_id: str, seconds: float) -> None:
    """耗时观测同样分族：`workspace_launch_seconds` 的 P50/P95 是 SLA 判据的读数面，
    池内补位的耗时不是用户感受到的启动时间。"""
    if is_pool_runtime(workspace):
        WARM_POOL_PREWARM_SECONDS.labels(template_id=template_id).observe(seconds)
        return
    record_workspace_launch_duration(template_id, seconds)

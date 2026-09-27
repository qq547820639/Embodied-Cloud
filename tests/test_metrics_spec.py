"""`tests/metrics_spec.py` 里那几个解析/派生函数的判据。

为什么单独一支：运行时的 `test_metrics_exposition_is_what_the_code_declares` 只能证明
"现在跑得通"，证明不了 ③（标签越界）那条分支会不会开火 —— 在一个活进程里
prometheus_client 自己就拒绝未声明的标签键，所以那条只能在**解析层**证伪。
这里同时把"命名规则来自实测而不是记忆"钉住：拿装好的 prometheus_client 现场派生序列名，
与我方 `allowed_series()` 的推导比一次。
"""

from __future__ import annotations

import prometheus_client as pc

from tests.metrics_spec import allowed_series, expected_labels, parse_exposition


def test_expected_labels_only_widens_for_histogram_buckets() -> None:
    assert expected_labels("workspace_launch_seconds_bucket", {"template_id"}) == {"template_id", "le"}
    assert expected_labels("workspace_launch_seconds_count", {"template_id"}) == {"template_id"}
    assert expected_labels("workspace_launch_seconds", {"template_id"}) == {"template_id"}
    # 反向对照：`le` 不许漏进非 bucket 序列，否则 ③ 那条分支就是摆设
    assert "le" not in expected_labels("workspace_launch_seconds_sum", {"template_id"})


def test_parse_exposition_reads_a_known_sample() -> None:
    """自己写的正则要先喂一份已知样本：0 命中与错命中都长得像"没问题"。"""
    text = (
        "# HELP gpu_allocated Number of ALLOCATED GPUs\n"
        "# TYPE gpu_allocated gauge\n"
        "gpu_allocated 3\n"
        'workspace_launch_total{template_id="cartpole",provider="mock"} 2.0\n'
        'workspace_launch_seconds_bucket{template_id="cartpole",le="5.0"} 1.0\n'
        'workspace_launch_seconds_count{template_id="cartpole"} 2.0\n'
        "this line is not a sample at all\n"
    )
    parsed = parse_exposition(text)
    assert parsed == {
        "gpu_allocated": set(),
        "workspace_launch_total": {"template_id", "provider"},
        "workspace_launch_seconds_bucket": {"template_id", "le"},
        "workspace_launch_seconds_count": {"template_id"},
    }, parsed
    # ③ 的开火路径：喂一份带越界标签的暴露文本，比较逻辑必须点名它
    drift = {'workspace_launch_total{template_id="c",provider="m",cluster="eu"} 1.0'}
    observed = parse_exposition("\n".join(drift))
    extra = observed["workspace_launch_total"] - expected_labels(
        "workspace_launch_total", {"template_id", "provider"}
    )
    assert extra == {"cluster"}, extra


def test_allowed_series_matches_the_librarys_own_naming() -> None:
    """派生规则与装好的 prometheus_client 现场对齐（规则记在 metrics_spec 的 docstring 里）。"""
    registry = pc.CollectorRegistry()
    counter = pc.Counter(
        "probe_launch_total", "d", ["template_id", "provider"], registry=registry
    )
    counter.labels(template_id="t", provider="p").inc()
    gauge = pc.Gauge("probe_ready", "d", registry=registry)
    gauge.set(1)
    hist = pc.Histogram("probe_seconds", "d", ["template_id"], registry=registry)
    hist.labels(template_id="t").observe(0.5)
    observed = {s.name for metric in registry.collect() for s in metric.samples}
    declared = {
        "probe_launch_total": {"kind": "Counter", "labels": ["template_id", "provider"]},
        "probe_ready": {"kind": "Gauge", "labels": []},
        "probe_seconds": {"kind": "Histogram", "labels": ["template_id"]},
    }
    allowed = set(allowed_series(declared))
    assert observed <= allowed, f"库派出了规则没覆盖的序列：{sorted(observed - allowed)}"
    assert {"probe_launch_total", "probe_launch_created", "probe_seconds_bucket"} <= observed

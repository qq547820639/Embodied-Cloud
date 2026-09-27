"""测试侧共享的指标规格：从 `app/metrics.py` 的 AST 取声明，从 `/metrics` 文本取实际暴露。

放在测试工具里而不是各测试文件内部：文档对账与暴露对账必须用同一份解析，
否则"两份实现互相相等"什么都不证明。

序列命名规则不是推测——2026-09-27 在本机 `.venv` 的 prometheus_client 0.26.0 上实测
（Counter/Gauge/Histogram/Summary 各建一个、打一次点再 `collect()`）：

    Counter   族基名 = 声明名去掉尾部 `_total` ⇒ {基名_total, 基名_created}
    Gauge     {声明名}
    Histogram {基名_bucket, 基名_count, 基名_sum, 基名_created}   # 没有裸基名
    Summary   {基名_count, 基名_sum, 基名_created}
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
KINDS = ("Counter", "Gauge", "Histogram", "Summary", "Info", "Enum")

COUNTER_SUFFIXES = ("_total", "_created")
HISTOGRAM_SUFFIXES = ("_bucket", "_count", "_sum", "_created")
SUMMARY_SUFFIXES = ("_count", "_sum", "_created")
PLAIN_SUFFIXES = ("",)

SUFFIXES_BY_KIND: dict[str, tuple[str, ...]] = {
    "Counter": COUNTER_SUFFIXES,
    "Histogram": HISTOGRAM_SUFFIXES,
    "Summary": SUMMARY_SUFFIXES,
    "Gauge": PLAIN_SUFFIXES,
    "Info": PLAIN_SUFFIXES,
    "Enum": PLAIN_SUFFIXES,
}


def family_base(name: str, kind: str) -> str:
    """声明名 → prometheus 的族基名（只有 Counter 会吃掉尾部的 `_total`）。"""
    if kind == "Counter" and name.endswith("_total"):
        return name[: -len("_total")]
    return name


def declared_metrics(source: Path | None = None) -> dict[str, dict[str, object]]:
    """`{声明名: {"kind": 类型, "labels": [标签…]}}`，取自模块级赋值。"""
    tree = ast.parse((source or REPO / "app" / "metrics.py").read_text(encoding="utf-8"))
    out: dict[str, dict[str, object]] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        func = node.value.func
        kind = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if kind not in KINDS or not node.value.args:
            continue
        first = node.value.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        labels: list[str] = []
        for arg in node.value.args[1:]:
            if isinstance(arg, ast.List):
                labels = [e.value for e in arg.elts if isinstance(e, ast.Constant)]
                break
        out[first.value] = {"kind": kind, "labels": labels}
    return out


def allowed_series(declared: dict[str, dict[str, object]]) -> dict[str, str]:
    """`{可派生的序列名: 声明名}`；表外的序列名即"声明之外的暴露"。"""
    allowed: dict[str, str] = {}
    for name, spec in declared.items():
        kind = str(spec["kind"])
        base = family_base(name, kind)
        for suffix in SUFFIXES_BY_KIND.get(kind, PLAIN_SUFFIXES):
            allowed[f"{base}{suffix}"] = name
    return allowed


def expected_labels(series: str, family_labels: set[str]) -> set[str]:
    """某条序列允许的标签键：声明标签 + （仅 `_bucket`）histogram 自带的 `le`。"""
    return family_labels | ({"le"} if series.endswith("_bucket") else set())


def app_prefixes(declared: dict[str, dict[str, object]]) -> tuple[str, ...]:
    """本仓指标的第一段前缀（保留给需要粗筛的场合；判据不再靠它划界，见 `builtin_registered_names`）。"""
    return tuple(sorted({name.split("_")[0] for name in declared}))


def registered_names() -> dict[str, bool]:
    """默认注册表里每个序列名 → "是不是由本仓的指标对象注册的"。

    区分方式实测过：`prometheus_client.metrics.MetricWrapperBase` 的实例就是我们
    `Counter()/Gauge()/Histogram()` 建出来的对象；prometheus 自带的平台收集器
    （`python_gc_*`、`python_info`…）不是。注册表内部结构一旦改名，这里**显式失败**，
    绝不退回"按前缀猜"——那正是 N-43 留下的边界洞。
    """
    import prometheus_client as pc
    from prometheus_client.metrics import MetricWrapperBase

    mapping = getattr(pc.REGISTRY, "_collector_to_names", None)
    if mapping is None:
        raise AssertionError(
            "prometheus_client 的注册表结构变了（读不到 _collector_to_names）："
            "本判据失去划界能力，必须显式红，不能退回前缀猜测"
        )
    out: dict[str, bool] = {}
    for collector, names in mapping.items():
        ours = isinstance(collector, MetricWrapperBase)
        for name in names:
            out[str(name)] = ours
    return out


def declared_series(declared: dict[str, dict[str, object]]) -> set[str]:
    """声明 + 其族基名（`# TYPE` 行与 Gauge 用基名，Counter/Histogram 用派生名）。"""
    out = set(declared)
    for name, spec in declared.items():
        out.add(family_base(name, str(spec["kind"])))
        out.add(name)
    out |= set(allowed_series(declared))
    return out


SERIES_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)\s*$"
)
VALUE_TOKENS = re.compile(r"^-?(\d+(\.\d+)?([eE][-+]?\d+)?|NaN|\+Inf|-Inf)$")


def parse_exposition(text: str) -> dict[str, set[str]]:
    """`{序列名: 标签键集合}`；`# HELP`/`# TYPE` 注释行与非"`name[labels] 数值`"形状的行不参与。

    数值那一列必须真的像个数：第一版没校验，把 `this line is not a sample at all` 解析成了
    一个叫 `this` 的族（喂已知样本时才暴露）。
    """
    out: dict[str, set[str]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = SERIES_RE.match(line)
        if not match or not VALUE_TOKENS.match(match.group("value")):
            continue
        keys = {part.split("=", 1)[0].strip() for part in (match.group("labels") or "").split(",") if "=" in part}
        out.setdefault(match.group("name"), set()).update(keys)
    return out

"""配置面文档一致性：OPERATIONS.md 规定「新增配置必须在 .env.example 文档化」，
本文件把这条约定变成常驻判据——判据取自 `Settings` 的字段清单，不是手抄列表。

另一半管"惰性开关"：一个谁都不读的字段（设了不生效）是最容易骗到运维的配置项，
所以「无人读取」与「.env.example 里写明未启用」必须同时成立，且两个方向都会红。
"""

import ast
import re
from pathlib import Path

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# .env.example 里的键一律是 EMBODIEDCLOUD_<FIELD 大写>（可注释掉）
KEY_RE = re.compile(r"^#?\s*EMBODIEDCLOUD_([A-Z0-9_]+)\s*=", re.MULTILINE)


def _documented_fields() -> set[str]:
    return {m.group(1).lower() for m in KEY_RE.finditer(ENV_EXAMPLE.read_text())}


def test_every_settings_field_is_documented_in_env_example() -> None:
    undocumented = sorted(set(Settings.model_fields) - _documented_fields())
    assert not undocumented, f"以下配置未写进 .env.example：{undocumented}"


def test_env_example_keys_all_exist_in_settings() -> None:
    """反向：文档里不能有 Settings 已不认识的键（改了名/删了字段的残留）。"""
    stale = sorted(_documented_fields() - set(Settings.model_fields))
    assert not stale, f".env.example 有 Settings 里不存在的键：{stale}"


# ---------------------------------------------------------------------------
# 惰性开关：谁都不读的字段，必须在 .env.example 里明说"设了也不生效"
# ---------------------------------------------------------------------------

# 登记处。**双向**核对：出现"无人读取却没登记"（运维会以为它生效）或
# "登记了却已被读取"（文档在撒谎）都必须红。
INERT_SETTINGS: dict[str, str] = {
    "default_idle_timeout_minutes": (
        "缺可信的活动信号：容器 CPU 在 GPU 训练下会长时间接近 0，据此自动停机会误杀长跑任务并照秒扣费；"
        "可信信号（真机 GPU 利用率）被 NVIDIA 设备阻塞，见 docs/CURRENT_STATE.md §4 TECH DEBT"
    ),
}
INERT_MARKER = "未启用"
DECLARATION_FILE = "config.py"


def _app_sources(app_root: Path = APP_ROOT) -> list[Path]:
    return [
        p
        for p in sorted(app_root.rglob("*.py"))
        if "__pycache__" not in p.parts and p.name != DECLARATION_FILE
    ]


def _read_sites(field: str, files: list[Path]) -> list[str]:
    r"""一个字段在 `app/`（声明文件除外）里的读取位置，两种形态都算：

- 属性访问 `settings.<field>` / `self.settings.<field>`（AST 的 `Attribute.attr`，
  不看接收者是谁——真实代码里两种包装都有）；
- 字符串形式 `getattr(settings, "<field>")` / `dict["<field>"]`（AST 的字符串常量）。

命中即需人工过一眼：登记为惰性就意味着它不该出现在任何读取位置，
误报的代价是一次确认，漏报的代价是运维按一份谎言似的最小配置示例设值。
    """
    sites: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == field:
                sites.append(f"{path}:{node.lineno}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == field:
                sites.append(f"{path}:{node.lineno} (字符串形式)")
    return sites


def _inert_marker_missing_fields(text: str) -> list[str]:
    """`EMBODIEDCLOUD_<FIELD>=` 上方紧邻的注释区里必须有惰性标记。"""
    offenders: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^#?\s*EMBODIEDCLOUD_([A-Z0-9_]+)\s*=", line)
        if not m or m.group(1).lower() not in INERT_SETTINGS:
            continue
        block: list[str] = []
        j = i - 1
        while j >= 0 and lines[j].strip().startswith("#"):
            block.append(lines[j])
            j -= 1
        if not any(INERT_MARKER in b for b in block):
            offenders.append(m.group(1).lower())
    return offenders


def test_inert_settings_are_exactly_the_unread_fields() -> None:
    """未登记＝有人会被"设了就生效"误导；登记了却已被读＝文档在撒谎。"""
    files = _app_sources()
    assert files, "app/ 下没解析到任何源文件（排除声明文件后）——判据会恒真"
    unread = {f for f in Settings.model_fields if not _read_sites(f, files)}
    assert unread == set(INERT_SETTINGS), (
        f"无人读取的字段集合与惰性登记不符："
        f"漏登记 {sorted(unread - set(INERT_SETTINGS))} / 死登记 {sorted(set(INERT_SETTINGS) - unread)}"
    )


def test_read_probe_can_actually_see_reads() -> None:
    """非恒真控制：拿一个确实被读的字段走同一个探针，必须给非空结果。"""
    sites = _read_sites("ide_port_start", _app_sources())
    assert sites, "探针在已知有读取者的字段上读到 0 处——上一条判据是恒真的"
    assert _read_sites("no_such_settings_field", _app_sources()) == []


def test_inert_fields_are_marked_inert_in_env_example() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert INERT_SETTINGS, "没有登记任何惰性字段——下面这条判据会恒真"
    offenders = _inert_marker_missing_fields(text)
    assert not offenders, f"以下惰性开关未在 .env.example 标注「{INERT_MARKER}」：{offenders}"


def test_inert_marker_fires_without_the_annotation() -> None:
    """开火/不开火两侧：注释里没有惰性标记必须被点出来，写了就不开火。"""
    field = next(iter(INERT_SETTINGS))
    key = f"EMBODIEDCLOUD_{field.upper()}=60"
    assert _inert_marker_missing_fields(f"# 预留：当前{INERT_MARKER}（缺活动信号）\n{key}\n") == []
    assert _inert_marker_missing_fields(f"# 空闲超时（分钟）\n{key}\n") == [field]
    assert _inert_marker_missing_fields(f"{key}\n") == [field]
    # "预留"两个字不算标记：真正误导运维的是"设了就会生效"，只有明确说未启用才算澄清
    assert _inert_marker_missing_fields(f"# 预留\n{key}\n") == [field]
    # 标记必须紧邻该键，不能靠远处一句话
    assert _inert_marker_missing_fields(f"# {INERT_MARKER}\n\n{key}\n") == [field]

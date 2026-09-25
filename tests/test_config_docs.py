"""配置面文档一致性：OPERATIONS.md 规定「新增配置必须在 .env.example 文档化」，
本文件把这条约定变成常驻判据——判据取自 `Settings` 的字段清单，不是手抄列表。
"""

import re
from pathlib import Path

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

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

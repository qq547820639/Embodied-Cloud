"""构建时间基准的唯一来源。

`SOURCE_DATE_EPOCH` 决定 wheel zip 条目与 sdist tar 条目的 mtime；不钉它，同一棵树立
两次就得到两个 sha，`dist/checksums.txt` 那行只能当"某一次构建的记录"。

为什么要单独立一个文件：上一轮我在 Makefile 与 validate 各写了一遍
`git log -1 --format=%ct`，再靠"两处格式串必须相同"的文本判据兜着 —— 那是把重复当事实用。
现在口径只有一份（常驻判据按"`%ct` 在 scripts/ 下只出现一次"钉），
Makefile 那一条仍然保留，因为 `make build` 不经过本模块。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def epoch_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """在 base（默认当前环境）之上补一个 SOURCE_DATE_EPOCH。

    - 调用方已经给了值（比如 `make` 那一条 export）⇒ 原样保留，本模块不越权改写；
    - 给了空串 ⇒ 视同没给（空值传进 build 会让 zip 时间变成 1970 之外的未定义行为）；
    - 取不到 git ⇒ 兜底 "0" 而不是 None，宁要一个确定的数，也不要"这步悄悄不钉时间"。
    """
    env = dict(os.environ if base is None else base)
    if not env.get("SOURCE_DATE_EPOCH"):
        stamp = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],  # noqa: S607 git 由 PATH 解析，参数是写死的常量
            cwd=ROOT, text=True, capture_output=True, timeout=60,
        )
        env["SOURCE_DATE_EPOCH"] = stamp.stdout.strip() or "0"
    return env

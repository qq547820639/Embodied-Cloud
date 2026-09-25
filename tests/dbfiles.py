"""模块级测试库的进程隔离命名。

这些用例在 import 期就建 engine（模块级 `ENGINE`），文件名过去是写死的
`test-<名字>.db`。同一份仓库里并发跑两个 pytest 时，两个进程会打开并清空同一个
文件库——实测一次读出 30 例假红（登录/隔离/账本类全断），串行即绿。

 pid 后缀让每个进程拥有自己的库文件；`*.db` 已在 .gitignore 内，`make clean`
 负责收 `test-*.db`。
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def db_name(logical: str) -> str:
    """逻辑库名（如 'scheduler'）→ 本进程独占的文件名。"""
    return f"test-{logical}-{os.getpid()}.db"


def db_url(logical: str) -> str:
    return f"sqlite:///./{db_name(logical)}"


def db_path(logical: str) -> Path:
    return REPO_ROOT / db_name(logical)

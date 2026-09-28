"""模块级测试库的进程隔离命名。

这些用例在 import 期就建 engine（模块级 `ENGINE`），文件名过去是写死的
`test-<名字>.db`。同一份仓库里并发跑两个 pytest 时，两个进程会打开并清空同一个
文件库——实测一次读出 30 例假红（登录/隔离/账本类全断），串行即绿。

 pid 后缀让每个进程拥有自己的库文件；`*.db` 已在 .gitignore 内，`make clean`
 负责收 `test-*.db`。

 但"手工 clean 负责收"在实践中是不够的：一个模块级 `ENGINE` 就是一个库文件，实测
 一整轮跑完给同一个 pid 留下 **28 份**（那 316 个 pid 的中位数是 11 份，即多数趟在
 中途崩或被掐）；2026-09-28 数过本仓根目录共 **3497 个 `test-*.db`、1.59 GB、来自
 316 个不同 pid**——而 `conftest.py` 的会话收尾只删它自己那一份
 `test-embodiedcloud-<pid>.db`。于是这里补上同一形状的清扫：
 **只删文件名尾部 pid 属于本进程的那些**（别的并发跑的活库一律不碰——那正是当初加 pid
 后缀要防的事，清扫不能把它反过来做成互相删库）。

 崩溃／被掐那一趟的遗留则由 `sweep_dead_test_dbs` 收（N-107）：原先把这一半交给
 `make clean`，而那条目标没人跑，实测就剩 2 个死 pid 的孤儿在仓根（本会话的 N-99 基准
 那一趟 479 KB、更早一轮的 n74b 探针 455 KB）——孤儿因此是永久累积而不是"下次 clean 就好"。
"""

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def db_name(logical: str) -> str:
    """逻辑库名（如 'scheduler'）→ 本进程独占的文件名。"""
    return f"test-{logical}-{os.getpid()}.db"


def db_url(logical: str) -> str:
    return f"sqlite:///./{db_name(logical)}"


def db_path(logical: str) -> Path:
    return REPO_ROOT / db_name(logical)


PID_SUFFIX_RE = re.compile(r"^(?P<head>test-.+)-(?P<pid>\d+)\.db$")


def pid_of(filename: str) -> int | None:
    """`test-<逻辑名>-<pid>.db` → pid；不符合该形状（如旧版无 pid 的名字）返回 None。"""
    match = PID_SUFFIX_RE.match(filename)
    return int(match.group("pid")) if match else None


def sweep_own_test_dbs(pid: int | None = None, roots: tuple[Path, ...] | None = None) -> list[Path]:
    """删掉**本进程自己**遗留的 `test-*-<pid>.db`，返回被删的路径。

    `pid`/`roots` 可注入是为了让判据能在别人的进程与会话目录上打靶；默认＝本进程与
    「URL 的 `./` 所在目录（CWD）＋ REPO_ROOT」两个落点（`db_url` 用相对路径，而
    `db_path` 用 REPO_ROOT，两者在 CWD≠仓库根时不是同一个目录，只扫一处会漏）。
    """
    owner = os.getpid() if pid is None else pid
    search = (Path.cwd(), REPO_ROOT) if roots is None else roots
    removed: list[Path] = []
    seen: set[Path] = set()
    for root in search:
        if not root.is_dir():
            continue
        for candidate in sorted(root.glob("test-*.db")):
            resolved = candidate.resolve()
            if resolved in seen or pid_of(candidate.name) != owner:
                continue
            seen.add(resolved)
            try:
                candidate.unlink()
            except OSError:
                continue  # 已被别人收走/正被占用：跳过它，不当成失败
            removed.append(candidate)
    return removed


def pid_is_alive(pid: int) -> bool:
    """只有"确定这个进程已经不在了"才返回 False。

    `ProcessLookupError` ⇒ 真死了；`PermissionError` ⇒ 是别人的进程，活着（不能因为
    问不到就删它的库）；其它 `OSError`（含 ESRCH 之外的任何意外）一律当作活着。
    方向是单边保守：判错的代价是"孤儿留着"，反过来的代价是"删掉并发跑的活库"。
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def sweep_dead_test_dbs(roots: tuple[Path, ...] | None = None) -> list[Path]:
    """收掉**归属进程已经不在了**的 `test-*-<pid>.db`，返回被删的路径（N-107）。

    与 `sweep_own_test_dbs` 的分工是时间而不是判据：那一只在会话**收尾**收本进程这一趟的；
    这一只在会话**开头**收上一趟崩掉／被掐后遗留的。三条一律不碰：
    ① 名字里没有 pid 后缀（旧形状，无从判断归属）；② pid 就是本进程（可能正是自己要用的）；
    ③ `pid_is_alive` 说不清／活着（含别人的进程）。因此 pid 复用只会让它更保守：
    死 pid 被新进程占了 ⇒ 判活着 ⇒ 留着。
    """
    search = (Path.cwd(), REPO_ROOT) if roots is None else roots
    mine = os.getpid()
    removed: list[Path] = []
    seen: set[Path] = set()
    for root in search:
        if not root.is_dir():
            continue
        for candidate in sorted(root.glob("test-*.db")):
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            owner = pid_of(candidate.name)
            if owner is None or owner == mine or pid_is_alive(owner):
                continue
            seen.add(resolved)
            try:
                candidate.unlink()
            except OSError:
                continue  # 正被占用/已被别人收走：跳过，不当成失败
            removed.append(candidate)
    return removed

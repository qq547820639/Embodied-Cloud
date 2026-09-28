"""会话收尾的库文件清扫：只收本进程的，别的一律不碰（N-95）。

来龙去脉在 `tests/dbfiles.py` 的模块 docstring 里（一句话：N-31 给库名加了 pid 后缀
解决"两个 pytest 并跑互相清库"，但每个模块级 ENGINE 都留下一份文件，除手工 `make clean`
外没人收；本仓根目录实测堆到 3497 个 / 1.59 GB / 316 个不同 pid）。

这一档判据钉的是两件容易被写反的事：
1. **误删方向**——清扫一旦按"名字像 test-*.db"就删，就会删掉并发跑那一份活库，
   而这正是当初加 pid 后缀要防的故障（历史上它造成过 30 例假红）。所以危险方向的
   反证（别人的 pid 必须活着、内容不变）比"自己的删掉了"更要紧。
2. **接没接上**——清扫函数写出来没人调用，等于没写。`conftest.py` 的会话收尾本来
   就只删它自己那一份，这条接线要被结构判据钉住，而不是靠下一次有人数文件。
"""

import ast
from pathlib import Path

from tests.dbfiles import REPO_ROOT, pid_of, sweep_own_test_dbs

SUITE_SRC = REPO_ROOT / "tests" / "conftest.py"


def _touch(root: Path, name: str, content: bytes = b"sqlite-ish") -> Path:
    path = root / name
    path.write_bytes(content)
    return path


def test_sweep_removes_exactly_this_pids_files(tmp_path: Path) -> None:
    mine = _touch(tmp_path, "test-a-1.db")  # 另一个 pid 的库
    mine2 = _touch(tmp_path, "test-b-7.db")  # 本跑（pid=7）的库
    other = _touch(tmp_path, "test-c-8.db")
    legacy = _touch(tmp_path, "test-legacy.db")
    journal = _touch(tmp_path, "test-a-7.db-journal")

    removed = sweep_own_test_dbs(pid=7, roots=(tmp_path,))

    assert [p.name for p in removed] == ["test-b-7.db"], [p.name for p in removed]
    assert not mine2.exists()
    assert mine.exists() and other.exists(), "不是本 pid 的被删了"
    assert legacy.exists(), "旧版无 pid 的名字不该由本进程认领（它可能属于任何还在跑的会话）"
    assert journal.exists(), "清扫范围只限 .db 本身，附属文件另有归属"
    mine.unlink()
    other.unlink()
    legacy.unlink()
    journal.unlink()


def test_sweep_never_touches_a_concurrent_runs_database(tmp_path: Path) -> None:
    """危险方向的独立一极：并发的另一跑必须逐字节不受影响。"""
    live = _touch(tmp_path, "test-session-999999.db", b"another run is writing this")
    _touch(tmp_path, "test-session-7.db")

    removed = sweep_own_test_dbs(pid=7, roots=(tmp_path,))

    assert [p.name for p in removed] == ["test-session-7.db"]
    assert live.read_bytes() == b"another run is writing this"


def test_pid_of_recognizes_only_the_suffixed_shape() -> None:
    """名字里带数字的逻辑名不能把 pid 抢错，缺 pid 的旧名字不能读成 0。"""
    assert pid_of("test-billing-123.db") == 123
    assert pid_of("test-pg16-42.db") == 42, "逻辑名里的数字不该被当成尾部"
    assert pid_of("test-embodiedcloud-1.db") == 1
    assert pid_of("test-billing.db") is None
    assert pid_of("x-billing-1.db") is None
    assert pid_of("test-a-1.db-journal") is None


def test_two_roots_are_swept_once_each(tmp_path: Path) -> None:
    """`db_url` 用 `./`（CWD），`db_path` 用 REPO_ROOT：CWD≠仓库根时两处都得收，且不重复删。"""
    first = tmp_path / "one"
    second = tmp_path / "two"
    for root in (first, second):
        root.mkdir()
        _touch(root, "test-x-7.db")

    removed = sweep_own_test_dbs(pid=7, roots=(first, second, first))

    assert len(removed) == 2, [str(p) for p in removed]
    assert not (first / "test-x-7.db").exists() and not (second / "test-x-7.db").exists()


def session_finish_offenders(source: str) -> list[str]:
    """`pytest_sessionfinish` 里缺了哪些收尾动作（返回缺项列表，空＝都接上了）。"""
    tree = ast.parse(source)
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "pytest_sessionfinish"),
        None,
    )
    if fn is None:
        return ["pytest_sessionfinish 不存在"]
    called = {
        call.func.id
        for call in (c for c in ast.walk(fn) if isinstance(c, ast.Call))
        if isinstance(call.func, ast.Name)
    }
    return [name for name in ("sweep_own_test_dbs", "db_name") if name not in called]


def test_session_finish_actually_calls_the_sweep() -> None:
    """接线判据：会话收尾既要删会话库，也要调本进程的清扫。"""
    assert session_finish_offenders(SUITE_SRC.read_text(encoding="utf-8")) == []


def test_the_wiring_ruler_fires_when_the_call_is_dropped() -> None:
    """反向对照：把清扫调用摘掉，尺子必须点名它；顺带钉"这尺子读得到东西"。"""
    unhooked = (
        "def pytest_sessionfinish(session, exitstatus):\n"
        '    Path(db_name("embodiedcloud")).unlink(missing_ok=True)\n'
    )
    assert session_finish_offenders(unhooked) == ["sweep_own_test_dbs"]
    assert session_finish_offenders("def other():\n    pass\n") == ["pytest_sessionfinish 不存在"]
    hooked = unhooked + "    sweep_own_test_dbs()\n"
    assert session_finish_offenders(hooked) == []

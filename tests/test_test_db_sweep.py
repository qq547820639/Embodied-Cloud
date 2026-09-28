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

N-107 补上漏掉的另一半：`sweep_own_test_dbs` 只收"本进程这一趟"的，崩溃／被掐那一趟的
遗留原设计交给 `make clean`，而本仓没有任何循环会跑它——实测仓根就留着 2 个死 pid 的孤儿
（本会话 N-99 基准那一趟 479 KB、更早一轮 n74b 探针 455 KB）。新增的
`sweep_dead_test_dbs` 接在会话**开头**，只删"归属进程确定已不在"的那些，
方向单边保守（活 pid／本 pid／无 pid 后缀／别的名字一律不碰），判错的代价因此只会是
"孤儿多留一轮"，不会是"删掉别人的活库"。

牙齿（七臂变异电池实测，2026-09-28；各臂恢复后 `cmp` 逐字节相同、末跑 11 passed）：
基线与无关注释臂 0 红。H2「谁都不收」⇒ D1+D5；H4「死了说活着」⇒ D1+D2+D5；
H5「问不到（EPERM）说死了」⇒ 只红 D2（保守方向那一极有独立的牙）；
H1「不看死活」⇒ 只红 D1（真活子进程那格被删）；H3「摘掉接线」⇒ 只红 D3。
两条读数纪律：① H1/H4 两支为了让变异不在真仓库根上删别人的库，同时把 conftest 那一脚
中和成 `pass`，因此 D3 的红是**安全中和**造成的，不是臂的语义——记在两笔里；
② 首轮我把 H1 的 expect 写成"D1+D2+D5"，实际只有 D1+D3：D2 读的是 `pid_is_alive` 本体
（H1 没碰它），D5 在"不看死活"下照样删掉两格。expect 按观测改，不改判据。
现场证据：本档第一次跑之前仓根有 2 个死 pid 的孤儿（479 KB + 455 KB），
`pytest_sessionstart` 一落地就把它们收掉（同一次运行里 `ls test-*.db` 从 2 变 0）。
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

from tests.dbfiles import REPO_ROOT, pid_is_alive, pid_of, sweep_dead_test_dbs, sweep_own_test_dbs

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


# ---------------------------------------------------------------------------
# N-107：崩溃／被掐那一趟的遗留由"归属进程已不在"这一半收，接在会话开头
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    """真跑完并回收一个子进程，返回它的 pid——这个 pid 现在确定不在场。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _live_pid():
    """起一个还在睡的活进程，交回 (pid, 收尾函数)；测试必须在结束前叫收尾。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    return proc.pid, proc.terminate


def test_reaper_removes_dead_pid_and_keeps_everything_else(tmp_path: Path) -> None:
    """D1 只删"归属进程确定已不在"的那些；活 pid／本 pid／无 pid 后缀／别的名字都不许碰。"""
    gone = _dead_pid()
    dead = _touch(tmp_path, f"test-crashed-{gone}.db", b"owner already exited")
    live_pid, stop = _live_pid()
    try:
        live = _touch(tmp_path, f"test-concurrent-{live_pid}.db", b"another session is writing")
        mine = _touch(tmp_path, f"test-self-{os.getpid()}.db", b"this very process")
        legacy = _touch(tmp_path, "test-legacy.db", b"no pid suffix")
        foreign = _touch(tmp_path, f"smoke-{gone}.db", b"not the test-db shape")

        removed = sweep_dead_test_dbs(roots=(tmp_path,))

        assert [p.name for p in removed] == [dead.name], [p.name for p in removed]
        assert not dead.exists()
        assert live.read_bytes() == b"another session is writing", (
            "删掉并发跑的活库 ⇒ 正是 N-31 要防的那件故障"
        )
        assert mine.exists(), "本进程的库由收尾那一只收，开头这一只不许认领"
        assert legacy.exists() and foreign.exists()
        assert sweep_dead_test_dbs(roots=(tmp_path,)) == [], "第二趟必须无事可做"
    finally:
        stop()
        for path in (live, mine, legacy, foreign):
            path.unlink(missing_ok=True)


def test_pid_is_alive_is_conservative_in_the_right_direction(monkeypatch) -> None:
    """D2 三极：真死⇒False、真活⇒True、问不到（EPERM/其它 OSError）⇒一律 True。

    方向必须单边保守：判错的代价是"孤儿留着"，反过来的代价是"删掉别人的活库"。
    两极用真 pid（本进程、以及一个真跑完并被回收的子进程），两极用注入的异常——
    注入段里不许再起子进程：`monkeypatch.setattr(os, "kill", …)` 是进程范围的，
    `Popen.terminate()` 走的正是同一个 `os.kill`，会把收尾搞崩（本轮实测踩过）。
    """
    assert pid_is_alive(_dead_pid()) is False
    assert pid_is_alive(os.getpid()) is True

    def _raise(err):
        def _inner(pid, sig):
            raise err
        return _inner

    monkeypatch.setattr(os, "kill", _raise(PermissionError("not mine")))
    assert pid_is_alive(4_000_000) is True, "问不到就当活着——不许因为读不到就删"
    monkeypatch.setattr(os, "kill", _raise(OSError("unexpected")))
    assert pid_is_alive(4_000_000) is True


def session_start_offenders(source: str) -> list[str]:
    """`pytest_sessionstart` 里缺了哪个收口动作（空＝接上了）。与收尾那把尺同形。"""
    tree = ast.parse(source)
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "pytest_sessionstart"),
        None,
    )
    if fn is None:
        return ["pytest_sessionstart 不存在"]
    called = {
        call.func.id
        for call in (c for c in ast.walk(fn) if isinstance(c, ast.Call))
        if isinstance(call.func, ast.Name)
    }
    return [name for name in ("sweep_dead_test_dbs",) if name not in called]


def test_the_reaper_is_wired_into_session_start() -> None:
    """D3 接线：写得出函数不等于有人调它——会话开头必须真叫这一脚。"""
    assert session_start_offenders(SUITE_SRC.read_text(encoding="utf-8")) == []


def test_the_session_start_ruler_fires_when_the_call_is_dropped() -> None:
    """D4 尺子的反向对照与"读得到东西"两条（同收尾那把的三支形状）。"""
    unhooked = "def pytest_sessionstart(session):\n    collect_only = True\n"
    assert session_start_offenders(unhooked) == ["sweep_dead_test_dbs"]
    assert session_start_offenders("def other():\n    pass\n") == ["pytest_sessionstart 不存在"]
    hooked = unhooked + "    sweep_dead_test_dbs()\n"
    assert session_start_offenders(hooked) == []


def test_the_reaper_sweeps_both_roots_once_each(tmp_path: Path) -> None:
    """D5 与清扫同一形状：`db_url` 落 CWD、`db_path` 落 REPO_ROOT，两处都要收且不重复删。"""
    first, second = tmp_path / "one", tmp_path / "two"
    for root in (first, second):
        root.mkdir()
        _touch(root, f"test-x-{_dead_pid()}.db")

    removed = sweep_dead_test_dbs(roots=(first, second, first))

    assert len(removed) == 2, [str(p) for p in removed]
    assert not (first / removed[0].name).exists()

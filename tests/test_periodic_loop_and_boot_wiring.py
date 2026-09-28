"""周期回路本身、开机接线本身、lease 续期本身 —— 三处"机制在场但没人驱动/没人调用"的空白（N-100）。

普查（本轮，158 支恢复轴用例分档）给出的读数：R-a 99／R-b 3／R-c 50／N 6，
其中 R-b 与"无常驻断言"的四条集中在同一类形状上：**代码里有一个会做正确事情的机制，
但没有任何用例证明它真的被驱动**。这不是假想的脆弱——N-98 就是这一类的一格
（RECONCILE 有消费者无生产者），而它被发现的方式是读代码，不是测试变红。

本轮补的是同一族的另外几格：

1. `OperationWorker._run_periodic`（`app/services/worker.py:133-142`）与 `start()/stop()`
   （:115-131）：普查实测 `grep -rn "_run_periodic" tests/` 与 `grep -rn "worker.start()" tests/`
   均为 0 命中 ⇒ 把 `_run_forever` 里那句 `self._run_periodic()` 删掉，或把 `:144` 的
   `% every` 取模门删掉，全套件仍全绿——而 N-99 刚修好的那张卡又没人去回收了。
2. `app/deps.py` 的周期任务注册：四条里只有 N-99 那条被钉；`PERIODIC_QUOTA_EVERY`／
   `PERIODIC_WARM_POOL_EVERY`／`PERIODIC_HOLD_SWEEP_EVERY` 三个常量在 tests/ 里 0 命中
   ⇒ 删掉 hold 扫描那一行，"崩在 reserve 与 settle 之间就把额度永久占住"又回到无人认领。
3. 启动恢复的**接线**：`crash_recovery` 的行为有判据（`tests/test_scheduler.py:149`），
   但 `app/main.py` 真的在 lifespan 里调 `run_crash_recovery()` 这件事没人钉
   （tests/ 里 `run_crash_recovery` 的 5 处命中全是 docstring 文字，本轮逐条看过）。
4. `renew_lease`（`worker.py:300-322`）本身 0 命中：既有的 fencing 用例是靠**外部伪造**
   一条过期 lease 的行来触发 LeaseLost（`tests/test_worker_fencing.py:307` 那一支只断异常类型，
   不回头读行），所以"续期时不看 fencing token"这种改动可以一路绿。

判据分档：A 取模门与异常吸收、B 真线程驱动（`start()` 起来后探针必须被叫到，`stop()` 之后必须不再被叫到）、
C 四条注册各自在位、D 两处接线（lifespan 调用 + `crash_recovery` 的委托体）、
E `renew_lease` 的真读回（对 token 才延长、错 token 或已过期都不许动那两列）。

本轮的"牙齿"不靠合成夹具，靠**改前复算**：四臂各自只拆一条生产机制
（删 `_run_periodic()` 调用、把取模改成恒真、删 hold 那一行注册、把 `lease_expires_at > now`
去掉），每臂都必须只红它对应的那一支；跑完按字节还原。
"""

import ast
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    OperationStatus,
    OperationType,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from app.services.worker import OperationWorker
from app.utils import utcnow
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("periodic-loop"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)
REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_SRC = REPO_ROOT / "app" / "main.py"
ORCH_SRC = REPO_ROOT / "app" / "services" / "orchestrator.py"


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield


def _worker(**kwargs) -> OperationWorker:
    return OperationWorker(Factory, kwargs.pop("executor", None), **kwargs)


def _seed_op(op_id: str, *, fence: str, lease_seconds: float) -> None:
    """种一条 RUNNING＋有 lease 的 operation；`lease_seconds` 可为负（＝已经过期）。

    基础行（用户/模板/ws-1）只种一次：同一支用例会连续调它三次（两条正常行＋一条对照行），
    每次都补一份 Template 会撞在 `slug` 的唯一约束上。
    """
    with Factory() as db:
        if db.get(Template, "t1") is None:
            db.add(
                User(
                    id="u1", email="u1@x", username="u1",
                    password_hash="x",  # noqa: S106 测试桩用户，非真实密码
                    role=Role.STUDENT.value,
                )
            )
            db.add(
                Template(
                    id="t1", slug="t1", name="t1", description="d", category="c",
                    runtime="mock", launch_command="echo ok", enabled=True,
                    recommended_vram_gb=16, estimated_hourly_cost_cny=1.0,
                )
            )
            db.add(
                Workspace(
                    id="ws-1", name="ws-1", template_id="t1", provider="mock",
                    user_id="u1", status=WorkspaceStatus.RUNNING.value,
                )
            )
            db.commit()
        _seed_operation(db, op_id, fence=fence, lease_seconds=lease_seconds)
        db.commit()


def _seed_operation(db, op_id: str, *, fence: str, lease_seconds: float) -> None:
    """每个 op 配一格自己的 workspace。

    第一次写这版时两格 RUNNING 的 operation 挂在同一个 workspace 上，直接被
    `uq_ops_active_per_workspace`（`app/models.py:516-522`）挡下 —— 那是约束在生效，
    不是夹具的噪声：一行报告里"一个 workspace 至多一个活跃 operation"必须能被读出来。
    """
    now = utcnow()
    wid = f"ws-{op_id.split('-', 1)[1]}"
    if db.get(Workspace, wid) is None:
        db.add(
            Workspace(
                id=wid, name=wid, template_id="t1", provider="mock",
                user_id="u1", status=WorkspaceStatus.RUNNING.value,
            )
        )
    db.add(
        WorkspaceOperation(
            id=op_id,
            workspace_id=wid,
            operation_type=OperationType.STOP.value,
            status=OperationStatus.RUNNING.value,
            attempts=1,
            lease_owner="worker-old",
            fencing_token=fence,
            heartbeat_at=now - timedelta(seconds=30),
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
    )


def _lease_row(op_id: str) -> tuple[str, object, object]:
    with Factory() as db:
        op = db.get(WorkspaceOperation, op_id)
        return (op.fencing_token, op.heartbeat_at, op.lease_expires_at)


# ---------------------------------------------------------------------------
# A 取模门与异常吸收（不启动线程，纯确定性）
# ---------------------------------------------------------------------------


def test_the_cadence_gate_fires_exactly_on_multiples() -> None:
    """A1 every=2 与 every=3 的探针，6 tick 之后必须分别是 3 次与 2 次——一次不多一次不少。"""
    fired: dict[str, int] = {"a": 0, "b": 0}
    worker = _worker(periodic_tasks=[(2, lambda: fired.__setitem__("a", fired["a"] + 1)),
                                     (3, lambda: fired.__setitem__("b", fired["b"] + 1))])

    for _ in range(6):
        worker._run_periodic()

    assert fired == {"a": 3, "b": 2}, f"取模门算错了节拍：{fired}"


def test_a_failing_periodic_task_does_not_stop_the_others() -> None:
    """A2 一个周期任务抛错必须被吞下并继续跑其余的（`worker.py:139-142` 那个 try）。

    这条与 N-90 是同一族：一格/一个任务的故障不得带走整趟。这里没有第二个任务在场时，
    "吞掉异常"与"根本没人调用它"在读数上不可分，所以第二个探针是必要条件而不是装饰。
    """
    calls: list[str] = []

    def boom() -> None:
        calls.append("boom")
        raise RuntimeError("simulated: 周期任务失败")

    worker = _worker(periodic_tasks=[(1, boom), (1, lambda: calls.append("after"))])
    worker._run_periodic()

    assert calls == ["boom", "after"], f"失败的任务把后面的任务带走了：{calls}"


def test_the_loop_body_actually_calls_the_scheduler() -> None:
    """A3 `_run_forever` 的每一轮都必须真的调一次 `_run_periodic()`（不启动线程，替身驱动）。

    删掉那句调用是这一族最省事的回归方式：队列照排空、测试全绿，而周期任务一次都不跑。
    """
    worker = _worker()
    calls: list[int] = []
    worker._run_periodic = lambda: calls.append(1)  # type: ignore[method-assign]
    worker.tick_once = lambda: 0  # type: ignore[method-assign]

    def _wait_then_stop(seconds: float) -> None:
        calls.append(-1)
        worker._stop.set()

    worker._stop.wait = _wait_then_stop  # type: ignore[assignment]

    worker._run_forever()

    assert 1 in calls, f"循环没调用周期调度：{calls}"
    assert -1 in calls, "替身 _stop.wait 没被走到 ⇒ 这一轮跑的循环形状变了，本条判据作废"


# ---------------------------------------------------------------------------
# B 线程本身：start() 之后探针要被叫到，stop() 之后必须不再被叫到
# ---------------------------------------------------------------------------


def test_start_and_stop_actually_drive_the_periodic_tasks() -> None:
    """B1 真起线程：every=1 的探针在有限窗口内必须被叫到；stop() 之后线程消失且不再增长。

    断言只放在"叫到没有"与"停了还长不长"这两件事上，不量节奏／耗时（那是环境读数）。
    """
    count = {"n": 0}
    lock = threading.Lock()

    def probe() -> None:
        with lock:
            count["n"] += 1

    worker = _worker(periodic_tasks=[(1, probe)])
    worker.start()
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        with lock:
            if count["n"] >= 1:
                break
        time.sleep(0.1)
    with lock:
        after_start = count["n"]
    assert after_start >= 1, "start() 之后周期任务一次都没被叫到"

    worker.stop()
    assert worker._thread is None, "stop() 没有把线程句柄收掉"
    time.sleep(OperationWorker.TICK_INTERVAL + 1.5)
    with lock:
        after_stop = count["n"]
    assert after_stop == after_start, (
        f"stop() 之后还在跑：{after_start} → {after_stop}（线程没真的停）"
    )


def test_start_is_idempotent_and_a_second_start_does_not_double_the_loop() -> None:
    """B2 重复 start() 不许起第二个循环（否则每个周期任务都被跑两遍）。"""
    seen: list[str] = []
    worker = _worker(periodic_tasks=[(1, lambda: seen.append(threading.get_ident()) if len(seen) < 2 else None)])
    worker.start()
    second_start = threading.Thread(target=worker.start)
    second_start.start()
    second_start.join(timeout=5)
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and len(seen) < 1:
        time.sleep(0.1)
    worker.stop()
    assert seen, "探针没被叫到，这一条没量到东西"
    assert len(set(seen)) <= 1, f"两次 start() 起出了两个工作线程：{seen}"


# ---------------------------------------------------------------------------
# C 四条注册各自在位
# ---------------------------------------------------------------------------


def test_all_four_background_tasks_are_registered_with_their_constants() -> None:
    """C1 配额监控／warm pool maintain／hold 回收／reconcile 驱动者：四条都要在周期表上，间隔取自常量。

    间隔写成字面量的话常量就成了没人读的装饰（N-99 的 A 组同一条理由），所以这里同时钉"是谁"与"多久"。
    """
    from app import deps

    # 绑定方法每次属性访问都生成新对象 ⇒ id() 比不得，这里用 ==（Python 比 __self__ 与 __func__）
    entries = [(n, c) for n, c in deps.worker.periodic_tasks]
    expected = [
        ("配额监控", deps.orchestrator.monitor_runtime_quotas, OperationWorker.PERIODIC_QUOTA_EVERY),
        ("warm pool maintain", deps._warm_pool_maintain, OperationWorker.PERIODIC_WARM_POOL_EVERY),
        ("pending hold 回收", deps.orchestrator.release_expired_holds, OperationWorker.PERIODIC_HOLD_SWEEP_EVERY),
        ("reconcile 驱动者", deps._reconcile_stuck_cells, OperationWorker.PERIODIC_RECONCILE_EVERY),
    ]
    for label, callable_, cadence in expected:
        matched = [(n, c) for n, c in entries if c == callable_]
        assert len(matched) == 1, (
            f"{label} 在周期表上出现 {len(matched)} 次；表内容："
            f"{[(n, getattr(c, '__name__', repr(c))) for n, c in entries]}"
        )
        assert matched[0][0] == cadence, (
            f"{label} 的间隔不是常量 {cadence}，实际 {matched[0][0]}"
        )


# ---------------------------------------------------------------------------
# D 开机那一脚
# ---------------------------------------------------------------------------


def crash_recovery_delegation_offenders(source: str) -> list[str]:
    """`crash_recovery` 的函数体是否只做一件事：把判决交给 `self.reconcile_all()`（无关键字参数）。"""
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef) or node.name != "crash_recovery":
            continue
        calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
        if not any(
            isinstance(c.func, ast.Attribute)
            and c.func.attr == "reconcile_all"
            and not c.keywords
            for c in calls
        ):
            offenders.append("X1:没把判决交给无参 reconcile_all()")
        if any(isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "_reconcile_stuck_cells" for c in calls):
            offenders.append("X2:启动恢复改走了有界档")
    if not offenders and "def crash_recovery" not in source:
        offenders.append("X3:crash_recovery 不在了")
    return offenders


def test_the_lifespan_calls_crash_recovery() -> None:
    """D1 起一次 app（真 lifespan），启动恢复必须被调用一次——这一脚此前无人钉。"""
    from fastapi.testclient import TestClient

    import app.main as main_module

    calls: list[int] = []
    original = main_module.run_crash_recovery
    main_module.run_crash_recovery = lambda: calls.append(1)  # type: ignore[assignment]
    try:
        with TestClient(main_module.app):
            pass
    finally:
        main_module.run_crash_recovery = original  # type: ignore[assignment]

    assert calls == [1], f"lifespan 没调用 run_crash_recovery（调用次数 {calls}）"


def test_the_delegation_ruler_can_see_the_wiring_being_swapped() -> None:
    """D2 判据自测：把委托体换成有界档／换成别的调用，尺子必须点名；现状必须干净。"""
    src = ORCH_SRC.read_text(encoding="utf-8")
    assert crash_recovery_delegation_offenders(src) == [], crash_recovery_delegation_offenders(src)

    marker = "        return self.reconcile_all()\n"
    assert src.count(marker) == 1, f"锚点命中 {src.count(marker)} 次，不是 1"
    assert crash_recovery_delegation_offenders(
        src.replace(marker, "        return self.reconcile_all(limit=8, older_than_seconds=60)\n")
    ) == ["X1:没把判决交给无参 reconcile_all()"]
    assert crash_recovery_delegation_offenders(
        src.replace(marker, "        return self._reconcile_stuck_cells()\n")
    ) == ["X1:没把判决交给无参 reconcile_all()", "X2:启动恢复改走了有界档"]
    assert crash_recovery_delegation_offenders("def other():\n    return 1\n") == ["X3:crash_recovery 不在了"]


def test_main_actually_wires_the_recovery_call_in_text() -> None:
    """D3 文本面兜底：`app/main.py` 里那一句调用必须还在（D1 量行为，这条量被删掉的句子）。"""
    src = MAIN_SRC.read_text(encoding="utf-8")
    assert "run_crash_recovery()" in src, "main.py 里已经没有那句启动恢复了"
    assert src.count("run_crash_recovery()") == 1, "调用点不止一处，读者说不清是哪一次在驱动"


# ---------------------------------------------------------------------------
# E renew_lease：读回权威行，而不是只断一个异常
# ---------------------------------------------------------------------------


def _as_naive(value: object) -> object:
    """SQLite 把 DateTime(timezone=True) 存成无偏移串，写侧喂的是 aware ⇒ 比较前统一剥掉 tz。"""
    return value.replace(tzinfo=None) if hasattr(value, "replace") and getattr(value, "tzinfo", None) else value


def test_renew_lease_extends_only_for_the_matching_token() -> None:
    """E1 对 token 才延长；错 token 返回 False **且 heartbeat/lease 两列一动不动**。

    既有那支 fencing 用例是外部伪造一条过期行来触发 LeaseLost，从不回头读行
    （普查点名的 R-b 形状）；这里改判"续期这个动作本身"，所以必须读回权威列——
    否则一个"返回值 True 但 SQL 没带 token 条件"的改动可以一路绿。
    """
    _seed_op("op-good", fence="t-1", lease_seconds=60)
    _seed_op("op-stale", fence="t-2", lease_seconds=60)
    worker = _worker()
    good_before = _lease_row("op-good")
    stale_before = _lease_row("op-stale")

    with Factory() as db:
        assert worker.renew_lease(db, "op-good", "t-1") is True
    with Factory() as db:
        assert worker.renew_lease(db, "op-stale", "wrong-token") is False

    good_after = _lease_row("op-good")
    stale_after = _lease_row("op-stale")
    assert _as_naive(good_after[1]) > _as_naive(good_before[1]), (
        f"续期说成功了，heartbeat 却没动：{good_before} → {good_after}"
    )
    assert _as_naive(good_after[2]) > _as_naive(good_before[2]), (
        f"续期说成功了，lease 却没延长：{good_before} → {good_after}"
    )
    assert stale_after == stale_before, (
        f"错 token 的续期仍然改写了那一行（fencing 没生效）：{stale_before} → {stale_after}"
    )


def test_renew_lease_refuses_an_expired_lease() -> None:
    """E2 已过期的 lease 不许被续回来——那是"被 reclaim 之后旧 owner 复活"的那条路。

    极性配对：E1 是 token 不对、这一支是时间不对，两个条件各守半边；把
    `lease_expires_at > now` 那一半删掉只有这一支会红。
    """
    _seed_op("op-expired", fence="t-3", lease_seconds=-5)
    before = _lease_row("op-expired")
    worker = _worker()

    with Factory() as db:
        assert worker.renew_lease(db, "op-expired", "t-3") is False

    assert _lease_row("op-expired") == before, "过期 lease 被续回了（时间谓词没守）"


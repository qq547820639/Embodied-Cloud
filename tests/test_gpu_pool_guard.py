"""GPU 池回收（`tests/gpu_pool.py`）的判据——全套共卡夹具的内核。

常驻动机：全套共用一个 SQLite、mock 只 seed 8 张卡，而 `POST /api/workspaces`
占卡后没人还——于是"谁的 workspace 多"决定谁红。实测过两种形状：
`test_gpu_admin` 与一个建了 13 个 workspace 的文件配对即红（其余 55 个文件逐个配
都不红）；`test_workspace_credential` 在全量跑里以 `assert 'failed' == 'running'` 红过。
`tests/conftest.py` 的 autouse 守卫因此回收残留占用——现在按净额回收（原始空闲减去
队列欠款），"整池为空"只是它的一个子集。

这里量的是回收内核（不点它就只能停在 0 张空闲），并且**只用自己的 workspace 行**
——不拿别人的占用当夹具，免得本文件的读数受上游用例残留状态摆布。

还量 `ensure_free_gpus` 的**净额**口径（三支：必须开火／不该动手／报错带两个数）：
"AVAILABLE 有几张"没算上队列里那些"已入队、还没被执行"的 provision op，而每一个
这样的 op 都会在下一个 tick 吃掉一张卡。背景读数写在 `tests/gpu_pool.py` 的 docstring。

同文件还量 `tests/settle.py` 的收敛判据（正反两支）。本文件自己也欠一次收尾更正：
`rig` 过去只还卡、不删行，而它留下的 CREATED 行会被下一个模块启动时的 `reconcile_all()`
重新入队 PROVISION（＝让别人的 worker 替本文件抢卡），现在 teardown 连行一起删。

不声称"某一轮配对变绿是守卫单独给的"：真起 TestClient 时 app 的
`run_crash_recovery()` 也会把查无 runtime 的分配放掉；同一读数有两个可能成因，
就不能拿它当单因证据（如实写明）。
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.deps import SessionFactory, scheduler
from app.main import app
from app.models import (
    Gpu,
    GpuStatus,
    OperationType,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from app.services.worker import enqueue_operation
from tests.gpu_pool import allocated_workspaces, count_big_enough, ensure_free_gpus, reclaim_gpus
from tests.settle import await_workspace_settled

HOSTS = 8  # 抽干池需要的挂点数；mock seed 的卡数小于它时按实际卡数收


@pytest.fixture
def rig():
    """真 app lifespan（建表 + mock 主机 sync）+ 本文件专属的 user/workspace 行 + 达成前置。"""
    with TestClient(app):
        pass
    tag = uuid.uuid4().hex[:8]
    with SessionFactory() as db:
        cards = list(db.scalars(select(Gpu)))
        assert cards, "mock 主机没 seed 出卡：本文件的判据无从谈起"
        # N-33：前置在这里达成，不再留"看跑序"的跳过分支。
        # 本文件两支各需要 ≥2 张**净**余量（一支要抽干后再回收；另一支要把一张设成
        # DRAINING 后还得剩一张，守卫才会走"不该回收"那条判断）。全套共用一个库，
        # 上游残骸可能占着 → 先回收；回收后仍不够就是**有卡停在放不掉的状态**，那是缺陷。
        # 净额口径（N-85）：队列里那些"还没被执行掉的 provision op"每一个都会吃掉一张卡，
        # 只看 AVAILABLE 数会把"承诺 2 张、实际到手 1 张"当成达成。
        if count_big_enough(db, 8) - _queue_debt(db) < 2:
            reclaim_gpus(db, scheduler)
        free = count_big_enough(db, 8)
        debt = _queue_debt(db)
        assert free - debt >= 2, (
            f"回收之后净余量仍不足 2 张（原始空闲 {free} 张 − 未执行 provision op {debt} 个）："
            "说明有卡停在无法释放的状态，这是被测系统的缺陷，不该被条件跳过掩盖"
        )
        if db.scalar(select(Template).where(Template.id == "cartpole")) is None:
            db.add(
                Template(
                    id="cartpole", slug="cartpole", name="t", description="d", category="c",
                    runtime="mock", launch_command="", enabled=True, version="0.1.0",
                    recommended_vram_gb=8, estimated_hourly_cost_cny=1.0,
                )
            )
        user = User(
            id=f"u-guard-{tag}", email=f"guard-{tag}@example.org", username=f"guard{tag}",
            password_hash="x", role=Role.USER.value,  # noqa: S106 测试数据
        )
        db.add(user)
        db.flush()
        hooks = []
        for i in range(min(HOSTS, len(cards))):
            ws = Workspace(
                id=f"ws-guard-{tag}-{i}", name=f"guard {i}", template_id="cartpole",
                provider="mock", user_id=user.id, status=WorkspaceStatus.CREATED.value,
            )
            db.add(ws)
            hooks.append(ws.id)
        db.commit()
        state = {"cards": cards, "hooks": hooks}
        yield state
    # 收尾要还的不止"卡"。上面那 8 行 CREATED workspace 是一把延时引信：
    # app 每次启动都跑 `run_crash_recovery()` → `reconcile_all()`，其中
    # "QUEUED/CREATED 且无 active operation ⇒ 重新入队 PROVISION"
    # （app/services/orchestrator.py 的 CREATED/QUEUED 分支）。于是下一个模块起
    # TestClient 时，它自己的 worker 会先替本文件的残骸去抢卡。实测：本文件 +
    # test_api 配对，test_api 的 provision 被报成 `No GPU available with >= 8 GB VRAM`
    # （日志 4 条 `provision(ws-guard) failed, retrying`），终态判据当场翻红。
    with SessionFactory() as db:
        for hook in state["hooks"]:
            scheduler.release(db, hook)  # 自己借的卡自己还（顺带清 Gpu.workspace_id）
        db.execute(delete(WorkspaceOperation).where(WorkspaceOperation.workspace_id.in_(state["hooks"])))
        db.execute(delete(Workspace).where(Workspace.id.in_(state["hooks"])))
        db.commit()
        if count_big_enough(db, 8) == 0:
            reclaim_gpus(db, scheduler)


def _starve(db, rig) -> list[str]:
    """把 AVAILABLE 的卡逐张挂到本文件自己的 workspace 行上，返回挂上了卡的 hook。

    挂得动几张算几张：全套跑起来时上游用例可能已经占着几张，本文件的判据是
    "**空闲的**被我抽干"，不是"我抽干了 8 张"。
    """
    bound: list[str] = []
    for hook in rig["hooks"]:
        free = db.scalar(
            select(Gpu).where(Gpu.status == GpuStatus.AVAILABLE.value).limit(1)
        )
        if free is None:
            break
        free.status = GpuStatus.ALLOCATED.value
        free.workspace_id = hook
        bound.append(hook)
    db.commit()
    return bound


def test_starving_the_pool_really_leaves_zero_free(rig):
    """前提档：抽干之后必须真是 0 张空闲，否则"回收让它变绿"就是蒙的。"""
    with SessionFactory() as db:
        bound = _starve(db, rig)
        assert bound, "一张 AVAILABLE 卡都抽不出来：前提不成立"
        assert count_big_enough(db, 8) == 0, "抽过之后还有空闲卡：前提没成立"
        assert set(bound) <= {w.id for w in allocated_workspaces(db)}


def test_reclaim_frees_the_cards_and_clears_the_binding(rig):
    """正例：不点 `reclaim_gpus` 就永远回不到"够用"；点完还得连绑定一起清。"""
    with SessionFactory() as db:
        assert _starve(db, rig), (
            "rig 已经保证 ≥2 张空闲还抽不出一张：前置达成逻辑失效或被谁在中间占了卡"
        )
        assert count_big_enough(db, 8) == 0
        assert ensure_free_gpus(db, scheduler, need=1) >= 1
        still_bound = list(
            db.scalars(
                select(Gpu).where(
                    Gpu.status == GpuStatus.ALLOCATED.value, Gpu.workspace_id.is_not(None)
                )
            )
        )
        # 绑定不清，下一次分配会撞 uq_gpus_workspace——"释放"必须是完整的
        assert not still_bound, [gpu.id for gpu in still_bound]


def test_ensure_free_gpus_leaves_drained_cards_alone(rig):
    """不该动的时候不动：UNHEALTHY/DRAINED 是管理员判决，守卫与自动路径都不许碰。"""
    with SessionFactory() as db:
        free_before = count_big_enough(db, 8)
        assert free_before >= 2, (
            f"rig 承诺的 ≥2 张余量没达成（只剩 {free_before}）：要么前置被中途改动，要么有卡放不掉"
        )
        # N-85：这条判据核的是"守卫不该触发回收"，它成立的前提是**净额**还够 1 张。
        # 不加这道前提，队列欠账 ≥ 空闲数时守卫会去回收，返回值就不再等于 `free_before - 1`，
        # 而报错会指向"DRAINED 判据坏了"——那是夹具没造出这一档，不是被测系统的缺陷。
        debt = _queue_debt(db)
        assert free_before - 1 - debt >= 1, (
            f"本档要求「设成 DRAINED 之后净余量仍 ≥1」：原始空闲 {free_before} 张 − 1（DRAINED）"
            f" − 未执行 provision op {debt} 个 已经不够；换成有净余量的时刻再跑"
        )
        victim = db.scalar(select(Gpu).where(Gpu.status == GpuStatus.AVAILABLE.value))
        victim.status = GpuStatus.DRAINED.value
        db.commit()

        # 还有空闲卡 → 守卫不该触发回收，返回值必须就是"减去那张 DRAINED 后的现状"
        assert ensure_free_gpus(db, scheduler, need=1) == free_before - 1
        still = list(db.scalars(select(Gpu).where(Gpu.status == GpuStatus.DRAINED.value)))
        assert still, "DRAINED 的卡被放回 AVAILABLE：会抹掉管理员的判决"


# ---------------------------------------------------------------------------
# 净额前置：空闲卡数要**扣掉队列里还没被执行掉的 provision op** 才算数
# ---------------------------------------------------------------------------


def _queue_debt(db) -> int:
    """独立重数一遍"未 fulfilled 的 provision op"——**故意不 import** `tests.gpu_pool` 里那个。

    这三支判据要在"修复前"那一档也能编译、并且红在该红的地方：引用一个当时还不存在
    的名字只会得到 ImportError，那不是开火。所以这里按 `app/models.py:97-102` 的
    枚举自己数一遍（独立 oracle）。
    """
    return int(
        db.scalar(
            select(func.count())
            .select_from(WorkspaceOperation)
            .where(
                WorkspaceOperation.operation_type == OperationType.PROVISION.value,
                WorkspaceOperation.status.in_(("pending", "running", "retrying")),
            )
        )
        or 0
    )


@pytest.fixture
def pool_that_owes_the_queue():
    """把共享池做成"原始空闲 = 1 + 队列既有欠款"这个**合法但可能致命**的状态。

    这是一次受控抽干，复现的是全套跑动到达的池现场，不是声称重放了全套的跑序。
    实测的致命现场：`test_workspace_progress` 的正例调 `ensure_free_gpus(need=1)`
    时原始空闲正好 1 张，而同模块前一支留下的 queued provision op 会在
    `worker.tick_once()` 里先把那唯一一张吃掉（模块结束时两支的 ws 都是 running、
    `gpu 总=8 可用=6 分配行=2`），于是正例重试 3 次后 terminal failed。

    抽干走 `GpuScheduler.allocate`、回收走 `GpuScheduler.release`（本仓唯一的分配权威），
    不手写 UPDATE。挂卡的 workspace 行停在 CREATED —— `reclaim_gpus` 只走
    `LIVE_STATUSES`；另留一行**不挂卡**的 CREATED workspace 当作队列的债务人
    （queued 而未执行的真形状就是这样：有 op、还没有卡）。
    """
    with TestClient(app):
        pass  # lifespan：建表 + seed mock 主机与模板
    tag = uuid.uuid4().hex[:8]
    with SessionFactory() as db:
        reclaim_gpus(db, scheduler)  # 起步：把上游残留还掉，池回到已知位置（与 rig 同一手法）
        free0 = count_big_enough(db, 8)
        debt0 = _queue_debt(db)
        assert free0 >= 2, f"回收后只剩 {free0} 张 ≥8GiB 空闲卡：抽不到只剩 1 张的状态，前提无从谈起"
        if db.scalar(select(Template).where(Template.id == "cartpole")) is None:
            db.add(
                Template(
                    id="cartpole", slug="cartpole", name="t", description="d", category="c",
                    runtime="mock", launch_command="", enabled=True, version="0.1.0",
                    recommended_vram_gb=8, estimated_hourly_cost_cny=1.0,
                )
            )
        user = User(
            id=f"u-net-{tag}", email=f"net-{tag}@example.org", username=f"net{tag}",
            password_hash="x", role=Role.USER.value,  # noqa: S106 测试数据
        )
        db.add(user)
        db.flush()
        target = 1 + debt0
        mine: list[str] = []
        queue_ws = f"ws-net-{tag}-queue"
        db.add(
            Workspace(
                id=queue_ws, name="net queue", template_id="cartpole", provider="mock",
                user_id=user.id, status=WorkspaceStatus.CREATED.value,
            )
        )
        db.commit()
        # 逐张挂到本夹具自己的行上，直到原始空闲数等于"1 + 既有欠款"
        while count_big_enough(db, 8) > target:
            ws = Workspace(
                id=f"ws-net-{tag}-c{len(mine)}", name="net card", template_id="cartpole",
                provider="mock", user_id=user.id, status=WorkspaceStatus.CREATED.value,
            )
            db.add(ws)
            db.commit()
            scheduler.allocate(db, ws.id, 8)
            mine.append(ws.id)
        assert count_big_enough(db, 8) == target, (
            f"挂满 {len(mine)} 张后原始空闲仍是 {count_big_enough(db, 8)}，目标 {target}："
            "有卡停在 allocate/release 之外进不去的状态"
        )
        state = {
            "card_hooks": mine,
            "queue_ws": queue_ws,
            "debt_before": debt0,
            "free": target,
            "free_at_setup": free0,
        }
        yield state
    with SessionFactory() as db:
        ids = [*state["card_hooks"], state["queue_ws"]]
        db.execute(delete(WorkspaceOperation).where(WorkspaceOperation.workspace_id.in_(ids)))
        db.commit()
        for workspace_id in ids:
            scheduler.release(db, workspace_id)
        db.execute(delete(Workspace).where(Workspace.id.in_(ids)))
        db.execute(delete(User).where(User.id == f"u-net-{tag}"))
        db.commit()


def test_ensure_free_gpus_fires_when_the_queue_already_owes_the_last_free_card(
    pool_that_owes_the_queue,
):
    """必须开火：原始空闲 1 张 + 队列里 1 个未执行的 provision op ⇒ 净额不足，不许宣布满足。

    夹具把原始空闲做成 `1 + 既有欠款`，所以"正好 1 张空闲 + 正好 1 个欠款"这一档在
    既有欠款为 0 时逐字成立；既有欠款 >0 时状态同样净额不足，读数会一起打出来。
    """
    setup = pool_that_owes_the_queue
    with SessionFactory() as db:
        assert enqueue_operation(db, setup["queue_ws"], OperationType.PROVISION) is not None
        before = count_big_enough(db, 8)
        debt = _queue_debt(db)
        assert (before, debt) == (setup["free"], setup["debt_before"] + 1), (
            f"夹具前提没立住：原始空闲 {before}（目标 {setup['free']}）、"
            f"欠款 {debt}（既有 {setup['debt_before']} + 本用例 1）"
        )
        assert before - debt < 1, "净额其实已经够：这一档退化成'不该开火'，判据白写"
        # 合法出路只有两条：真的动手（原始空闲变大）或按"前置不成立"抛错。
        # "原样返回一个满足的读数"正是这一轮要拆掉的假承诺。
        try:
            free_after = ensure_free_gpus(db, scheduler, need=1)
        except AssertionError:
            free_after = None
        assert free_after is None or free_after > before, (
            f"原始空闲 {before} 张 − 未执行 provision op {debt} 个 = 净额 "
            f"{before - debt} < need=1，ensure_free_gpus 却原样返回 {free_after}："
            "它把自己宣布成'满足了'，下一支用例的 tick 会先替那条 queued op 吃掉最后一张卡"
        )
        if free_after is not None:
            assert free_after - _queue_debt(db) >= 1, (
                f"动手之后净额仍是 {free_after - _queue_debt(db)}：出参没兑现净额承诺"
            )


def test_ensure_free_gpus_stays_its_hand_when_the_net_is_already_enough(pool_that_owes_the_queue):
    """反向对照：不入队时净额正好是 1，属合法状态——守卫一张卡都不许多还。

    没有这一支，"净额规则"可以退化成"只要空闲卡少就抽干池子"而照样让上一支绿。
    """
    with SessionFactory() as db:
        before = count_big_enough(db, 8)
        debt = _queue_debt(db)
        assert before - debt == 1, f"夹具没做成'净额恰好为 1'的合法状态（{before}−{debt}）"
        assert ensure_free_gpus(db, scheduler, need=1) == before, (
            "净额够用却动了手：会抹掉别的用例正在借的卡"
        )
        still_bound = {w.id for w in allocated_workspaces(db)}
        assert set(pool_that_owes_the_queue["card_hooks"]) <= still_bound, (
            "夹具挂上去的卡被提前还掉了：上面那个等值断言就成了蒙的"
        )


def test_ensure_free_gpus_failure_message_reports_both_numbers(pool_that_owes_the_queue):
    """报错必须同时报出"原始空闲几张"和"队列欠款几个"，否则下一个读者去找不存在的贼。

    用 `need=10_000` 把"回收也救不回来"这一档做成确定的，只验文案形状。
    """
    setup = pool_that_owes_the_queue
    with SessionFactory() as db:
        assert enqueue_operation(db, setup["queue_ws"], OperationType.PROVISION) is not None
        with pytest.raises(AssertionError) as got:
            ensure_free_gpus(db, scheduler, need=10_000)
        free_now = count_big_enough(db, 8)
        debt_now = _queue_debt(db)
        text = str(got.value)
        assert f"原始空闲 {free_now} 张" in text, text
        assert f"未执行 provision op {debt_now} 个" in text, (
            f"报错里找不到欠款数（现场欠款 {debt_now} 个）：{text}"
        )


class _StubResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _StubClient:
    """只满足 await_workspace_settled 的读取形状：按脚本依次返回状态，并记次数。"""

    def __init__(self, script: list[dict]):
        self.script = script
        self.calls = 0

    def get(self, url: str, headers: dict | None = None) -> _StubResponse:
        payload = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return _StubResponse(payload)


def test_await_settled_fires_when_the_workspace_never_settles(rig):
    """反向对照：一直 queued 就必须按"前提未达成"翻红，且把最后读数和池子现场一起报出来。

    没有这一支，"等不到就说没说清"的那类红又会退回成 `assert 'queued' == 'running'`。
    """
    client = _StubClient([{"status": "queued", "error_message": "No GPU available with >= 8 GB VRAM"}])
    with pytest.raises(AssertionError) as got:
        await_workspace_settled(client, "ws-" + "0" * 30, {}, timeout_seconds=0.2, interval=0.01)
    text = str(got.value)
    assert "前提未达成" in text, text
    assert "'queued'" in text, text  # 最后一次读数必须在串里
    assert "No GPU available" in text, text  # 以及它为什么还没收敛
    assert "空闲卡" in text, text  # 域内现场：池子读数真的接上了（不是恒真的占位串）
    assert client.calls >= 2, client.calls  # 它确实轮询过，不是读一次就下结论


def test_await_settled_returns_on_either_terminal_status():
    """正向对照：running／failed 都是终态，都不该抛；provisioning 则要继续等。

    这一支同时证明判据不是恒真（queued/provisioning 那一路会走满超时）也不是恒假。
    """
    for terminal in ("running", "failed"):
        client = _StubClient([{"status": "provisioning"}, {"status": terminal}])
        state = await_workspace_settled(client, "ws-x", {}, timeout_seconds=1.0, interval=0.01)
        assert state["status"] == terminal
        assert client.calls == 2, (terminal, client.calls)  # 第一次没收敛⇒它真的在等

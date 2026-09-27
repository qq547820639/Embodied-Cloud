"""GPU 池回收（`tests/gpu_pool.py`）的判据——全套共卡夹具的内核。

常驻动机：全套共用一个 SQLite、mock 只 seed 8 张卡，而 `POST /api/workspaces`
占卡后没人还——于是"谁的 workspace 多"决定谁红。实测过两种形状：
`test_gpu_admin` 与一个建了 13 个 workspace 的文件配对即红（其余 55 个文件逐个配
都不红）；`test_workspace_credential` 在全量跑里以 `assert 'failed' == 'running'` 红过。
`tests/conftest.py` 的 autouse 守卫因此"在整池为空时"回收残留占用。

这里量的是回收内核（不点它就只能停在 0 张空闲），并且**只用自己的 workspace 行**
——不拿别人的占用当夹具，免得本文件的读数受上游用例残留状态摆布。

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
from sqlalchemy import delete, select

from app.deps import SessionFactory, scheduler
from app.main import app
from app.models import (
    Gpu,
    GpuStatus,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
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
        # 本文件两支各需要 ≥2 张 ≥8 GiB 的空闲卡（一支要抽干后再回收；另一支要把一张设成
        # DRAINING 后还得剩一张，守卫才会走"不该回收"那条判断）。全套共用一个库，
        # 上游残骸可能占着 → 先回收；回收后仍不够就是**有卡停在放不掉的状态**，那是缺陷。
        if count_big_enough(db, 8) < 2:
            reclaim_gpus(db, scheduler)
        free = count_big_enough(db, 8)
        assert free >= 2, (
            f"回收之后仍只有 {free} 张 ≥8 GiB 的空闲卡：说明有卡停在无法释放的状态，"
            "这是被测系统的缺陷，不该被条件跳过掩盖"
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


def test_ensure_free_gpus_leaves_draining_cards_alone(rig):
    """不该动的时候不动：UNHEALTHY/DRAINING 可能是别的用例故意设出来的前提。"""
    with SessionFactory() as db:
        free_before = count_big_enough(db, 8)
        assert free_before >= 2, (
            f"rig 承诺的 ≥2 张余量没达成（只剩 {free_before}）：要么前置被中途改动，要么有卡放不掉"
        )
        victim = db.scalar(select(Gpu).where(Gpu.status == GpuStatus.AVAILABLE.value))
        victim.status = GpuStatus.DRAINING.value
        db.commit()

        # 还有空闲卡 → 守卫不该触发回收，返回值必须就是"减去那张 DRAINING 后的现状"
        assert ensure_free_gpus(db, scheduler, need=1) == free_before - 1
        still = list(db.scalars(select(Gpu).where(Gpu.status == GpuStatus.DRAINING.value)))
        assert still, "DRAINING 的卡被放回 AVAILABLE：会抹掉别的用例故意设出来的状态"


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

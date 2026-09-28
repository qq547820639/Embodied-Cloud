"""释放 GPU 的准入判据：provider 认账 runtime 已不在，才算停过（N-63）。

缺陷形状（改前）：`stop()` 把 FAILED 当"幂等收尾"入口，直接进 `_finalize_stop`
—— 结算 + `scheduler.release` + 置 STOPPED，但**从不叫 `provider.stop`**。于是
`provider.stop` 失败留下的那个还在吃 `--gpus device=N` 的容器，在重试后被判已停、
GPU 被放回池子供下一次分配（一卡双跑）；而 `reconcile_all` 又按 status 跳过
STOPPED/FAILED，这个孤儿永久不可见。同一条不变量在 `destroy()` 里早就写了
（"provider 清理失败 → 不得释放 GPU / 置 DELETED"），`reconcile_all` 的
STOPPING+ALIVE 分支是第二处（`contextlib.suppress` 吞掉停止失败后照样释放）。

判据消费的是 **provider 自己声明的 runtime 事实**，不是调用返回码，也不是计数器：
mock 只有 UNKNOWN（providers/mock.py:72-74），证不了"还活着"这件事，所以这里换一把
带状态的替身。两极缺一不可：命令成功但 runtime 仍在 ⇒ 不释放；命令失败但 provider
亲口说没了 ⇒ 必须释放（否则 K8s 的 404 会把 GPU 永久钉死）。

第二组判据钉的是"没达成目标时对外说了什么"：状态留在 STOPPING（不是 FAILED——
`recover_stuck_gpu_allocations` 只保护非终态，写成 FAILED 就会被它把卡放掉，
本函数的准入判据等于被绕过），且 durable STOP operation 不许记 SUCCEEDED。
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    CreditLedger,
    Gpu,
    GpuAllocation,
    GpuStatus,
    LedgerType,
    OperationStatus,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceStatus,
)
from app.services.billing import BillingPolicy
from app.services.ledger import CreditLedgerService
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.base import RuntimeState
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.services.streaming import StreamingSessionService
from app.services.worker import OperationWorker
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("stop-admission"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


# 替身的三种"停止命令的回话"，对应本轮判据要分开的几档
BEHAVIORS = ("stops", "lie", "error_then_gone")


class StatefulProvider(MockProvider):
    """provider 侧的 runtime 事实由它自己声明，不由 orchestrator 猜。

    - `stops`：命令成功且 runtime 真的没了（正常档）
    - `lie`：命令返回成功，但 runtime 照旧活着（改前会把 GPU 放掉）
    - `error_then_gone`：命令抛错，但 runtime 其实已经没了（K8s 对已删 Deployment 的 404）
    - `fail_times`：前 N 次 `stop()` 抛错且事实不变（daemon 抖动，重试才有意义）
    """

    def __init__(self, behavior: str = "stops", fail_times: int = 0):
        super().__init__("http://127.0.0.1:8000")
        assert behavior in BEHAVIORS, behavior
        self.behavior = behavior
        self.fail_times = fail_times
        self.stop_calls = 0
        self.alive = True

    def stop(self, workspace):
        self.stop_calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            if self.behavior == "error_then_gone":
                self.alive = False
            raise RuntimeError("simulated: runtime stop failure")
        if self.behavior == "error_then_gone":
            self.alive = False
            raise RuntimeError("simulated: deployment not found (404)")
        if self.behavior == "lie":
            return None
        self.alive = False
        return None

    def reconcile(self, workspace: Workspace) -> RuntimeState:
        return RuntimeState.ALIVE if self.alive else RuntimeState.MISSING


class FailingReleaseScheduler:
    """`scheduler.release` 抛错：结算已经落库、状态还没置成 STOPPED 的那一档。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def release(self, db, workspace_id):
        self.calls += 1
        raise RuntimeError("simulated: GPU release failure")

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _seed(db) -> None:
    GpuScheduler(Factory).sync_host(
        db,
        host_id="host-1",
        name="h1",
        address="127.0.0.1",
        provider="mock",
        gpus=[GpuInfo(gpu_uuid="gpu-1", model="RTX", memory_total=24564, index=0)],
    )
    db.add(
        Template(
            id="cartpole",
            slug="cartpole",
            name="cartpole",
            description="test",
            category="test",
            runtime="mock",
            launch_command="echo ok",
            enabled=True,
            requires_streaming=False,
            recommended_vram_gb=16,
            estimated_hourly_cost_cny=1.0,
        )
    )
    db.commit()


def _running_workspace(provider: StatefulProvider):
    """真走 provision：GPU 由 scheduler 原子分配，容器名落库（stop 才有对象可叫）。"""
    orchestrator = WorkspaceOrchestrator(
        Factory,
        provider,
        Path("/tmp/test-stop-admission"),  # noqa: S108 测试隔离目录
        streaming=StreamingSessionService(Factory),
    )
    with Factory() as db:
        _seed(db)
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = ws.id
    orchestrator._start(wid)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value
        # 运行段真实存在：结算与配额才有可核对的前提
        ws.started_at = datetime.now(UTC) - timedelta(seconds=30)
        db.commit()
    return orchestrator, wid


def _side_effects(db, workspace_id: str) -> dict:
    """从权威表读"释放到底发生了没有"：分配行、卡状态、本运行段是否已入账。"""
    gpu = db.scalar(select(Gpu))
    return {
        "allocated": db.scalar(
            select(GpuAllocation).where(GpuAllocation.workspace_id == workspace_id)
        )
        is not None,
        "gpu_free": gpu.status == GpuStatus.AVAILABLE.value,
        "usage_entries": int(
            db.scalar(
                select(func.count(CreditLedger.id)).where(
                    CreditLedger.workspace_id == workspace_id,
                    CreditLedger.type == str(LedgerType.USAGE),
                )
            )
            or 0
        ),
    }


NOT_RELEASED = {"allocated": True, "gpu_free": False, "usage_entries": 0}
RELEASED = {"allocated": False, "gpu_free": True, "usage_entries": 1}
# `_finalize_stop` 的次序是"先结算、后 release"，所以 release 失败这一档钱已经入账：
# 运行段真的跑过，扣款是对的；没做的是把卡还回去，而那一步可重试。
FINALIZE_FAILED = {"allocated": True, "gpu_free": False, "usage_entries": 1}


# ---------------------------------------------------------------------------
# 本轮修掉的缺陷：重试必须真的再叫一次 provider.stop
# ---------------------------------------------------------------------------


def test_retry_re_asks_the_provider_before_touching_the_gpu():
    """停止失败 → 留在 STOPPING 且不释放；重试叫到 provider 认账为止才 STOPPED＋释放。"""
    provider = StatefulProvider(fail_times=1)
    orchestrator, wid = _running_workspace(provider)

    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))

    with Factory() as db:
        ws = db.get(Workspace, wid)
        # 没达成目标就不写终态：FAILED 会被 recover_stuck_gpu_allocations 当孤儿放卡
        assert ws.status == WorkspaceStatus.STOPPING.value
        assert ws.error_message
        assert _side_effects(db, wid) == NOT_RELEASED

    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))

    with Factory() as db:
        assert db.get(Workspace, wid).status == WorkspaceStatus.STOPPED.value
        assert _side_effects(db, wid) == RELEASED
    # 承重两读：改前重试根本不叫 provider（stop_calls 停在 1），
    # 于是容器在 provider 自己嘴里还活着 —— 那正是"一卡双跑"的那一半。
    assert provider.stop_calls == 2
    assert provider.alive is False


def test_stop_command_succeeding_is_not_evidence_the_runtime_is_gone():
    """命令返回成功但 provider 仍自述 ALIVE ⇒ 不结算、不释放、不判 STOPPED。"""
    provider = StatefulProvider(behavior="lie")
    orchestrator, wid = _running_workspace(provider)

    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPING.value
        # 判决的原因来自 provider 的读数，不是那句假的"成功"
        assert "provider reports runtime alive" in (ws.error_message or "")
        assert _side_effects(db, wid) == NOT_RELEASED
    assert provider.alive is True


def test_positive_absence_releases_even_when_the_command_errored():
    """另一极：命令抛错但 provider 亲口说 runtime 没了 ⇒ 必须释放，否则卡被永久钉死。"""
    provider = StatefulProvider(behavior="error_then_gone")
    orchestrator, wid = _running_workspace(provider)

    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))

    with Factory() as db:
        assert db.get(Workspace, wid).status == WorkspaceStatus.STOPPED.value
        assert _side_effects(db, wid) == RELEASED
    # 一次就够：不需要重试，因为事实已经成立
    assert provider.stop_calls == 1


# ---------------------------------------------------------------------------
# 同一判据的第二处消费位：reconcile 的 STOPPING + ALIVE
# ---------------------------------------------------------------------------


def _mark_stopping(wid: str) -> None:
    with Factory() as db:
        ws = db.get(Workspace, wid)
        ws.status = WorkspaceStatus.STOPPING.value
        db.commit()


def test_reconcile_does_not_release_a_lying_stop():
    """STOPPING+ALIVE 且停止命令"成功"而卡还在吃：保持 STOPPING，不释放，不计 stopped。"""
    provider = StatefulProvider(behavior="lie")
    orchestrator, wid = _running_workspace(provider)
    _mark_stopping(wid)

    stats = orchestrator.reconcile_all()

    assert stats["stopped"] == 0
    with Factory() as db:
        assert db.get(Workspace, wid).status == WorkspaceStatus.STOPPING.value
        assert _side_effects(db, wid) == NOT_RELEASED


def test_reconcile_stops_a_live_runtime_and_reports_it():
    """同一分支的合规档：再试停止且 provider 认账 ⇒ STOPPED＋释放＋stopped 计数。"""
    provider = StatefulProvider()
    orchestrator, wid = _running_workspace(provider)
    _mark_stopping(wid)

    stats = orchestrator.reconcile_all()

    assert stats["stopped"] == 1
    with Factory() as db:
        assert db.get(Workspace, wid).status == WorkspaceStatus.STOPPED.value
        assert _side_effects(db, wid) == RELEASED
    assert provider.alive is False


# ---------------------------------------------------------------------------
# 没达成目标时对外说了什么：边界异常不外泄、operation 不记假成功
# ---------------------------------------------------------------------------


def test_release_failure_stays_inside_the_boundary_and_is_recoverable():
    """`scheduler.release` 抛错：不泄漏给调用方（ADR 0002），状态留 STOPPING，卡仍占着。"""
    provider = StatefulProvider()
    orchestrator, wid = _running_workspace(provider)
    failing = FailingReleaseScheduler(orchestrator.scheduler)
    orchestrator.scheduler = failing

    with Factory() as db:
        # 这一句不许抛：改前它被 stop() 的 try 兜住过，重构时很容易把终态那段挪出保护范围
        orchestrator.stop(db, db.get(Workspace, wid))

    assert failing.calls == 1
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPING.value
        assert "finalize failed" in (ws.error_message or "")
        # 钱已入账、卡仍占着：可重试的那一半只有 release
        assert _side_effects(db, wid) == FINALIZE_FAILED

    # 换回能用的 scheduler：重试收敛（provider 已认账 runtime 不在），账本按幂等键不重复入账
    orchestrator.scheduler = failing.inner
    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))
    with Factory() as db:
        assert db.get(Workspace, wid).status == WorkspaceStatus.STOPPED.value
        assert _side_effects(db, wid) == RELEASED


def test_durable_stop_is_not_recorded_succeeded_while_runtime_alive():
    """STOP operation 的判决跟着"目标达成"走，不跟着"没抛异常"走。"""
    provider = StatefulProvider(behavior="lie")
    orchestrator, wid = _running_workspace(provider)
    worker = OperationWorker(Factory, orchestrator)

    op = worker.enqueue(wid, "stop")
    assert op is not None
    worker.tick_once()

    with Factory() as db:
        db_op = db.get(type(op), op.id)
        assert db_op.status == OperationStatus.RETRYING.value, (
            f"卡还被活容器吃着，STOP 却记成 {db_op.status}——operation 台账在替没做的事背书"
        )
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPING.value
        assert _side_effects(db, wid) == NOT_RELEASED
    assert provider.alive is True


def test_quota_monitor_does_not_count_a_stop_that_was_not_admitted():
    """透支触发的停止没被放行时：不报 stopped，也不拿配额原因盖掉真正阻塞的原因。

    monitor 走的就是 `stop()`（orchestrator.py:682），所以放行判据一处生效、两处受益；
    这条钉的是它自己的台账：`stats["stopped"]` 是"我停下了几台"的对外主张。
    """
    provider = StatefulProvider(behavior="lie")
    ledger = CreditLedgerService(Factory)
    orchestrator = WorkspaceOrchestrator(
        Factory,
        provider,
        Path("/tmp/test-stop-admission"),  # noqa: S108 测试隔离目录
        streaming=StreamingSessionService(Factory),
        ledger=ledger,
        billing=BillingPolicy(Factory, ledger),
    )
    with Factory() as db:
        _seed(db)
        db.add(
            User(
                id="u1", email="u@x", username="u",
                password_hash="x",  # noqa: S106 测试桩用户，非真实密码
                role=Role.USER.value,
            )
        )
        db.commit()
        # 先给够启动额度（否则 provision 自己被门禁挡掉，这条会绿得毫无意义）
        ledger.record(
            db, type=LedgerType.RECHARGE, amount=1000, user_id="u1", description="recharge"
        )
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = ws.id
    orchestrator._start(wid)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value
        ws.started_at = datetime.now(UTC) - timedelta(seconds=30)
        # 投影余额转负：另一段的消耗把余额吃穿（monitor 的触发条件）
        ledger.record(
            db, type=LedgerType.USAGE, amount=-5000, user_id="u1", description="overdraft"
        )
        db.commit()

    stats = orchestrator.monitor_runtime_quotas()

    assert stats["stopped"] == 0, "runtime 还活着却把这台算进已停"
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPING.value
        # 留下的是放行判据的原因，不是"credits exhausted"
        assert "provider reports runtime alive" in (ws.error_message or ""), ws.error_message
        assert _side_effects(db, wid) == NOT_RELEASED


# ---------------------------------------------------------------------------
# 判据本身：一条判决只许有一份实现，两个入口都得经它
# ---------------------------------------------------------------------------

def release_admission_wiring(source: str) -> dict[str, int]:
    """从源码 AST 读"释放准入判据"的定义位与消费链。

    返回 `{"definitions": _release_admitted 定义数, "cleanup_definitions": _stop_cleanup 定义数,
    "stop_consumers": stop() 里对 _stop_cleanup 的调用数,
    "reconcile_consumers": reconcile_all() 里对 _stop_cleanup 的调用数}`。

    链的形状是：判据本体只有一份，消费位点共四处 —— `_stop_cleanup` 的成功档与报错档
    （同一个收尾函数的两极）、`_fail` 的 provision 补偿档（N-67 接上），入口侧 `stop` 走
    `_stop_cleanup` 一处、`reconcile_all` 走它两处（STOPPING+ALIVE 的重试档与 RUNNING 的
    节点不一致档）。按 AST 判而不是按文本判：
    注释里出现判据名不算接上，挪动行号也不会让这条判据失效。
    """
    tree = ast.parse(source)

    def calls_in(func_name: str, target: str) -> int:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                return sum(
                    1
                    for inner in ast.walk(node)
                    if isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == target
                )
        return -1  # 函数不存在：与"存在但没接线"分开报，别让缺席读成 0

    return {
        "definitions": sum(
            1
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_release_admitted"
        ),
        "cleanup_definitions": sum(
            1
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_stop_cleanup"
        ),
        "admitted_uses": sum(
            1
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_release_admitted"
        ),
        "stop_consumers": calls_in("stop", "_stop_cleanup"),
        # N-90：`reconcile_all` 的逐格判决被搬进 `_reconcile_one`（异常边界要落在「一格」上）。
        # 按两个函数名一起数，缺席那一侧由 -1 哨兵顶出来（-1 + 2 == 1 ≠ 2 即红）：宁可误报
        # 「入口没委派」，也不放过「少接一条收尾分支」。「入口确实把每交给它」由
        # tests/test_reconcile_cell_isolation.py 的结构判据钉住，不靠这里代劳。
        "reconcile_consumers": calls_in("reconcile_all", "_stop_cleanup")
        + calls_in("_reconcile_one", "_stop_cleanup"),
    }


def _orchestrator_source() -> str:
    return (REPO_ROOT / "app" / "services" / "orchestrator.py").read_text(encoding="utf-8")


def test_the_release_admission_judgment_has_one_copy_both_entries_consume_it():
    """判据恰好一份实现、共三处消费；入口侧另有三处分支经 `_stop_cleanup` 接上它。

    N-68 之后 reconcile 的节点不一致分支也走 `_stop_cleanup`（不再自己 settle+release+FAILED），
    所以 `reconcile_consumers` 从 1 变 2 —— 这条判据是"接线位点"的棘轮，新增消费位点必须
    连同本行一起改判，不许悄悄多接或不接。
    N-67 之后 `admitted_uses` 从 2 变 3：provision 失败那一路（`_fail`）也接上了同一份判据，
    改前三处之外的它就是"卡被盲放"的那一处。
    """
    assert release_admission_wiring(_orchestrator_source()) == {
        "definitions": 1,
        "cleanup_definitions": 1,
        "admitted_uses": 3,  # `_stop_cleanup` 的成功档与报错档 + `_fail` 的补偿档
        "stop_consumers": 1,
        "reconcile_consumers": 2,
    }


def test_the_wiring_checker_can_see_a_disconnected_consumer():
    """反向对照：`reconcile_all` 的两条收尾分支各断一次，尺子都要看见。

    没有这一支，上一条判据在"判据本身读不到东西"的形状下会一直绿着。
    N-68 之后 reconcile 的收尾分支有两处 `_stop_cleanup`（STOPPING+ALIVE 重试档、
    RUNNING 节点不一致档；N-90 起这两档住在 `_reconcile_one` 里），所以变异要分三档：全不改=2、只断一条=1、两条都退回
    直接 finalize=0。只数"0 与 2"会把"其中一条分支根本没接"读成通过。
    """
    source = _orchestrator_source()
    line = "error = self._stop_cleanup(db, w)"
    # 落地证明按锚点唯一性做：空改写（命中 0）不许被读成"变异通过"
    assert source.count(line) == 2, f"锚点不是恰好两处（{source.count(line)}）"
    one_cut = source.replace(line, "self._finalize_stop(db, w)", 1)
    assert release_admission_wiring(one_cut)["reconcile_consumers"] == 1, "少一条分支看不见"
    both_cut = source.replace(line, "self._finalize_stop(db, w)")
    assert release_admission_wiring(both_cut)["reconcile_consumers"] == 0
    # 另一极：真实源码上同一把尺子不开火
    assert release_admission_wiring(source)["reconcile_consumers"] == 2


@pytest.mark.parametrize("behavior", BEHAVIORS)
def test_no_behavior_leaves_the_gpu_free_while_the_runtime_is_alive(behavior: str):
    """三档行为的共同不变量：provider 说活着时，卡状态与分配行不得同时"已释放"。

    参数化不新增判据，只是把同一个不变量在三档上各跑一遍；`stops` 与
    `error_then_gone` 两档 runtime 最终不活着，因此该支只断"不出现
    GPU AVAILABLE ＋ provider.alive"这一对矛盾。
    """
    provider = StatefulProvider(behavior=behavior)
    orchestrator, wid = _running_workspace(provider)

    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))

    with Factory() as db:
        effects = _side_effects(db, wid)
    assert not (effects["gpu_free"] and provider.alive), (behavior, effects, provider.alive)


class UnreachableProvider(StatefulProvider):
    """`stop` 照常被叫，但此后连"还在不在"都答不上来（`reconcile` 自己抛）。

    判据的第三种结果（ADR 0008 同一口径：问不到不等于不存在）。这一档同时钉两件事：
    不放卡，以及异常不得穿出 ADR 0002 划的 provider/scheduler 边界 —— 由 `stop()`
    正常返回这一隐含断言承担（`_stop_cleanup` 的报错档是在 except 处理器里第二次问的，
    若判据不吞这个异常，它就会一路穿出 `stop()`）。
    """

    def reconcile(self, workspace):  # type: ignore[override]
        raise RuntimeError("engine unreachable")


def test_unobservable_runtime_does_not_release_and_does_not_escape_stop():
    provider = UnreachableProvider(behavior="stops")
    orchestrator, wid = _running_workspace(provider)

    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))  # 不外抛 = 本条的隐含断言

    with Factory() as db:
        ws = db.get(Workspace, wid)
        effects = _side_effects(db, wid)
        assert ws.status == WorkspaceStatus.STOPPING.value, ws.status
        assert effects["allocated"] is True and effects["gpu_free"] is False, effects
        # 原因写的是 provider 亲口的回话：没放行有两种子情形（自述活着／问不到），
        # 只点名其中一种的消息在另一种场合就是假话。这一档必须是 unobservable。
        msg = ws.error_message or ""
        assert "release not admitted" in msg and "unobservable" in msg, msg

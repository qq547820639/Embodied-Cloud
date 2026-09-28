"""池子的余量与分配器的挑卡必须是**同一条证据**（N-114 的 (a) 面，接 N-112）。

缺陷（本轮已在产品代码里修掉，本文件把它钉住）：`WarmPoolManager._free_capacities_gib`
（`app/services/warmpool.py:194`）过去只按 `select(Gpu.memory_total).where(Gpu.status ==
AVAILABLE)` 数空闲显存，而 N-112 之后分配器（`app/services/scheduler.py:197-203`）在同一个
WHERE 里还多要一条 `host_is_visible()`（定义在 `app/services/scheduler.py:67`，一条
EXISTS 在 `gpu_hosts.status == 'online'` 上）。两个谓词各说各话的后果是一串空转：一台
停止同步的节点上那张仍标 AVAILABLE 的卡被池子记成余量 ⇒ 补位闸门
（`app/services/warmpool.py:127` 取余量、`:156-161` 判放行）开出 PREWARMING 一格 ⇒
worker 起来占不到卡 ⇒ 收割成 FAILED → 入队 DESTROY → 冷却，一整个预热周期白烧。

修法是**借那一条谓词**（`app/services/warmpool.py:44` 从 `.scheduler` import
`host_is_visible`），刻意不手写第二份规则——第二份会自己漂，漂了就是这次的缺陷重演。

判据分两组。

行为面三支：
- 第 1 支是决定性的：唯一够用的卡挂在失联节点上 ⇒ 这一格不许开。
- 第 2 支证明它是证据驱动而非永久抽干：节点重新同步的下一趟 `maintain()` 照开。
- 第 3 支是必须不开火的对照：两台都在线时闸门照旧放行，并把"池子挑的卡＝分配器
  真占的卡"这一同向性钉住（best-fit 最小够用，两边选中同一张大卡）。

结构面两支：
- 第 4 支读 `warmpool.py` 的语法树，钉"调的是那条共享谓词、名字来自 `.scheduler`、
  函数体里没有 `GpuHost`"——即同一条规则而非第二份副本。纯语法、不建表、不开库。
- 第 5 支是第 4 支的反证控制：同一把尺子拿去读人造源，"合规／自己重抄了一遍规则／
  根本没有规则"三种读数必须互不相同，否则第 4 支的绿说明不了任何事。

限制（写清楚，别让人误读）：
- 本文件证明的是**池子停止开这一格**。它不证明 worker 那一侧的失败代价（占不到卡 →
  FAILED → DESTROY → 冷却那一串）——那要真把 PROVISION 驱动到失败，属
  `tests/test_warmpool.py` 与 `tests/test_provision_release_admission.py` 的范围。
- 第 1 支里"两张卡的行都没被改过"钉的是 N-112 那条"不许顺手改写 `gpus.status`"的
  不变量（理由在 `app/services/scheduler.py:67-78`）。它在改前也成立（`maintain` 只
  create ＋ 入队，不占卡），因此是护栏不是判别位；判别位是 `created == 0` 与零行 workspace。
- "失联"一律走证据列：把 `last_synced_at` 写成模块级固定时刻，再由仓库自己的
  `expire_stale_hosts(..., now=...)` 改判 `online/offline`。不手写 `UPDATE gpu_hosts SET
  status='offline'`——手工覆盖结论列的话，本文件测的就不是那条 EXISTS 而是我自己。
- 没有 sleep、没有经过时间断言、也没有拿墙上时钟比较：判决时刻 `JUDGED_AT` 与两条
  `last_synced_at` 都是固定值。（第 2 支的"恢复在线"走真 `sync_host`，它盖的是当前时刻，
  但那一支之后再没有第二次改判，所以那个值不参与任何比较。）
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    Gpu,
    GpuHost,
    GpuStatus,
    OperationStatus,
    OperationType,
    Template,
    WarmPoolState,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.scheduler import HOST_OFFLINE, HOST_ONLINE, GpuInfo, GpuScheduler
from app.services.warmpool import WarmPoolManager
from app.services.worker import OperationWorker
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("warmpool-capacity"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)

CAPACITY_SOURCE = Path(__file__).resolve().parents[1] / "app" / "services" / "warmpool.py"

# ---------------------------------------------------------------------------
# 固定时刻：stale 由"证据写死 + 仓库自己的改判"造出来，不靠真实经过时间
# ---------------------------------------------------------------------------
BASE = datetime(2026, 1, 1, tzinfo=UTC)
STALE_AFTER_SECONDS = 600
JUDGED_AT = BASE + timedelta(seconds=STALE_AFTER_SECONDS + 30)
# 在线那台的证据：比判决时刻新 30 秒 ⇒ `expire_stale_hosts` 不碰它（当场断言，见 _seed_fleet）
FRESH_AT = BASE + timedelta(seconds=STALE_AFTER_SECONDS)

SMALL_GIB = 8  # 唯一一张不够模板的卡
LARGE_GIB = 24  # 全场唯一够用的卡，挂在失联节点上
REQUIREMENT_GB = 16

ONLINE_NODE = "host-online"
LOST_NODE = "host-lost"
ONLINE_CARD = "gpu-small"
LOST_CARD = "gpu-large"
TEMPLATE_ID = "lab16"


@pytest.fixture(autouse=True)
def _fresh_db():
    """每例一次重建表；每段读写各开一个短命会话（见 `_maintain` 的注释）。

    会话一旦在事务里读过一次，就锁在自己的快照上：另一个会话提交之后它读到的还是旧账
    （`tests/test_warmpool.py:322` 用 `db.rollback()` 顶这件事）。这里的形状是**根本不分享
    会话**，于是也没有可失效的快照，更没有哪个会话能活到下一例、把表结构之外的东西带过去。
    """
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


# ---------------------------------------------------------------------------
# 夹具（形状抄 tests/test_warmpool.py：建模板／播种卡／造 manager；不跨文件 import fixture）
# ---------------------------------------------------------------------------


def _make_template() -> None:
    """两列一起设：闸门读 `gpu_requirement_gb`（分配器也用这一列），展示列只给人看。

    只设展示列正是 `tests/test_warmpool.py:660` 那支钉过的缺陷形状，所以这里按"真需求"设。
    """
    with Factory() as db:
        db.add(
            Template(
                id=TEMPLATE_ID,
                slug=TEMPLATE_ID,
                name=TEMPLATE_ID,
                version="0.1.0",
                description="test",
                category="test",
                runtime="mock",
                enabled=True,
                gpu_requirement_gb=REQUIREMENT_GB,
                recommended_vram_gb=REQUIREMENT_GB,
            )
        )
        db.commit()


def _sync_node(host_id: str, gpu_uuid: str, gib: int) -> None:
    """走真 inventory 入口：host 落 online（结论）＋ `last_synced_at`（证据），卡落 AVAILABLE。"""
    with Factory() as db:
        GpuScheduler(Factory).sync_host(
            db,
            host_id=host_id,
            name=f"name-{host_id}",
            address="10.0.0.1",
            provider="mock",
            gpus=[GpuInfo(gpu_uuid=gpu_uuid, model="m", memory_total=gib * 1024, index=0)],
        )


def _stamp(host_id: str, seen_at: datetime) -> None:
    """把"最近一次同步成功"的证据写成固定时刻。"""
    with Factory() as db:
        host = db.get(GpuHost, host_id)
        assert host is not None, f"没有这台节点：{host_id}"
        host.last_synced_at = seen_at
        db.commit()


def _expire_stale_hosts() -> None:
    with Factory() as db:
        moved = GpuScheduler(Factory).expire_stale_hosts(
            db,
            offline_after_seconds=STALE_AFTER_SECONDS,
            now=JUDGED_AT,
        )
    assert moved == 1, f"该被改判的是恰好一台，实得 {moved} 台：前提塌了，后面读数一律作废"


def _host_status(host_id: str) -> str:
    with Factory() as db:
        return db.get(GpuHost, host_id).status


def _card(gpu_uuid: str) -> tuple[str, str | None]:
    """(status, workspace_id)：从**另一个会话**读，只看得见已提交的事实。"""
    with Factory() as db:
        gpu = db.scalar(select(Gpu).where(Gpu.gpu_uuid == gpu_uuid))
        assert gpu is not None, f"没有这张卡：{gpu_uuid}"
        return gpu.status, gpu.workspace_id


def _workspace_rows() -> list[tuple[str, str | None, str]]:
    """(id, warm_pool_state, status)，按 id 排，保证判据读到的次序是确定的。"""
    with Factory() as db:
        return [
            (w.id, w.warm_pool_state, w.status)
            for w in db.scalars(select(Workspace).order_by(Workspace.id))
        ]


def _queued_provision_ops() -> int:
    with Factory() as db:
        return int(
            db.scalar(
                select(func.count(WorkspaceOperation.id)).where(
                    WorkspaceOperation.operation_type == OperationType.PROVISION.value,
                    WorkspaceOperation.status == OperationStatus.PENDING.value,
                )
            )
            or 0
        )


def _make_manager(*, size: int = 1, reserve: int = 0) -> WarmPoolManager:
    provider = MockProvider("http://127.0.0.1:8000")
    orchestrator = WorkspaceOrchestrator(
        Factory,
        provider,
        Path("/tmp/test-embodiedcloud-warmpool-capacity"),  # noqa: S108 测试隔离目录
    )
    settings = SimpleNamespace(
        warm_pool_enabled=True,
        warm_pool_size=size,
        warm_pool_reserve_slots=reserve,
    )
    return WarmPoolManager(Factory, orchestrator, settings)


def _maintain(manager: WarmPoolManager) -> dict[str, int]:
    with Factory() as db:
        return manager.maintain(db)


def _drain(manager: WarmPoolManager, max_ticks: int = 20) -> None:
    """跑空队列再返回：收敛条件是"tick 说没活了"，不是等了多久。"""
    worker = OperationWorker(Factory, manager.orchestrator)
    for _ in range(max_ticks):
        if worker.tick_once() == 0:
            return
    raise AssertionError(f"PROVISION 队列 {max_ticks} 轮没排空")


def _seed_fleet(*, lose_large_node: bool) -> None:
    """两台节点各一张卡：小卡那台留在 online，大卡那台按需要判成失联。"""
    _sync_node(ONLINE_NODE, ONLINE_CARD, SMALL_GIB)
    _sync_node(LOST_NODE, LOST_CARD, LARGE_GIB)
    _make_template()
    if lose_large_node:
        _stamp(ONLINE_NODE, FRESH_AT)
        _stamp(LOST_NODE, BASE)
        _expire_stale_hosts()
        assert _host_status(ONLINE_NODE) == HOST_ONLINE
        assert _host_status(LOST_NODE) == HOST_OFFLINE
    else:
        assert (_host_status(ONLINE_NODE), _host_status(LOST_NODE)) == (HOST_ONLINE, HOST_ONLINE)
    # 前提的另一半：失联只动结论列，没动卡
    assert _card(LOST_CARD) == (GpuStatus.AVAILABLE.value, None), "失联不许顺手改写 gpus.status"


# ---------------------------------------------------------------------------
# 1) 决定性的那一支：唯一够用的卡挂在失联节点上 ⇒ 这一格不许开
# ---------------------------------------------------------------------------


def test_the_pool_does_not_fill_a_slot_only_an_invisible_node_can_serve() -> None:
    """场上唯一装得下 16 GiB 的卡挂在失联节点上 ⇒ 池子不许开这一格。

    改前这里数到的余量是 `[8, 24]`（`gpus.status` 一列说了算），闸门挑走 24 那张并开出
    PREWARMING；而分配器 `app/services/scheduler.py:197-203` 拒发那张卡，于是那一格从
    生下来就注定是"provision 失败 → 收割 FAILED → DESTROY → 冷却"的一行。
    """
    _seed_fleet(lose_large_node=True)
    manager = _make_manager(size=1, reserve=0)

    stats = _maintain(manager)

    assert stats["created"] == 0, f"池子为一张分配器拒发的卡开了格 {stats}"
    assert stats["skipped_no_capacity"] >= 1, f"没放行却没记账，运维只看见池子莫名其妙是空的 {stats}"
    assert _workspace_rows() == [], f"开出来的行就是那行注定失败的行：{_workspace_rows()}"
    assert _queued_provision_ops() == 0, "PROVISION 已入队：worker 一起就去吃那次必然失败"
    # N-112 的不变量仍在：闸门改判的是"开不开格"，不是把卡的状态抬走或绑给谁
    assert _card(LOST_CARD) == (GpuStatus.AVAILABLE.value, None), "不许被顺手改写状态列/绑定"
    assert _card(ONLINE_CARD) == (GpuStatus.AVAILABLE.value, None)


# ---------------------------------------------------------------------------
# 2) 它是证据驱动的闸门，不是永久抽干
# ---------------------------------------------------------------------------


def test_the_same_slot_is_filled_once_the_node_is_visible_again() -> None:
    """一次成功重报把节点抬回 online ⇒ **同一个 manager** 的下一趟 maintain 照开这一格。

    复用同一个实例是判据的一部分：冷却退避（`app/services/warmpool.py:143`）与 `_fail_cooldown`
    都挂在实例上，换新实例就等于把"上一轮没开格"这件事抹掉再验一次，量不到"证据回来即恢复"。
    """
    _seed_fleet(lose_large_node=True)
    manager = _make_manager(size=1, reserve=0)

    first = _maintain(manager)
    assert first["created"] == 0, f"前提塌了：失联节点上的卡还是给开了格 {first}"

    _sync_node(LOST_NODE, LOST_CARD, LARGE_GIB)  # 仓库唯一的恢复路径：inventory 重报
    assert _host_status(LOST_NODE) == HOST_ONLINE, "重报没把结论列抬回来，这一支就没在测恢复"

    second = _maintain(manager)
    assert second["created"] == 1, f"节点已经重新可见，这一格仍被扣着＝闸门把失联当成了永久注销 {second}"
    assert second["skipped_no_capacity"] == 0, second
    rows = _workspace_rows()
    assert len(rows) == 1, rows
    assert rows[0][1] == WarmPoolState.PREWARMING.value, rows


# ---------------------------------------------------------------------------
# 3) 必须不开火的对照：健康舰队不受影响
# ---------------------------------------------------------------------------


def test_a_healthy_fleet_is_unaffected() -> None:
    """两台都在线、同一份 16 GiB 需求 ⇒ 照开，且池子挑的卡就是分配器真占的那张。

    没有这一支，上面两条的 `created == 0` 也可以来自"闸门见不到在线节点就永远不开格"
    甚至"永远不开格"。后半段把队列真跑空：best-fit（`scheduler.candidate_order`）选最小
    够用的卡，与闸门 `next(g for g in free_gib if g >= required)`（free 已排序）同向——
    两边都该落在 24 GiB 那张上，这是"同一个谓词"最直白的读数。
    """
    _seed_fleet(lose_large_node=False)
    manager = _make_manager(size=1, reserve=0)

    stats = _maintain(manager)
    assert (stats["created"], stats["skipped_no_capacity"]) == (1, 0), stats
    rows = _workspace_rows()
    assert len(rows) == 1, f"健康舰队上只该开一格：{rows}"
    slot_id, state, status = rows[0]
    assert state == WarmPoolState.PREWARMING.value, rows
    assert status == WorkspaceStatus.CREATED.value, rows
    assert _queued_provision_ops() == 1, "补位是异步入队的：PROVISION 必须在队列里等着 worker"
    # maintain 自己不占卡（`app/services/warmpool.py:178-190` 只 create + start_async）
    assert _card(LOST_CARD) == (GpuStatus.AVAILABLE.value, None)

    _drain(manager)

    assert _card(LOST_CARD) == (GpuStatus.ALLOCATED.value, slot_id), "大卡那张才是两边共同选中的"
    assert _card(ONLINE_CARD) == (GpuStatus.AVAILABLE.value, None), "小卡本来就不够用，不该被牵连"
    assert _workspace_rows() == [(slot_id, WarmPoolState.PREWARMING.value, WorkspaceStatus.RUNNING.value)]


# ---------------------------------------------------------------------------
# 结构面：同一条谓词，不是第二份规则
#
# 只读语法树，不建表、不开库，也不 import 被测模块——判据要能在产品代码抛错时照样读数。
# ---------------------------------------------------------------------------

CAPACITY_FN = "_free_capacities_gib"
SHARED_PREDICATE = "host_is_visible"
HOST_TABLE = "GpuHost"

VERDICT_COMPLIANT = "compliant"
VERDICT_DUPLICATE = "duplicate_rule"
VERDICT_ABSENT = "no_rule"


def _capacity_fn(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == CAPACITY_FN:
            return node
    return None


def _names(fn: ast.AST) -> set[str]:
    """函数体里出现过的名字（字符串/docstring 不算：它们不是 Name 节点）。"""
    return {node.id for node in ast.walk(fn) if isinstance(node, ast.Name)}


def _called(fn: ast.AST, name: str) -> bool:
    """被**调用**了（裸名或 `mod.name(...)`）——只是引用一下而没 apply 到查询里等于没吃这条谓词。"""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id == name:
            return True
        if isinstance(target, ast.Attribute) and target.attr == name:
            return True
    return False


def _imported_from_scheduler(tree: ast.Module) -> set[str]:
    """从 `*.scheduler`（含 `.scheduler`）按原名 import 进来的名字集合。"""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("scheduler"):
            found.update(alias.name for alias in node.names if alias.asname is None)
    return found


def _rule_verdict(source: str) -> str:
    """这个源里的余量计算读的是哪条规则。三档互斥，优先级：重抄 > 合规 > 没有。

    "重抄"排在最前是有意的：一份代码里同时出现共享谓词调用和自己那条 `GpuHost` EXISTS，
    就是两份规则各说各话的开端，正是本次缺陷的形状，不该被读成合规。
    """
    tree = ast.parse(source)
    fn = _capacity_fn(tree)
    if fn is None:
        return VERDICT_ABSENT
    if HOST_TABLE in _names(fn):
        return VERDICT_DUPLICATE
    if (
        _called(fn, SHARED_PREDICATE)
        and SHARED_PREDICATE in _imported_from_scheduler(tree)
    ):
        return VERDICT_COMPLIANT
    return VERDICT_ABSENT


_COMPLIANT_SRC = '''
from sqlalchemy import select

from app.models import Gpu, GpuStatus

from .scheduler import host_is_visible


def _free_capacities_gib(db):
    return [
        int(memory_mib // 1024)
        for memory_mib in db.scalars(
            select(Gpu.memory_total).where(
                Gpu.status == GpuStatus.AVAILABLE.value,
                host_is_visible(),
            )
        )
    ]
'''

# 同一形状，只把共享谓词换成手写的一份副本（`GpuHost.status == "online"`）
_DUPLICATE_RULE_SRC = '''
from sqlalchemy import select

from app.models import Gpu, GpuHost, GpuStatus

from .scheduler import host_is_visible


def _free_capacities_gib(db):
    return [
        int(memory_mib // 1024)
        for memory_mib in db.scalars(
            select(Gpu.memory_total).where(
                Gpu.status == GpuStatus.AVAILABLE.value,
                select(GpuHost.id).where(GpuHost.status == "online").exists(),
            )
        )
    ]
'''

# 改前的形状：只按 `gpus.status` 数卡，没有可见性这一半
_NO_RULE_SRC = '''
from sqlalchemy import select

from app.models import Gpu, GpuStatus


def _free_capacities_gib(db):
    return [
        int(memory_mib // 1024)
        for memory_mib in db.scalars(
            select(Gpu.memory_total).where(Gpu.status == GpuStatus.AVAILABLE.value)
        )
    ]
'''


def test_the_two_predicates_are_the_same_object_not_a_second_copy() -> None:
    """闸门吃的是 `.scheduler` 那条 `host_is_visible`，不是池子里重抄的第二份。

    (a)+(b) 合起来就是"同一个对象"：模块里这个名字只有一个绑定，而那个绑定来自
    `app/services/scheduler.py:67`；`_free_capacities_gib` 调的正是它。再加上 (c)
    函数体不出现 `GpuHost`——规则的实现细节没有第二处落点。
    """
    source = CAPACITY_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = _capacity_fn(tree)
    assert fn is not None, f"{CAPACITY_SOURCE.name} 里找不到 {CAPACITY_FN}，这把尺没有作用面"

    body_names = sorted(_names(fn))
    imported = sorted(_imported_from_scheduler(tree))

    # (a) 函数体里确实把它**用上了**
    # 失败文案带上两处真实读数：换成手写副本的变异要在这一步就被看见，而不是只看见"没调用"
    assert _called(fn, SHARED_PREDICATE), (
        f"{CAPACITY_FN} 没调用 {SHARED_PREDICATE}()：余量与分配器又分成两条谓词说话（N-114 的形状）。"
        f"函数体里的名字：{body_names}；从 *.scheduler import 的：{imported}"
    )
    # (b) 那个名字来自 scheduler 模块（相对或绝对 import 都算）
    assert SHARED_PREDICATE in imported, (
        f"{SHARED_PREDICATE} 不在从 *.scheduler import 的名字里（实得 {imported}）："
        "闸门在用另一处的实现，不是分配器那一条"
    )
    # (c) 函数体里没有第二份规则的实现细节
    assert HOST_TABLE not in body_names, (
        f"{CAPACITY_FN} 里出现了 {HOST_TABLE}：这是手写的第二份可见性规则，它会自己漂"
    )
    assert _rule_verdict(source) == VERDICT_COMPLIANT


def test_the_structural_ruler_fires_when_the_pool_reimplements_the_rule() -> None:
    """第 4 支的反证：同一把尺必须能读出三种不同形状，否则它的绿不说明任何事。

    三档各自只由一个变量分开：合规 vs 重抄＝谓词来源换了；合规 vs 没有＝那一半被删掉。
    第四档（删掉 import 那一行）钉的是 (b) 半边承重——只查"调用了 host_is_visible"的尺子
    读不出"名字从哪来"，那样的话在池子里 `def host_is_visible` 一份也会照样判绿。
    """
    real_source = CAPACITY_SOURCE.read_text(encoding="utf-8")
    assert _rule_verdict(real_source) == VERDICT_COMPLIANT, "真源判出的读数不是合规：第 4 支与这把尺打架"

    # 作用面前提：人造源里真有那个函数，否则"读不到规则"是夹具塌了而不是判据开火
    for name, synthetic in (
        ("compliant", _COMPLIANT_SRC),
        ("duplicate", _DUPLICATE_RULE_SRC),
        ("no_rule", _NO_RULE_SRC),
    ):
        assert _capacity_fn(ast.parse(synthetic)) is not None, f"人造源 {name} 里没有 {CAPACITY_FN}"

    no_import_src = _COMPLIANT_SRC.replace("from .scheduler import host_is_visible\n", "")
    assert no_import_src != _COMPLIANT_SRC, (
        "import 那行在人造源里删不掉（needle 写错了）：(b) 半边没有对照，谈不上承重"
    )

    readings = {
        "real": _rule_verdict(real_source),
        "synthetic_compliant": _rule_verdict(_COMPLIANT_SRC),
        "duplicate_rule": _rule_verdict(_DUPLICATE_RULE_SRC),
        "no_call": _rule_verdict(_NO_RULE_SRC),
        "no_import": _rule_verdict(no_import_src),
    }
    assert readings["real"] == VERDICT_COMPLIANT, readings
    assert readings["synthetic_compliant"] == VERDICT_COMPLIANT, readings
    assert readings["duplicate_rule"] == VERDICT_DUPLICATE, (
        f"手写的 GpuHost EXISTS 没被判成第二份规则：{readings}"
    )
    assert readings["no_call"] == VERDICT_ABSENT, readings
    assert readings["no_import"] == VERDICT_ABSENT, readings
    # 三档必须互不相同：一把只会吐一个值的尺子过不了这一行
    assert len({VERDICT_COMPLIANT, VERDICT_DUPLICATE, VERDICT_ABSENT}) == 3
    assert len({readings["synthetic_compliant"], readings["duplicate_rule"], readings["no_call"]}) == 3, readings

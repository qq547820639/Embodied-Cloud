"""GPU 主机的「在线」要由最近一次同步背书（N-110）。

缺陷的来龙去脉：`gpu_hosts.status` 的默认值是 `online`，而全仓唯一的写入点是
`GpuScheduler.sync_host`；`sync_host` 又只被 `bootstrap_gpu_inventory` 调用，后者
过去只有 `bootstrap_db` 一个调用者（开机那一脚）。于是两件事同时成立：

1. 一台早已离开集群的节点，对 `GET /api/gpus/hosts` 与前端表格永久报 online；
2. `sync_host` 里"未再上报的 GPU → DRAINING"那段收敛在进程存活期内没有驱动者——
   节点少了一张卡，要等重启才看得见。

这与 N-108 是同一个问题的两个面（存在性主张要有事实背书），也与 N-93/N-98 是
同一个问题的另一个面（有收敛、没驱动者）。这一档判据钉的是改完之后：
证据列有人写、判决只读证据列、两个驱动者都在周期表上、且它们跑的是同一份实现。

夹具用 tmp_path 下的**文件库**而不是内存库：StaticPool 会让所有会话共用同一条连接，
那时"另一个会话读到了"证明不了 commit 真的发生过——而"忘了提交"正是这里要防的
一种改法（N-108 的 K4 臂量出来过）。
"""

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import deps
from app.db import Base
from app.models import Gpu, GpuHealth, GpuHost, GpuStatus
from app.services.scheduler import HOST_OFFLINE, HOST_ONLINE, GpuInfo, GpuScheduler
from app.utils import utcnow

DEPS_SRC = Path(deps.__file__).resolve()
THRESHOLD_SECONDS = 600


@pytest.fixture()
def factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hosts.db'}", future=True)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, future=True)
    engine.dispose()


def _sync(sf, host_id: str = "h1", gpus: tuple[str, ...] = ("gpu-a", "gpu-b")) -> None:
    with sf() as db:
        GpuScheduler(sf).sync_host(
            db,
            host_id=host_id,
            name=f"name-{host_id}",
            address="10.0.0.1",
            provider="docker",
            gpus=[GpuInfo(gpu_uuid=u, model="m", memory_total=24564, index=i) for i, u in enumerate(gpus)],
        )


def _backdate(sf, host_id: str, seconds: int) -> None:
    with sf() as db:
        host = db.get(GpuHost, host_id)
        host.last_synced_at = utcnow() - timedelta(seconds=seconds)
        db.commit()


def _status(sf, host_id: str) -> str:
    """从**另一个会话**读（同一文件库、另一条连接）：只看得见已提交的事实。"""
    with sf() as db:
        return db.get(GpuHost, host_id).status


# ---------------------------------------------------------------------------
# 判决只读证据列
# ---------------------------------------------------------------------------
def test_a_host_that_stopped_syncing_is_judged_offline(factory) -> None:
    """同步过期 ⇒ offline，且改动当场落库（不提交的话读者仍看见 online）。"""
    _sync(factory)
    _backdate(factory, "h1", THRESHOLD_SECONDS + 1)

    with factory() as db:
        assert GpuScheduler(factory).expire_stale_hosts(
            db, offline_after_seconds=THRESHOLD_SECONDS
        ) == 1

    assert _status(factory, "h1") == HOST_OFFLINE
    with factory() as db:  # 幂等：已经 offline 的不算第二次改判
        assert GpuScheduler(factory).expire_stale_hosts(
            db, offline_after_seconds=THRESHOLD_SECONDS
        ) == 0
    assert _status(factory, "h1") == HOST_OFFLINE


def test_a_recently_synced_host_stays_online(factory) -> None:
    """不开火对照：刚同步过的节点不该被误判。"""
    _sync(factory)

    with factory() as db:
        assert GpuScheduler(factory).expire_stale_hosts(
            db, offline_after_seconds=THRESHOLD_SECONDS
        ) == 0
    assert _status(factory, "h1") == HOST_ONLINE


def test_a_host_that_never_synced_is_left_alone(factory) -> None:
    """`last_synced_at IS NULL`（本列上线前的存量行）无从判失效，一律不动。"""
    _sync(factory)
    with factory() as db:
        db.get(GpuHost, "h1").last_synced_at = None
        db.commit()

    with factory() as db:
        assert GpuScheduler(factory).expire_stale_hosts(
            db, offline_after_seconds=1  # 阈值小到 1 秒也不该把它判死
        ) == 0
    assert _status(factory, "h1") == HOST_ONLINE


def test_the_threshold_is_the_argument_not_a_baked_in_number(factory) -> None:
    """两侧各卡一次：刚好没过窗口 ⇒ 在线，刚好越过 ⇒ 离线；再换一个窗口判同一行 ⇒ 离线。"""
    _sync(factory)
    _backdate(factory, "h1", THRESHOLD_SECONDS - 1)
    with factory() as db:
        assert GpuScheduler(factory).expire_stale_hosts(
            db, offline_after_seconds=THRESHOLD_SECONDS
        ) == 0
    assert _status(factory, "h1") == HOST_ONLINE

    with factory() as db:
        assert GpuScheduler(factory).expire_stale_hosts(
            db, offline_after_seconds=THRESHOLD_SECONDS - 2
        ) == 1
    assert _status(factory, "h1") == HOST_OFFLINE


def test_each_host_is_judged_on_its_own_row(factory) -> None:
    """一旧一新：只改判旧的那台，别的一律不碰。"""
    _sync(factory, "old")
    _sync(factory, "fresh")
    _backdate(factory, "old", THRESHOLD_SECONDS + 60)

    with factory() as db:
        assert GpuScheduler(factory).expire_stale_hosts(
            db, offline_after_seconds=THRESHOLD_SECONDS
        ) == 1

    assert _status(factory, "old") == HOST_OFFLINE
    assert _status(factory, "fresh") == HOST_ONLINE


# ---------------------------------------------------------------------------
# 证据列由同步写
# ---------------------------------------------------------------------------
def test_syncing_is_what_evidences_online(factory) -> None:
    """同步给证据列盖章、把 offline 判回 online；再次同步会把时间往前推。"""
    _sync(factory)
    with factory() as db:
        first = db.get(GpuHost, "h1").last_synced_at
    assert first is not None, "新发现的节点也必须带上证据"

    _backdate(factory, "h1", THRESHOLD_SECONDS + 1)
    with factory() as db:
        GpuScheduler(factory).expire_stale_hosts(db, offline_after_seconds=THRESHOLD_SECONDS)
    assert _status(factory, "h1") == HOST_OFFLINE

    _sync(factory)  # 节点重新上报 ⇒ 结论跟着回到 online
    with factory() as db:
        second = db.get(GpuHost, "h1").last_synced_at
    assert second > first
    assert _status(factory, "h1") == HOST_ONLINE


def test_the_word_online_has_exactly_one_source() -> None:
    """列默认值与判决用的常量必须是同一个词，否则"从没同步过"与"在线"会撞名。"""
    assert GpuHost.__table__.columns["status"].default.arg == HOST_ONLINE


# ---------------------------------------------------------------------------
# 驱动者：周期表上的两档
# ---------------------------------------------------------------------------
def test_both_drivers_are_registered_with_their_constants() -> None:
    """两档都在周期表上，且间隔取自常量。

    "取自常量"这一半光靠数值比不出来：`(120, fn)` 与
    `(OperationWorker.PERIODIC_INVENTORY_EVERY, fn)` 在本轮默认值下**数值相等**，
    上面那根判据一路绿（N-110 电池 A8 臂实测存活就是这个原因）。
    所以源码面另有一根 `cadence_literal_offenders`。
    """
    from app.services.worker import OperationWorker

    entries = {getattr(c, "__name__", repr(c)): n for n, c in deps.worker.periodic_tasks}
    assert entries["_refresh_gpu_inventory"] == OperationWorker.PERIODIC_INVENTORY_EVERY
    assert entries["_expire_stale_gpu_hosts"] == OperationWorker.PERIODIC_HOST_SWEEP_EVERY


def cadence_literal_offenders(source: str) -> list[str]:
    """`periodic_tasks=[...]` 里第一元是裸数字的那些档（返回档名，空＝都取自常量）。

    只看真正的字面量元组：`(120, fn)` 记账，`(OperationWorker.PERIODIC_X_EVERY, fn)` 不记。
    """
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.keyword) and node.arg == "periodic_tasks"):
            continue
        elements = node.value.elts if isinstance(node.value, ast.List) else []
        for cell in elements:
            if not (isinstance(cell, ast.Tuple) and cell.elts):
                continue
            cadence = cell.elts[0]
            if isinstance(cadence, ast.Constant):
                target = ast.unparse(cell.elts[1]) if len(cell.elts) > 1 else "?"
                offenders.append(f"{target} 的间隔是裸数字 {cadence.value}")
    return offenders


def test_no_periodic_cadence_is_written_as_a_bare_number() -> None:
    """常量表不能成装饰：每一档的间隔都必须引用它自己那个常量。"""
    assert cadence_literal_offenders(DEPS_SRC.read_text(encoding="utf-8")) == []


def test_the_bare_number_ruler_fires_on_a_literal_cadence() -> None:
    """反向对照：把常量换成数值相等的字面量，尺子必须点名——数值判据看不见这件事。"""
    literal = (
        "worker = OperationWorker(\n"
        "    SessionFactory,\n"
        "    periodic_tasks=[(OperationWorker.PERIODIC_QUOTA_EVERY, a), (120, b)],\n"
        ")\n"
    )
    assert cadence_literal_offenders(literal) == ["b 的间隔是裸数字 120"]
    assert cadence_literal_offenders(
        "worker = OperationWorker(x, periodic_tasks=[(OperationWorker.PERIODIC_X, b)])\n"
    ) == []


def inventory_sweep_offenders(source: str) -> list[str]:
    """`_expire_stale_gpu_hosts` 这个闭包缺了哪些接线要件（空＝都接上了）。

    只看 `ast.Attribute` 的访问名，不看 `ast.dump`：N-108 的 K5 臂实测过，
    docstring 里写着同一个名字就能把代码的洞填平——注释不是接线。
    """
    tree = ast.parse(source)
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_expire_stale_gpu_hosts"
        ),
        None,
    )
    if fn is None:
        return ["_expire_stale_gpu_hosts 不存在"]
    attrs = {node.attr for node in ast.walk(fn) if isinstance(node, ast.Attribute)}
    offenders = []
    if "gpu_host_offline_after_seconds" not in attrs:
        offenders.append("X1:阈值没取自 settings（写死或漏传）")
    if "expire_stale_hosts" not in attrs:
        offenders.append("X2:没调用 expire_stale_hosts")
    return offenders


def test_the_host_sweep_closure_reads_its_threshold_from_config() -> None:
    assert inventory_sweep_offenders(DEPS_SRC.read_text(encoding="utf-8")) == []


def test_the_wiring_ruler_fires_when_the_config_argument_is_dropped() -> None:
    """反向对照：把阈值改成裸数字，尺子必须点名它。"""
    hardcoded = (
        "def _expire_stale_gpu_hosts() -> int:\n"
        "    with SessionFactory() as db:\n"
        "        return scheduler.expire_stale_hosts(db, offline_after_seconds=600)\n"
    )
    assert inventory_sweep_offenders(hardcoded) == ["X1:阈值没取自 settings（写死或漏传）"]
    hooked = hardcoded.replace(
        "offline_after_seconds=600",
        "offline_after_seconds=settings.gpu_host_offline_after_seconds",
    )
    assert inventory_sweep_offenders(hooked) == []
    # N-108 的 K5 就是死在这一形状上：docstring 里写着设置名，而代码传的是裸数字。
    comment_only = (
        "def _expire_stale_gpu_hosts() -> int:\n"
        '    """阈值取自 settings.gpu_host_offline_after_seconds。"""\n'
        "    with SessionFactory() as db:\n"
        "        return scheduler.expire_stale_hosts(db, offline_after_seconds=600)\n"
    )
    assert inventory_sweep_offenders(comment_only) == ["X1:阈值没取自 settings（写死或漏传）"]
    assert inventory_sweep_offenders("def other():\n    pass\n") == ["_expire_stale_gpu_hosts 不存在"]


def test_the_periodic_driver_reuses_the_boot_sync_instead_of_a_second_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """周期档必须回到开机那同一个函数：两套 inventory 同步迟早互相过期。"""
    seen: list[str] = []
    monkeypatch.setattr(deps, "bootstrap_gpu_inventory", lambda db: seen.append("synced"))

    deps._refresh_gpu_inventory()

    assert seen == ["synced"]


def test_a_shrunken_inventory_drains_the_missing_card_without_a_restart(factory) -> None:
    """驱动者存在的意义：第二次同步少报的卡当场 DRAINING。

    三张缺席的卡分开钉（ADR 0010 之后）：
    - AVAILABLE 的那张该被降级；
    - 已被人工下架（DRAINED）的那张**不许**被顺手改写成 DRAINING——缺席降级只作用于
      "还在池子里"的卡（AVAILABLE/DRAINING），别人的判决不是它能覆盖的；
    - unhealthy 的那张照常被降级（它答的是"这次没报上来"），但**健康列一律不动**：
      判决由 `/healthy` 解除，不由一次重报或一次缺席代劳。

    真机上这一步靠 provider 少报；本机 mock provider 永远报同样 8 张，
    所以这里直接喂 `sync_host`——被钉的是"同步本身能收敛"，
    "provider 会在运行中少报"属另一档（未证实，见本轮收尾）。
    """
    _sync(factory, gpus=("gpu-a", "gpu-b", "gpu-c", "gpu-d", "gpu-e"))
    with factory() as db:
        sched = GpuScheduler(factory)
        unhealthy = db.scalar(select(Gpu).where(Gpu.gpu_uuid == "gpu-d"))
        sched.mark_unhealthy(db, unhealthy.id)
        drained = db.scalar(select(Gpu).where(Gpu.gpu_uuid == "gpu-e"))
        assert sched.mark_drained(db, drained.id) is True

    _sync(factory, gpus=("gpu-a", "gpu-b"))  # 节点上报少了 c、d、e

    with factory() as db:
        statuses = {g.gpu_uuid: g.status for g in db.scalars(select(Gpu))}
        healths = {g.gpu_uuid: g.health for g in db.scalars(select(Gpu))}
    assert statuses == {
        "gpu-a": GpuStatus.AVAILABLE.value,
        "gpu-b": GpuStatus.AVAILABLE.value,
        "gpu-c": GpuStatus.DRAINING.value,
        "gpu-d": GpuStatus.DRAINING.value,
        "gpu-e": GpuStatus.DRAINED.value,
    }, statuses
    assert healths["gpu-d"] == GpuHealth.UNHEALTHY.value, "缺席降级不许顺手解除健康判决"
    assert healths["gpu-e"] is None, "别的卡不该被这趟同步写上健康判决"

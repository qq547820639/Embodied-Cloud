"""分配读的是节点的证据，不是卡的判决（N-112，闭 N-111）。

N-110 给 `gpu_hosts` 补了 `last_synced_at`，并让 `expire_stale_hosts` 按它改判
`online/offline`——但那一半只说了真话，没有改变行为：一台彻底不再同步的节点，
它名下那些仍标 `AVAILABLE` 的卡照旧会被 `allocate` 选中，于是新工作区被派到一张
不存在的卡上（N-111 登记的正是这一格）。

为什么不在这里改写 `gpus.status`（把失联节点的卡 DRAINING 掉）：
- 管理员手工 `/drain`、`/unhealthy` 写下的判决有自己的生命周期；inventory 每 2 min
  重报一次，若"重新出现即回 AVAILABLE"，管理员的 drain 活不过两分钟。
- 而"节点暂时看不见"必须能随一次成功重报自动恢复。
读写侧共用同一个谓词就同时满足两边：**没有人改写过卡的状态**，节点回到 online 的
下一趟分配自然看得到那张卡。

判据一律从真数据库读（`allocate` 走真 SELECT ... FOR UPDATE SKIP LOCKED；SQLite 下
该子句是 no-op，所以"锁面只落在 gpus"这一半在这里不可观测，见文件末的未证实）。
"""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Gpu, GpuHost, GpuStatus, Workspace
from app.services.scheduler import GpuInfo, GpuPoolContendedError, GpuScheduler
from app.utils import utcnow

VISIBLE_HOST = "h-visible"
HIDDEN_HOST = "h-hidden"
SMALL_MIB = 24564  # 24 GB 档：够 20 GB 的需求，不够 40 GB
BIG_MIB = 49152  # 48 GB 档：只有它够 40 GB
THRESHOLD = 600


@pytest.fixture()
def factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'alloc.db'}", future=True)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, future=True)
    engine.dispose()


def _sync(sf: sessionmaker, host_id: str, gpu_uuid: str, memory_total: int) -> None:
    with sf() as db:
        GpuScheduler(sf).sync_host(
            db,
            host_id=host_id,
            name=f"name-{host_id}",
            address="10.0.0.1",
            provider="docker",
            gpus=[GpuInfo(gpu_uuid=gpu_uuid, model="m", memory_total=memory_total, index=0)],
        )


def _seed(sf: sessionmaker) -> None:
    """两台节点各一张卡；然后把 h-hidden 的心跳改旧，让它由**真判决**变成 offline。"""
    _sync(sf, VISIBLE_HOST, "gpu-visible", SMALL_MIB)
    _sync(sf, HIDDEN_HOST, "gpu-hidden", BIG_MIB)
    _backdate(sf, HIDDEN_HOST, THRESHOLD + 30)
    with sf() as db:
        assert GpuScheduler(sf).expire_stale_hosts(db, offline_after_seconds=THRESHOLD) == 1
    with sf() as db:
        assert db.get(GpuHost, HIDDEN_HOST).status == "offline"
        assert db.get(GpuHost, VISIBLE_HOST).status == "online"


def _backdate(sf: sessionmaker, host_id: str, seconds: int) -> None:
    with sf() as db:
        db.get(GpuHost, host_id).last_synced_at = utcnow() - timedelta(seconds=seconds)
        db.commit()


def _workspace(db, wid: str) -> Workspace:
    w = Workspace(id=wid, name=wid, template_id="cartpole", provider="docker", status="queued")
    db.add(w)
    db.commit()
    return w


def _gpu_status(sf: sessionmaker, gpu_uuid: str) -> str:
    with sf() as db:
        return db.scalar(select(Gpu).where(Gpu.gpu_uuid == gpu_uuid)).status


# ---------------------------------------------------------------------------
# 失联节点上的卡不许被选中，而且不许被报成"在等锁"
# ---------------------------------------------------------------------------
def test_a_card_on_an_unseen_node_is_not_allocated(factory) -> None:
    """只有失联节点那张卡满足需求 ⇒ 报的是容量结论，不是"卡正被别人锁着"。

    两个计数面必须吃同一个谓词：只筛候选不筛 `still_waiting` 的话，这张看不见的卡
    会被数进"还在等锁"里，`GpuPoolContendedError` 让运维去等一把并不存在的锁。
    """
    _seed(factory)
    with factory() as db:
        _workspace(db, "w1")
        with pytest.raises(RuntimeError) as seen:
            GpuScheduler(factory).allocate(db, "w1", gpu_requirement_gb=40)
    message = str(seen.value)
    assert "No GPU available" in message, message
    assert not isinstance(seen.value, GpuPoolContendedError), f"失联卡被数进等锁计数：{message}"
    assert _gpu_status(factory, "gpu-hidden") == GpuStatus.AVAILABLE.value, "不许被顺手改写状态"


def test_a_card_on_a_seen_node_still_allocates(factory) -> None:
    """不开火对照：在线节点的卡照旧分配成功，选中的正是它。"""
    _seed(factory)
    with factory() as db:
        _workspace(db, "w2")
        gpu = GpuScheduler(factory).allocate(db, "w2", gpu_requirement_gb=20)
    assert gpu.gpu_uuid == "gpu-visible"
    assert gpu.workspace_id == "w2"


def test_the_card_becomes_allocatable_again_without_any_status_rewrite(factory) -> None:
    """节点重新同步 ⇒ 那张卡立刻可分配；全程没人动过 `gpus.status`（这就是可逆那一半）。"""
    _seed(factory)
    with factory() as db:
        _workspace(db, "w3")
        with pytest.raises(RuntimeError):
            GpuScheduler(factory).allocate(db, "w3", gpu_requirement_gb=40)
    assert _gpu_status(factory, "gpu-hidden") == GpuStatus.AVAILABLE.value

    _sync(factory, HIDDEN_HOST, "gpu-hidden", BIG_MIB)  # 一次成功重报即恢复在线
    with factory() as db:
        gpu = GpuScheduler(factory).allocate(db, "w3", gpu_requirement_gb=40)
    assert gpu.gpu_uuid == "gpu-hidden"
    assert _gpu_status(factory, "gpu-hidden") == GpuStatus.ALLOCATED.value


def test_a_released_card_does_not_escape_through_the_release_path(factory) -> None:
    """分配→释放之后节点才失联：下一位仍然拿不到它（收口路径不是放行口）。"""
    _seed(factory)
    with factory() as db:
        _workspace(db, "w4")
        first = GpuScheduler(factory).allocate(db, "w4", gpu_requirement_gb=20)
    scheduler = GpuScheduler(factory)
    with factory() as db:
        scheduler.release(db, "w4")
    assert first.gpu_uuid == "gpu-visible"
    _backdate(factory, VISIBLE_HOST, THRESHOLD + 30)
    with factory() as db:
        scheduler.expire_stale_hosts(db, offline_after_seconds=THRESHOLD)

    with factory() as db:
        _workspace(db, "w5")
        with pytest.raises(RuntimeError) as seen:
            scheduler.allocate(db, "w5", gpu_requirement_gb=8)
    assert not isinstance(seen.value, GpuPoolContendedError), str(seen.value)
    assert _gpu_status(factory, "gpu-visible") == GpuStatus.AVAILABLE.value


def test_visibility_is_not_a_way_back_in_for_a_hand_marked_card(factory) -> None:
    """失联节点上的 UNHEALTHY 卡：节点恢复在线也不许被这层放回来（那是两个轴）。"""
    _seed(factory)
    with factory() as db:
        scheduler = GpuScheduler(factory)
        victim = db.scalar(select(Gpu).where(Gpu.gpu_uuid == "gpu-hidden"))
        scheduler.mark_unhealthy(db, victim.id)
    _sync(factory, HIDDEN_HOST, "gpu-hidden", BIG_MIB)

    with factory() as db:
        _workspace(db, "w6")
        with pytest.raises(RuntimeError):
            GpuScheduler(factory).allocate(db, "w6", gpu_requirement_gb=40)
    assert _gpu_status(factory, "gpu-hidden") == GpuStatus.UNHEALTHY.value


def test_the_predicate_is_correlated_to_each_cards_own_host(factory) -> None:
    """谓词必须按"这张卡所在的那台节点"判，不是"场上还有没有在线节点"。

    少了关联条件，`EXISTS (SELECT 1 FROM gpu_hosts WHERE status='online')` 在任意
    一台在线时就把所有卡都算可见——这类假关联在本文件其它判据里恰好都不红
    （那些档要么只有一台节点，要么被筛的是同一种节点）。
    """
    _sync(factory, VISIBLE_HOST, "gpu-visible", SMALL_MIB)
    _sync(factory, HIDDEN_HOST, "gpu-hidden", BIG_MIB)
    _backdate(factory, HIDDEN_HOST, THRESHOLD + 30)
    with factory() as db:
        GpuScheduler(factory).expire_stale_hosts(db, offline_after_seconds=THRESHOLD)
        # 场上确实还有一台在线节点：如果谓词不相关，这张失联节点的卡就会被选中
        _workspace(db, "w7")
        with pytest.raises(RuntimeError):
            GpuScheduler(factory).allocate(db, "w7", gpu_requirement_gb=40)

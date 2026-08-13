"""GPU Scheduler：原子分配、并发不重复、unhealthy 不调度、stop/delete 释放、crash recovery。"""

import threading

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import (
    Base,
    Gpu,
    GpuAllocation,
    GpuStatus,
    OperationStatus,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from app.services.scheduler import (
    VRAM_TOLERANCE_MIB,
    GpuInfo,
    GpuScheduler,
    recover_stuck_gpu_allocations,
    recover_stuck_workspaces,
)

ENGINE = create_engine("sqlite:///./test-scheduler.db", connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


def _setup_two_gpus() -> GpuScheduler:
    scheduler = GpuScheduler(Factory)
    with Factory() as db:
        scheduler.sync_host(
            db,
            host_id="h1",
            name="host-1",
            address="127.0.0.1",
            provider="mock",
            gpus=[
                GpuInfo(gpu_uuid="gpu-1", model="RTX A6000", memory_total=49152, index=0),
                GpuInfo(gpu_uuid="gpu-2", model="RTX A5000", memory_total=24564, index=1),
            ],
        )
    return scheduler


def _workspace(db, wid: str) -> Workspace:
    w = Workspace(id=wid, name="ws", template_id="cartpole", provider="mock", status="queued")
    db.add(w)
    db.commit()
    return w


def test_allocate_then_release_returns_to_available():
    scheduler = _setup_two_gpus()
    with Factory() as db:
        _workspace(db, "w1")
        gpu = scheduler.allocate(db, "w1", gpu_requirement_gb=8)
        assert gpu.status == GpuStatus.ALLOCATED.value
        assert gpu.workspace_id == "w1"
        assert gpu.gpu_uuid == "gpu-2"  # 按 memory 升序优先小显存（24GB < 48GB）
        alloc = db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == "w1"))
        assert alloc is not None and alloc.released_at is None

        scheduler.release(db, "w1")
        gpu = db.get(Gpu, gpu.id)
        assert gpu.status == GpuStatus.AVAILABLE.value
        assert gpu.workspace_id is None
        # 分配绑定记录被删除（运行时绑定非审计账本）
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == "w1")) is None

        # 释放后可再次分配
        gpu2 = scheduler.allocate(db, "w1", gpu_requirement_gb=8)
        assert gpu2.status == GpuStatus.ALLOCATED.value


def test_concurrent_allocation_no_duplicates():
    """并发分配同一池：每张 GPU 至多被一个 workspace 占用，且不重复分配。"""
    scheduler = _setup_two_gpus()
    results: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(i: int):
        with Factory() as db:
            wid = f"w-{i}"
            _workspace(db, wid)
            try:
                gpu = scheduler.allocate(db, wid, gpu_requirement_gb=8)
                with lock:
                    results.append(f"{wid}:{gpu.id}")
            except RuntimeError as exc:
                with lock:
                    errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) >= 2  # 至少有 2 个成功（2 张 GPU）
    # 无重复 GPU 分配
    gpu_ids = [r.split(":")[1] for r in results]
    assert len(gpu_ids) == len(set(gpu_ids))
    # 成功数不超过 GPU 数
    assert len(results) <= 2
    # 失败者得到明确错误
    assert len(errors) == 8 - len(results)


def test_unhealthy_gpu_not_scheduled():
    scheduler = _setup_two_gpus()
    with Factory() as db:
        gpu1 = db.scalar(select(Gpu).where(Gpu.gpu_uuid == "gpu-1"))
        scheduler.mark_unhealthy(db, gpu1.id)
        _workspace(db, "wu")
        gpu = scheduler.allocate(db, "wu", gpu_requirement_gb=8)
        assert gpu.gpu_uuid == "gpu-2"  # unhealthy 的 gpu-1 未被分配


def test_draining_gpu_not_scheduled():
    scheduler = _setup_two_gpus()
    with Factory() as db:
        gpu1 = db.scalar(select(Gpu).where(Gpu.gpu_uuid == "gpu-1"))
        scheduler.mark_draining(db, gpu1.id)
        _workspace(db, "wd")
        gpu = scheduler.allocate(db, "wd", gpu_requirement_gb=8)
        assert gpu.gpu_uuid == "gpu-2"


def test_insufficient_vram_raises():
    scheduler = _setup_two_gpus()
    with Factory() as db:
        _workspace(db, "wv")
        with pytest.raises(RuntimeError, match="No GPU available"):
            scheduler.allocate(db, "wv", gpu_requirement_gb=64)  # 256GB 需求，池内最大 48GB


def test_crash_recovery_releases_terminal_orphan_allocation():
    scheduler = _setup_two_gpus()
    with Factory() as db:
        _workspace(db, "w-orphan")
        gpu = scheduler.allocate(db, "w-orphan", gpu_requirement_gb=8)
        assert db.get(Gpu, gpu.id).status == GpuStatus.ALLOCATED.value

        # 模拟崩溃：workspace 已终态（FAILED）且无 active operation，GPU 仍绑定 → 真正孤儿
        ws = db.get(Workspace, "w-orphan")
        ws.status = WorkspaceStatus.FAILED.value
        db.commit()

        recover_stuck_gpu_allocations(db)
        gpu = db.get(Gpu, gpu.id)
        assert gpu.status == GpuStatus.AVAILABLE.value
        assert gpu.workspace_id is None
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == "w-orphan")) is None


def test_recover_stuck_workspaces_finds_queued_and_provisioning():
    _setup_two_gpus()
    with Factory() as db:
        _workspace(db, "w-queued")  # 默认 queued
        db.add(
            Workspace(
                id="w-prov", name="ws2", template_id="cartpole", provider="mock",
                status=WorkspaceStatus.PROVISIONING.value,
            )
        )
        db.commit()
        assert set(recover_stuck_workspaces(db)) == {"w-queued", "w-prov"}


@pytest.mark.parametrize(
    "status", [WorkspaceStatus.PROVISIONING.value, WorkspaceStatus.STOPPING.value]
)
def test_recover_preserves_nonterminal_allocations(status):
    """PROVISIONING/STOPPING 是进行中的生命周期：持有 GpuAllocation 时不得被误释放。"""
    scheduler = _setup_two_gpus()
    with Factory() as db:
        _workspace(db, "w-keep")
        gpu = scheduler.allocate(db, "w-keep", gpu_requirement_gb=8)
        ws = db.get(Workspace, "w-keep")
        ws.status = status
        db.commit()

        recover_stuck_gpu_allocations(db)
        gpu = db.get(Gpu, gpu.id)
        assert gpu.status == GpuStatus.ALLOCATED.value
        assert gpu.workspace_id == "w-keep"
        alloc = db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == "w-keep"))
        assert alloc is not None and alloc.released_at is None


def test_recover_releases_queued_orphan_allocation():
    """QUEUED 阶段尚未分配 GPU，持卡即孤儿 → 必须释放（无 active op）。"""
    scheduler = _setup_two_gpus()
    with Factory() as db:
        _workspace(db, "w-queued-orphan")  # 默认 queued
        gpu = scheduler.allocate(db, "w-queued-orphan", gpu_requirement_gb=8)
        assert db.get(Gpu, gpu.id).status == GpuStatus.ALLOCATED.value

        recover_stuck_gpu_allocations(db)
        gpu = db.get(Gpu, gpu.id)
        assert gpu.status == GpuStatus.AVAILABLE.value
        assert gpu.workspace_id is None
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == "w-queued-orphan")) is None


def test_recover_preserves_workspace_with_active_operation():
    """终态 workspace 但存在 active operation（如 DESTROY 重试中）：分配必须保留。"""
    scheduler = _setup_two_gpus()
    with Factory() as db:
        _workspace(db, "w-op")
        gpu = scheduler.allocate(db, "w-op", gpu_requirement_gb=8)
        ws = db.get(Workspace, "w-op")
        ws.status = WorkspaceStatus.FAILED.value
        db.add(
            WorkspaceOperation(
                id="op-1",
                workspace_id="w-op",
                operation_type="destroy",
                status=OperationStatus.RUNNING.value,
            )
        )
        db.commit()

        recover_stuck_gpu_allocations(db)
        gpu = db.get(Gpu, gpu.id)
        assert gpu.status == GpuStatus.ALLOCATED.value
        alloc = db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == "w-op"))
        assert alloc is not None and alloc.released_at is None


def test_recover_releases_orphan_allocation_without_workspace():
    """无对应 workspace 行的孤儿分配：必须释放。"""
    _setup_two_gpus()
    with Factory() as db:
        gpu = db.scalar(select(Gpu).where(Gpu.gpu_uuid == "gpu-1"))
        db.add(
            GpuAllocation(
                id="alloc-ghost", gpu_id=gpu.id, workspace_id="ghost-ws", host_id=gpu.host_id
            )
        )
        db.commit()

        recover_stuck_gpu_allocations(db)
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.id == "alloc-ghost")) is None


def _setup_single_gpu(memory_total: int) -> GpuScheduler:
    scheduler = GpuScheduler(Factory)
    with Factory() as db:
        scheduler.sync_host(
            db,
            host_id="h1",
            name="host-1",
            address="127.0.0.1",
            provider="mock",
            gpus=[GpuInfo(gpu_uuid=f"gpu-{memory_total}", model="RTX", memory_total=memory_total, index=0)],
        )
    return scheduler


def test_vram_24gb_card_satisfies_24gb_requirement():
    """24564 MiB（24GB 卡实际可用显存）应满足 24GB 模板需求。"""
    scheduler = _setup_single_gpu(24564)
    with Factory() as db:
        _workspace(db, "w24")
        gpu = scheduler.allocate(db, "w24", gpu_requirement_gb=24)
        assert gpu.memory_total == 24564


def test_vram_boundary_exact_threshold_passes():
    """等于容差阈值（24GB → 24576 - 16 = 24560 MiB）应满足。"""
    threshold = 24 * 1024 - VRAM_TOLERANCE_MIB
    scheduler = _setup_single_gpu(threshold)
    with Factory() as db:
        _workspace(db, "wb1")
        gpu = scheduler.allocate(db, "wb1", gpu_requirement_gb=24)
        assert gpu.memory_total == threshold


def test_vram_boundary_one_mib_below_fails():
    """差 1 MiB（阈值 - 1）不满足 24GB。"""
    threshold = 24 * 1024 - VRAM_TOLERANCE_MIB
    scheduler = _setup_single_gpu(threshold - 1)
    with Factory() as db:
        _workspace(db, "wb2")
        with pytest.raises(RuntimeError, match="No GPU available"):
            scheduler.allocate(db, "wb2", gpu_requirement_gb=24)

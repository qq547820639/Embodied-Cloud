"""共享测试库里的 GPU 池前置条件。

全套用例共用一个 SQLite 库（`tests/conftest.py`），而 mock 主机只 seed 8 张虚拟卡。
`POST /api/workspaces` 会走 provision 并占住一张卡，**测试结束后没人回收**——所以
"跑到后半程卡被占干"是这套夹具的固有风险，不是被测系统的问题。实测读数：把
`tests/test_gpu_admin.py` 与一个新建 13 个 workspace 的测试文件配成一对跑，前者即红，
报错是 provision 重试三次后 `No GPU available with >= 8 GB VRAM`（不是断言失败）；
其余 55 个测试文件逐个配上去都不红。

结论：需要空闲卡的用例必须**显式声明并达成**这个前置条件，而不是靠"排在前面"。
回收走 `GpuScheduler.release`（本仓唯一的分配权威），不手写 UPDATE。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Gpu, GpuStatus, Workspace, WorkspaceStatus
from app.services.scheduler import GpuScheduler

#: `Gpu.memory_total` 单位是 MiB（`allocate` 把模板的 GB 需求乘 1024 再比较）。
MIB = 1024
#: 回收时顺手改状态的 workspace 状态：这些卡的占用者已经不会再自己释放了。
LIVE_STATUSES = (
    WorkspaceStatus.CREATED.value,
    WorkspaceStatus.QUEUED.value,
    WorkspaceStatus.PROVISIONING.value,
    WorkspaceStatus.RUNNING.value,
    WorkspaceStatus.STOPPING.value,
)


def allocated_workspaces(db: Session) -> list[Workspace]:
    return list(
        db.scalars(
            select(Workspace).where(
                Workspace.id.in_(
                    select(Gpu.workspace_id).where(
                        Gpu.status == GpuStatus.ALLOCATED.value,
                        Gpu.workspace_id.is_not(None),
                    )
                )
            )
        )
    )


def reclaim_gpus(db: Session, scheduler: GpuScheduler) -> int:
    """释放所有 ALLOCATED 的卡，返回释放张数。

    只动 ALLOCATED：UNHEALTHY / DRAINING 是别的用例**故意**设出来的状态，
    回收它们会把那些用例的前提抹掉。
    """
    released = 0
    for workspace in allocated_workspaces(db):
        scheduler.release(db, workspace.id)
        if workspace.status in LIVE_STATUSES:
            workspace.status = WorkspaceStatus.STOPPED.value
            db.commit()
        released += 1
    return released


def count_big_enough(db: Session, requirement_gb: int) -> int:
    """够用的空闲卡数（按 mock 的 MiB 口径比，不扣厂商预留容差——判据从严）。"""
    return len(
        list(
            db.scalars(
                select(Gpu).where(
                    Gpu.status == GpuStatus.AVAILABLE.value,
                    Gpu.memory_total >= requirement_gb * MIB,
                )
            )
        )
    )


def ensure_free_gpus(db: Session, scheduler: GpuScheduler, need: int = 1, requirement_gb: int = 8) -> int:
    """达成"至少 `need` 张够用的空闲卡"这个前置条件，返回达成后的空闲数。"""
    if count_big_enough(db, requirement_gb) < need:
        reclaim_gpus(db, scheduler)
    free = count_big_enough(db, requirement_gb)
    assert free >= need, (
        f"前置条件不成立：需要 ≥{need} 张 ≥{requirement_gb}GB 空闲卡，"
        f"回收后仍只有 {free} 张（mock 池被 seed 成固定大小？还是 UNHEALTHY/DRAINING 被设太多）"
    )
    return free

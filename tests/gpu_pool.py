"""共享测试库里的 GPU 池前置条件。

全套用例共用一个 SQLite 库（`tests/conftest.py`），而 mock 主机只 seed 8 张虚拟卡。
`POST /api/workspaces` 会走 provision 并占住一张卡，**测试结束后没人回收**——所以
"跑到后半程卡被占干"是这套夹具的固有风险，不是被测系统的问题。实测读数：把
`tests/test_gpu_admin.py` 与一个新建 13 个 workspace 的测试文件配成一对跑，前者即红，
报错是 provision 重试三次后 `No GPU available with >= 8 GB VRAM`（不是断言失败）；
其余 55 个测试文件逐个配上去都不红。

结论：需要空闲卡的用例必须**显式声明并达成**这个前置条件，而不是靠"排在前面"。
回收走 `GpuScheduler.release`（本仓唯一的分配权威），不手写 UPDATE。

前置条件按**净额**承诺，因为"AVAILABLE 有几张"会撒谎：队列里每个还没被执行掉的
provision op 都是一张**已经在别人预算里的卡**——`enqueue_operation` 只写一行
PENDING（`app/services/worker.py:35`），拿到卡是下一次 `worker.tick_once()` 的事。
共用一库里没人保证由谁来 tick：实测 `tests/test_workspace_progress.py` 单跑结束时
`gpu 总=8 可用=6 分配行=2`，两支用例各占一张卡——正例的 tick 顺手把同模块前一支
（`_queued_workspace` 故意留成未执行的 queued op）也推进了，于是那一支也吃掉一张。
该模块只声明 `ensure_free_gpus(need=1)`：原始空闲正好 1 张时它宣布"满足"，紧接着
自己的 provision 就报 `No GPU available with >= 8 GB VRAM (workspace ... attempts=3)`。
所以这里判的是 `AVAILABLE 张数 − 未执行 provision op 数 ≥ need`，欠款由
`unfulfilled_provision_ops` 数；不这么改的话，每个消费方都得自己手抄一遍这套算术
（`tests/test_gpu_pool_guard.py` 的 `rig` 就是这么写死 ≥2 张的）。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Gpu,
    GpuStatus,
    OperationStatus,
    OperationType,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from app.services.scheduler import GpuScheduler, health_is_usable

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


#: 非终态 operation：`enqueue_operation` 写 PENDING，worker claim 后 RUNNING，
#: 可重试失败落 RETRYING（枚举见 `app/models.py:97-102`）。这三种都可能被下一次
#: tick 执行掉，而 `models.py:480,511` 保证同一 workspace 至多一个 active op，
#: 所以"未执行的 provision op 数"就是"还会被 provision 吃掉的卡数"的上界。
UNFULFILLED_OPERATION_STATUSES = (
    OperationStatus.PENDING.value,
    OperationStatus.RUNNING.value,
    OperationStatus.RETRYING.value,
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

    只动 ALLOCATED：UNHEALTHY / DRAINED 是管理员判决、DRAINING 是别人故意设出来的缺席档，
    回收它们会把那些用例的前提抹掉（N-123 之后生产代码的 `release` 同样只把占用中的卡还池）。
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
    """够用的空闲卡数（按 mock 的 MiB 口径比，不扣厂商预留容差——判据从严）。

    必须与分配器吃同一批谓词（N-122 的规矩，ADR 0010 之后多一条 `health_is_usable()`）：
    健康自 N-126 起不在 `status` 里，只数 AVAILABLE 会把一张被判不健康的卡算成余量，
    而 `allocate` 拒发它 ⇒ 夹具宣布"净额够"而真请求拿不到卡（N-85 那一族的复发形状）。
    """
    return len(
        list(
            db.scalars(
                select(Gpu).where(
                    Gpu.status == GpuStatus.AVAILABLE.value,
                    health_is_usable(),
                    Gpu.memory_total >= requirement_gb * MIB,
                )
            )
        )
    )


def unfulfilled_provision_ops(db: Session) -> int:
    """队列里"已入队、还没被执行"的 provision op 数＝空闲卡的**隐性欠款**（张数）。

    为什么需要它：`count_big_enough` 只看 AVAILABLE，而那些 op 一旦被人 tick 就会各占
    一张卡。谁去 tick 不由入队方决定——共用一库里，别的用例的 `worker.tick_once()`
    （或它自己 lifespan 起的后台线程）会把它们一起排掉。实测
    `tests/test_workspace_progress.py` 单跑结束时 `gpu 总=8 可用=6 分配行=2`：
    该模块只承诺 need=1，却真占了两张。原始空闲正好 1 张时旧判据宣布"满足"，
    正例随即 `attempts=3` 落 terminal failed。
    """
    return int(
        db.scalar(
            select(func.count())
            .select_from(WorkspaceOperation)
            .where(
                WorkspaceOperation.operation_type == OperationType.PROVISION.value,
                WorkspaceOperation.status.in_(UNFULFILLED_OPERATION_STATUSES),
            )
        )
        or 0
    )


def ensure_free_gpus(db: Session, scheduler: GpuScheduler, need: int = 1, requirement_gb: int = 8) -> int:
    """达成"扣掉队列欠款后仍有至少 `need` 张够用的空闲卡"，返回达成后的**原始**空闲数。

    出参口径是 `count_big_enough` 的原始读数（不含扣减），调用方按"现在有几张 AVAILABLE"
    读它——`tests/test_gpu_pool_guard.py::test_ensure_free_gpus_leaves_drained_cards_alone`
    的等值断言钉的就是这个口径。承诺的是净额：`出参 − unfulfilled_provision_ops ≥ need`。
    """
    if count_big_enough(db, requirement_gb) - unfulfilled_provision_ops(db) < need:
        reclaim_gpus(db, scheduler)
    free = count_big_enough(db, requirement_gb)
    debt = unfulfilled_provision_ops(db)
    assert free - debt >= need, (
        f"前置条件不成立：需要净额 ≥{need} 张 ≥{requirement_gb}GB 空闲卡，"
        f"回收后原始空闲 {free} 张、另有未执行 provision op {debt} 个（净额 {free - debt}）。"
        "欠款不是有人偷了卡：它们是别的用例入队、等下一次 tick 替它们吃卡的 op。"
        "原始空闲本身就少＝池被 seed 成固定大小或有卡停在放不掉的状态"
        "（UNHEALTHY/DRAINING 被设太多）；原始空闲够而净额不足＝欠款侧没人回收"
    )
    return free

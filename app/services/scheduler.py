"""GPU Scheduler：inventory 同步、原子分配/释放、crash recovery 支撑。

并发安全设计（见 docs/ARCHITECTURE.md §3）：
- 分配：`SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`（PostgreSQL 生效；SQLite 为
  no-op）—— 每轮只锁一行候选，候选被别人锁住时短暂退避重试；
  `gpu_allocations.gpu_id/workspace_id` 唯一约束兜底 —— 并发下至多一个事务插入成功。
- 释放：幂等；stop/delete 后 GPU 回 AVAILABLE。
- UNHEALTHY 不参与调度；DRAINING 不再分配新 workspace。
"""

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Gpu, GpuAllocation, GpuHost, GpuStatus, Workspace, WorkspaceStatus
from ..utils import utcnow

# nvidia-smi `--query-gpu=memory.total` 上报单位为 MiB；标称「N GB」的卡实际可用
# MiB 常略低于 N*1024（如 24GB 卡上报 24564 MiB，比 24576 少 12 MiB）。分配时
# 给需求 MiB 留一个小容差，避免「24GB 卡无法满足 24GB 模板」的误判。
VRAM_TOLERANCE_MIB = 16

# `gpu_hosts.status` 是自由文本列（不是 GpuStatus 那样的枚举），这两个值就是它的全部
# 词表：写在哪里、由谁改写，都只在本模块里发生。
HOST_ONLINE = "online"
HOST_OFFLINE = "offline"


@dataclass
class GpuInfo:
    gpu_uuid: str
    model: str
    memory_total: int
    index: int = 0


# PostgreSQL/psycopg 的唯一约束冲突 SQLSTATE
_UNIQUE_VIOLATION = "23505"


class GpuPoolContendedError(RuntimeError):
    """池子里**有**满足需求的空闲卡，只是每一张都被并发事务锁住，且在重试预算内没等到。

    与"真的没有卡"必须分开报：前者是可重试的等待（也是"卡其实空闲"这一事实的唯一记录），
    后者是容量结论。两者共用一句话时，运维看到"无卡可用"、用户看到工作区一路重试到
    FAILED，而桌上还摆着空闲卡——本轮在真 PostgreSQL 行锁上把这个形状量了出来
    （tests/test_postgres_concurrency.py::test_allocate_retry_window_...）。
    """


def candidate_order() -> list:
    """候选排序就是分配策略本身：默认 best-fit —— 先用刚好够用的卡，把大卡留给大任务。

    单列成一个函数（而不是把 ORDER BY 埋在 SQL 里）是为了能被实测对照：
    `tests/test_scheduler_policy.py` 逐条换掉它、跑同一份工作负载与同一个分配器，
    量出这个选择到底值多少张卡。埋在语句里的策略没人知道它比替代品好在哪。
    """
    return [Gpu.memory_total.asc()]


def _is_unique_contention(exc: IntegrityError) -> bool:
    """这次 IntegrityError 是"卡被别人抢了"（唯一冲突），还是我们自己写坏了数据？

    给 gpu_allocations 加外键之后，两类冲突都会落进同一个 except 分支：
    唯一冲突 = 并发竞争（该重试下一张卡）；外键冲突 = 往分配表写了不存在的
    workspace/host（重试多少次都不会成功，且会被误报成"没有空闲卡"）。
    先按驱动给的 SQLSTATE 判，退回到 SQLite 的约束文案。
    """
    orig = getattr(exc, "orig", None)
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if state:
        return str(state) == _UNIQUE_VIOLATION
    return "UNIQUE constraint failed" in str(orig)

class GpuScheduler:
    # 候选正被其他事务锁住时的重试窗口（0.05+0.1+0.15+0.2 ≈ 0.5s 上限）
    ALLOCATE_MAX_ATTEMPTS = 5
    ALLOCATE_BACKOFF_SECONDS = 0.05

    def __init__(self, session_factory):
        self.session_factory = session_factory

    # ------------------------------------------------------------------
    # inventory
    # ------------------------------------------------------------------
    def sync_host(
        self,
        db: Session,
        host_id: str,
        name: str,
        address: str,
        provider: str,
        gpus: list[GpuInfo],
    ) -> GpuHost:
        """幂等 upsert host + gpus；消失的 GPU 标记 DRAINING（不再分配）。

        每次成功的同步都顺手给 `last_synced_at` 盖章：`status="online"` 是结论，
        这一列才是它的证据，`expire_stale_hosts` 只按证据改判。
        """
        seen_at = utcnow()
        host = db.get(GpuHost, host_id)
        if host is None:
            host = GpuHost(
                id=host_id,
                name=name,
                address=address,
                provider=provider,
                status=HOST_ONLINE,
                last_synced_at=seen_at,
            )
            db.add(host)
        else:
            host.address = address
            host.provider = provider
            host.status = HOST_ONLINE
            host.last_synced_at = seen_at
        db.flush()

        seen: set[str] = set()
        for info in gpus:
            seen.add(info.gpu_uuid)
            gpu = db.scalar(select(Gpu).where(Gpu.gpu_uuid == info.gpu_uuid))
            if gpu is None:
                db.add(
                    Gpu(
                        id=str(uuid.uuid4()),
                        gpu_uuid=info.gpu_uuid,
                        host_id=host_id,
                        model=info.model,
                        memory_total=info.memory_total,
                        gpu_index=info.index,
                        status=GpuStatus.AVAILABLE.value,
                    )
                )
            else:
                gpu.model = info.model
                gpu.memory_total = info.memory_total
                gpu.gpu_index = info.index
        # 未再上报的 GPU → DRAINING（保留历史分配记录，但不再新分配）
        for stale in db.scalars(select(Gpu).where(Gpu.host_id == host_id)):
            if stale.gpu_uuid not in seen and stale.status in {
                GpuStatus.AVAILABLE.value,
                GpuStatus.DRAINING.value,
            }:
                stale.status = GpuStatus.DRAINING.value
        db.commit()
        return host

    # ------------------------------------------------------------------
    # allocate / release
    # ------------------------------------------------------------------
    def allocate(self, db: Session, workspace_id: str, gpu_requirement_gb: int = 0) -> Gpu:
        """原子分配一张满足显存需求的 GPU；失败抛 RuntimeError。

        返回已标记 ALLOCATED 的 Gpu（同一 db 会话内）。

        一次只锁**一行**候选（`FOR UPDATE SKIP LOCKED` + `LIMIT 1`）。不加 limit
        会把整批候选一起锁住：并发启动时后来者扫到 0 行，会在卡其实空闲的情况下被
        误判成「无卡可用」——这个缺陷只在真行锁的 PostgreSQL 上看得见（SQLite 下
        FOR UPDATE 是 no-op），由 tests/test_postgres_concurrency.py 钉住。
        因此「本轮没锁到」不等于「没卡」：仍有 AVAILABLE 行时短暂退避后重试。
        """
        # 统一单位：模板需求 GB → MiB（1 GiB = 1024 MiB），再扣掉厂商预留容差
        required_mib = gpu_requirement_gb * 1024 - VRAM_TOLERANCE_MIB
        waiting = 0  # 最后一次看到的"空闲但被别人锁着"的候选数
        for attempt in range(self.ALLOCATE_MAX_ATTEMPTS):
            gpu = db.scalar(
                select(Gpu)
                .where(
                    Gpu.status == GpuStatus.AVAILABLE.value,
                    Gpu.memory_total >= required_mib,
                )
                .order_by(*candidate_order())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if gpu is not None:
                gpu.status = GpuStatus.ALLOCATED.value
                gpu.workspace_id = workspace_id
                gpu.updated_at = utcnow()
                db.add(
                    GpuAllocation(
                        id=str(uuid.uuid4()),
                        gpu_id=gpu.id,
                        workspace_id=workspace_id,
                        host_id=gpu.host_id,
                    )
                )
                try:
                    db.commit()
                except IntegrityError as exc:
                    # 只有唯一约束冲突才是"另一路已占用这张卡"（并发竞争，换下一张重试）；
                    # 外键冲突说明我们自己在往 gpu_allocations 写不存在的 workspace/host，
                    # 一律重试会把数据缺陷报成"没有空闲卡"。
                    contention = _is_unique_contention(exc)
                    db.rollback()
                    if not contention:
                        raise RuntimeError(
                            f"GPU 分配被完整性约束拒绝（非并发争用）: {exc}"
                        ) from exc
                    continue
                return gpu
            # 没锁到候选：区分「真没卡」与「卡正被别人锁着」
            still_waiting = db.scalar(
                select(func.count())
                .select_from(Gpu)
                .where(Gpu.status == GpuStatus.AVAILABLE.value, Gpu.memory_total >= required_mib)
            )
            if not still_waiting:
                break
            waiting = int(still_waiting)
            time.sleep(self.ALLOCATE_BACKOFF_SECONDS * (attempt + 1))
        if waiting:
            budget = self.ALLOCATE_BACKOFF_SECONDS * sum(
                range(1, self.ALLOCATE_MAX_ATTEMPTS)
            )
            raise GpuPoolContendedError(
                f"{waiting} 张 >= {gpu_requirement_gb} GB 的卡处于 AVAILABLE，但都被并发事务锁住；"
                f"重试预算 {budget:.2f}s 内没有等到（不是容量不足）(workspace {workspace_id[:8]})"
            )
        raise RuntimeError(
            f"No GPU available with >= {gpu_requirement_gb} GB VRAM "
            f"(workspace {workspace_id[:8]})"
        )

    def release(self, db: Session, workspace_id: str) -> None:
        """幂等释放：GPU 回 AVAILABLE、清 workspace_id、删除运行时分配绑定记录，
        并把那一格上残留的 `gpu_id/gpu_index/gpu_name` 一起清掉（N-86）。

        本函数是唯一的分配权威，所以"放卡"这件事在这里是**三件成对**而不是两件：
        卡回池、分配行删除、格子不再声称持有它。只写前两件的话，读者
        （`schemas.py` 的 `WorkspaceOut` → API/前端、`warmpool.py` 的 unbooked inflight 计数）
        拿到的是"这格还占着那张已易主的卡"，而这类漂移回收器看不见——它既没有分配行、
        `Gpu.workspace_id` 也不再指向那格。调用方原先各自手写清列（`_fail` 那一处保留，
        它还要兜住 release 自己抛错的情形），现在由这里统一兜住。

        删除而非软标记：gpu_allocations.gpu_id 唯一约束用于"同一 GPU 至多一个
        活动分配"，保留已释放行会阻塞重新分配；分配记录不是审计账本
        （审计走 credit_ledger），运行时绑定删除是安全的。
        """
        now = utcnow()
        allocs = db.scalars(
            select(GpuAllocation).where(
                GpuAllocation.workspace_id == workspace_id, GpuAllocation.released_at.is_(None)
            )
        ).all()
        for alloc in allocs:
            alloc.released_at = now
            db.delete(alloc)
        # 兼容：直接挂在 Gpu.workspace_id 的孤儿绑定也清理
        db.execute(
            update(Gpu)
            .where(Gpu.workspace_id == workspace_id)
            .values(status=GpuStatus.AVAILABLE.value, workspace_id=None, updated_at=now)
        )
        # 成对的第二半：这张卡回池了，那一格就不许再声称持有它。走 ORM 而不是批量
        # UPDATE —— 调用方（`_finalize_stop`、warm pool 的撤销档）手上正拿着同一个实例，
        # 批量 UPDATE 会让身份图里的旧值活过本次提交，读者拿到的仍是"我持有那张卡"。
        # 格子不存在（ghost 分配）时这里就是 no-op，与上面两条 UPDATE 的边界一致。
        holder = db.get(Workspace, workspace_id)
        if holder is not None:
            holder.gpu_id = None
            holder.gpu_index = None
            holder.gpu_name = None
        db.commit()

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def expire_stale_hosts(
        self, db: Session, *, offline_after_seconds: int, now: datetime | None = None
    ) -> int:
        """把「上次同步已过期」的 host 从 online 改判 offline，返回改了几台。

        判据只看证据列，不看结论列：`status == online` 且 `last_synced_at` 早于阈值。
        `last_synced_at IS NULL`（本列上线后还没同步过的存量行）**一律不动**——
        从没同步过不等于同步失败，把它判成 offline 与把它留在 online 一样是编造。

        这里不碰调度：`allocate` 的权威是 `gpus.status`，host 的 `status` 只有一处
        读者（admin 的 `GET /api/gpus/hosts` 与前端表格）。本函数的职责就是把那一处
        读数变成真话——运维不该看到一台早已离开集群的节点仍然"在线"。
        """
        moment = now or utcnow()
        threshold = moment - timedelta(seconds=offline_after_seconds)
        stale = db.scalars(
            select(GpuHost).where(
                GpuHost.status == HOST_ONLINE,
                GpuHost.last_synced_at.is_not(None),
                GpuHost.last_synced_at < threshold,
            )
        ).all()
        for host in stale:
            host.status = HOST_OFFLINE
        if stale:
            db.commit()
        return len(stale)

    def mark_unhealthy(self, db: Session, gpu_id: str) -> None:
        gpu = db.get(Gpu, gpu_id)
        if gpu is not None:
            gpu.status = GpuStatus.UNHEALTHY.value
            gpu.updated_at = utcnow()
            db.commit()

    def mark_draining(self, db: Session, gpu_id: str) -> None:
        gpu = db.get(Gpu, gpu_id)
        if gpu is not None and gpu.status == GpuStatus.AVAILABLE.value:
            gpu.status = GpuStatus.DRAINING.value
            gpu.updated_at = utcnow()
            db.commit()

    def list_gpus(self, db: Session) -> list[Gpu]:
        return list(db.scalars(select(Gpu).order_by(Gpu.host_id, Gpu.gpu_uuid)))


def recover_stuck_gpu_allocations(db: Session) -> None:
    """释放所有非占用 workspace 的孤儿 GPU 绑定，并让两张权威表在同一事务里一起改口。

    占用判据（避免误释放进行中生命周期，防止同一物理 GPU 被二次分配 → 一卡双跑）：
    - workspace 状态属于非终态 {PROVISIONING, RUNNING, STOPPING}
      （QUEUED 阶段尚未进入分配环节，GPU 分配只发生在 PROVISIONING 阶段，
       QUEUED 却持有分配属于孤儿残留，故不保护；PROVISIONING/STOPPING 的
       runtime 状态未知，保守保留）
    - 或存在 active operation（workspace_operations.status ∈ {PENDING, RUNNING, RETRYING}）
    其余（workspace 不存在、终态 workspace 且无 active operation）的 GPU 绑定
    视为孤儿，予以释放。

    释放的落点是两张表，不是一张：`gpus` 说"这张卡没人了"，`workspaces.gpu_id /
    gpu_index / gpu_name` 说"这一格不持有卡"。只改前者会留下"卡已易主、格子还在
    对外声称持有那张卡"的漂移 —— `WorkspaceOrchestrator.reconcile_all` 的节点不一致
    分支正是按 `w.gpu_id` 反查预留 host 的，卡若已转授他人，它拿去比的就是别人的节点。
    本函数是漂移的收口者：它判定为孤儿并放掉的那几格，列一并清掉；占用/受保护的格
    一列都不动（那是 `_fail` / `_finalize_stop` / warm pool 各自准入路径的责任，
    本函数不越界替它们清）。Workspace 模型没有 updated_at 列，故此处无时间戳可刷。
    """
    from ..models import OperationStatus, Workspace, WorkspaceOperation

    occupied_ids = set(
        db.scalars(
            select(Workspace.id).where(
                Workspace.status.in_(
                    [
                        WorkspaceStatus.PROVISIONING.value,
                        WorkspaceStatus.RUNNING.value,
                        WorkspaceStatus.STOPPING.value,
                    ]
                )
            )
        )
    )
    # active operation 的 workspace 也视为占用（终态 workspace 仍可能有 DESTROY/STOP 重试中）
    occupied_ids |= set(
        db.scalars(
            select(WorkspaceOperation.workspace_id).where(
                WorkspaceOperation.status.in_(
                    [
                        OperationStatus.PENDING.value,
                        OperationStatus.RUNNING.value,
                        OperationStatus.RETRYING.value,
                    ]
                )
            )
        )
    )
    # 被本函数判定为孤儿并放了卡的格：卡与列必须成对处理，缺一即漂移
    orphan_ids: set[str] = set()
    allocs = db.scalars(
        select(GpuAllocation).where(GpuAllocation.released_at.is_(None))
    ).all()
    for alloc in allocs:
        if alloc.workspace_id not in occupied_ids:
            # 记名要在 delete 之前：实例进入 DELETED 态后读属性不再可靠
            orphan_ids.add(alloc.workspace_id)
            alloc.released_at = utcnow()
            db.delete(alloc)
    # 归还所有非占用 workspace 的 GPU 绑定（occupied_ids 为空时清空所有绑定）
    # 每个放卡分支各自记名：先读出"这次被清掉归属的是哪几格"再放行 —— UPDATE 之后无从回看。
    if occupied_ids:
        bound_ids = db.scalars(
            select(Gpu.workspace_id).where(
                Gpu.workspace_id.is_not(None), Gpu.workspace_id.notin_(occupied_ids)
            )
        ).all()
        orphan_ids.update(bid for bid in bound_ids if bid is not None)
        db.execute(
            update(Gpu)
            .where(Gpu.workspace_id.notin_(occupied_ids))
            .values(status=GpuStatus.AVAILABLE.value, workspace_id=None, updated_at=utcnow())
        )
    else:
        bound_ids = db.scalars(
            select(Gpu.workspace_id).where(Gpu.workspace_id.is_not(None))
        ).all()
        orphan_ids.update(bid for bid in bound_ids if bid is not None)
        db.execute(
            update(Gpu)
            .where(Gpu.workspace_id.is_not(None))
            .values(status=GpuStatus.AVAILABLE.value, workspace_id=None, updated_at=utcnow())
        )
    # 成对的第二半：这些格不得再声称持有已被强制放掉的卡。范围只由 orphan_ids 决定，
    # 受保护/占用的格即使留着 gpu_id 也不在本语句的 WHERE 里。必须在 commit 之前 ——
    # 卡与列两次提交之间存在一个"卡已放、列还指着它"的窗口，崩在那儿就是新漂移。
    if orphan_ids:
        db.execute(
            update(Workspace)
            .where(Workspace.id.in_(sorted(orphan_ids)))
            .values(gpu_id=None, gpu_index=None, gpu_name=None)
        )
    db.commit()

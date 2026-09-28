"""GPU Scheduler：inventory 同步、原子分配/释放、crash recovery 支撑。

并发安全设计（见 docs/ARCHITECTURE.md §3）：
- 分配：`SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`（PostgreSQL 生效；SQLite 为
  no-op）—— 每轮只锁一行候选，候选被别人锁住时短暂退避重试；
  `gpu_allocations.gpu_id/workspace_id` 唯一约束兜底 —— 并发下至多一个事务插入成功。
- 释放：幂等；stop/delete 后占用中的 GPU 回 AVAILABLE，但带着管理员下架意图
  （`gpus.drain_requested_at` 非空）的占用卡落成 DRAINED；已有的 DRAINED 只清绑定、
  不改状态列——自动路径不替管理员撤判决（N-123／N-125）。
- DRAINED 不参与调度也不自动归位；DRAINING（缺席降级）重新被上报到即归位。健康判决不住在
  状态列里（N-126／ADR 0010）：它由 `gpus.health` 说，分配候选用 `health_is_usable()` 拒掉。
"""

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement

from ..models import (
    Gpu,
    GpuAllocation,
    GpuHealth,
    GpuHost,
    GpuStatus,
    Workspace,
    WorkspaceStatus,
)
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


def host_is_visible() -> ColumnElement[bool]:
    """这张卡所在的节点，今天还看得见吗（N-112，闭 N-111）。

    复用 N-110 那条证据，不新写第二份：`gpu_hosts.status` 由最近一次同步背书，
    所以"看不见节点"与"不该往那儿派卡"本来就是同一件事的两半。

    刻意**不改** `gpus.status`：
    - 管理员手工写下的判决（`/drain` 的意图列、`/unhealthy` 的健康列）有各自的
      语义与生命周期，
      一个周期任务顺手把它们抬回 AVAILABLE 是不可接受的（重报每 2 min 一次）；
    - 而"节点暂时失联"必须能随一次成功重报自动恢复。读证据列同时满足两边：
      节点回到 online 的下一趟分配就看得到那张卡，全程没有人改写过卡的状态。
    """
    return (
        select(GpuHost.id)
        .where(GpuHost.id == Gpu.host_id, GpuHost.status == HOST_ONLINE)
        .exists()
    )


def health_is_usable() -> ColumnElement[bool]:
    """这张卡的健康判决允许用它吗（ADR 0010，闭登记项 N-126）。

    `gpus.health` 是 nullable 证据列：NULL＝从没人判过 ⇒ 照用；`healthy`＝说过健康 ⇒ 照用；
    只有亲口判下的 `unhealthy` 才挡分配。健康不再挤在 `gpus.status` 里，所以一张卡可以
    **既在用又不健康**，两个事实同时为真而不互相抹掉（改前 `mark_unhealthy` 覆盖 allocated，
    对外的 `gpu_allocated` 当场少一张，而格上的归属还在）。

    与 `host_is_visible()` 同一条规矩：**只有一份**。候选筛选、等锁计数（`still_waiting`）
    与 warm pool 的余量都吃它——写第二份就会漂，N-122 量的正是漂的代价。
    """
    return or_(Gpu.health.is_(None), Gpu.health != GpuHealth.UNHEALTHY.value)


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
        """幂等 upsert host + gpus；消失的 GPU 标记 DRAINING（不再分配），重新上报即归位。

        两个方向都只碰机器判决：缺席降级只作用于 AVAILABLE／DRAINING，归位只把 DRAINING
        抬回 AVAILABLE。管理员写下的 DRAINED 与 UNHEALTHY 在这条路上既不解除也不覆盖——
        这条不变量由 `tests/test_gpu_drain_provenance.py` 的登记册判据钉住（N-123）。

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
                # 归位只作用于机器判决：DRAINING 的含义就是"上一次上报里没有它"，这一次有了，
                # 那句判决便不再成立。DRAINED／UNHEALTHY 是人的判决，重报既不解除也不覆盖
                # （N-114 (b)：把两件事分成两个值，才敢让自动路径动手改状态列）。
                if gpu.status == GpuStatus.DRAINING.value:
                    gpu.status = GpuStatus.AVAILABLE.value
                    gpu.updated_at = seen_at
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

        候选与"还在等锁的那把尺"必须吃同一个可见性谓词（`host_is_visible()`，N-112）：
        只筛候选不筛计数的话，一张"节点已失联"的卡会被 `still_waiting` 数进去，
        于是分配失败被报成「N 张卡正被别人锁着」——那是把容量结论说成了等待，
        运维会去等锁，而该做的是去看节点。
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
                    host_is_visible(),
                    health_is_usable(),
                )
                .order_by(*candidate_order())
                # `of=[Gpu]`：可见性判断不许把节点那一行也锁上。为什么留着它——本轮在真
                # PostgreSQL 上量过三档（证人在 tests/test_postgres_concurrency.py::
                # test_allocate_leaves_the_host_row_lock_free）：
                # ① EXISTS ＋ OF：主机行仍能被别的会话锁走（正常）；
                # ② EXISTS 摘掉 OF：也锁不到——PG 不会为一个 WHERE 子查询里的表加行锁；
                # ③ 把这条 EXISTS 改写成 `.join(GpuHost, ...)` 且没有 OF：主机行**会被锁**，
                #    于是同一台机器上的多张卡每次分配都在同一行上排队。
                # 留着 OF 就是为了 ③：「顺手把这个 EXISTS 改成 join」是一次正常重构。
                # 出处（本轮打开 sql-select 页读过）："A locking clause without a table
                # list affects all tables used in the statement."
                .with_for_update(skip_locked=True, of=[Gpu])
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
            # 没锁到候选：区分「真没卡」与「卡正被别人锁着」——两把尺同一个谓词
            still_waiting = db.scalar(
                select(func.count())
                .select_from(Gpu)
                .where(
                    Gpu.status == GpuStatus.AVAILABLE.value,
                    Gpu.memory_total >= required_mib,
                    host_is_visible(),
                    health_is_usable(),
                )
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
        # 状态列按"这张卡原来是什么判决"分叉：占用还回池子；占用中但挂着管理员下架意图的卡
        # 落成 drained（意图是人的判决，释放只是把它兑现，不是替管理员撤判决，N-125）；
        # 已有的 drained 原样留着，只清绑定（健康判决不住在状态列里，这一脚碰不到它，N-126）。
        db.execute(
            update(Gpu)
            .where(Gpu.workspace_id == workspace_id)
            .values(
                status=case(
                    (
                        and_(
                            Gpu.status == GpuStatus.ALLOCATED.value,
                            Gpu.drain_requested_at.is_not(None),
                        ),
                        GpuStatus.DRAINED.value,
                    ),
                    (Gpu.status == GpuStatus.ALLOCATED.value, GpuStatus.AVAILABLE.value),
                    else_=Gpu.status,
                ),
                workspace_id=None,
                updated_at=now,
            )
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

        这里不碰调度，也不碰 `gpus.status`：改卡的状态会把管理员的判决一起顶掉（见
        `host_is_visible` 的说明）。但**别把"host 的 status 只有一处读者"当成事实**——
        这句在本仓一度成立，N-112 之后就不成立了：`allocate` 的候选与等锁计数（`:202`／`:251`）
        都经由 `host_is_visible()`（`:81`）读它，本轮 N-122 又多了 warm pool 的容量闸门
        （`app/services/warmpool.py:211`）。今天它的读者是「一台可见性判定」，
        运维看到的 `GET /api/gpus/hosts` 只是其中最后一处。
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

    def mark_unhealthy(self, db: Session, gpu_id: str) -> bool:
        """判这张卡不健康——只写 `health`，**不碰 `status`**（ADR 0010）。

        改前它把 `status` 直接改写成 unhealthy：一张正在被用的卡被判掉之后，
        `gpus.status == allocated` 这个本仓唯一的占用载体当场消失（对外的
        `gpu_allocated` 少一张，`app/main.py:34-37`），而 `gpus.workspace_id` 与
        那一格的 `workspaces.gpu_id` 都还指着对方——两张表互相打脸，且没有任何判据会红。
        现在两个维度各自成立：占用照旧是 allocated，健康照旧是 unhealthy，分配器靠
        `health_is_usable()` 挡派工。返回值是「这张卡现在是否带着 unhealthy 判决」。
        """
        gpu = db.get(Gpu, gpu_id)
        if gpu is None:
            return False
        if gpu.health != GpuHealth.UNHEALTHY.value:
            gpu.health = GpuHealth.UNHEALTHY.value
            gpu.updated_at = utcnow()
            db.commit()
        return True

    def mark_healthy(self, db: Session, gpu_id: str) -> bool:
        """解除健康判决（管理员动作，与 `/undrain` 同形）。

        只写 `health`：这张卡该回到 available 还是继续 draining／drained，由那两个维度
        自己的所有者决定（下一次成功上报／管理员的 `/undrain`），这条路不替它们改口。
        """
        gpu = db.get(Gpu, gpu_id)
        if gpu is None:
            return False
        if gpu.health is not None:
            gpu.health = None
            gpu.updated_at = utcnow()
            db.commit()
        return True

    def mark_drained(self, db: Session, gpu_id: str) -> bool:
        """管理员要求这张卡离开池子——占用中也判得动（N-125，闭登记项 N-124）。

        写的是意图证据列 `gpus.drain_requested_at`，占用事实不动：`status == allocated`
        是这张卡在 `gpus` 一侧唯一的占用载体（对外的 `gpu_allocated` 指标就数它，
        `app/main.py:34-37`；`workspaces.gpu_id` 那半边仍在，覆盖它就会让两张表各说各话）。
        池子里的卡（available／被缺席降级过的 draining）当场
        判成 drained；正在被用的卡保持 allocated，等 `release` 那一脚按意图把它落成 drained
        ——K8s 的 `spec.unschedulable` 与 SLURM 的 `state=drain` 都是这个分工。
        返回值是「这张卡现在是否带着管理员意图」，False 只可能是卡不存在。N-123 那版
        用 409 拒绝占用中的卡，是因为当时没有第二列可以说"意图已经登记"。
        """
        gpu = db.get(Gpu, gpu_id)
        if gpu is None:
            return False
        pool_states = {GpuStatus.AVAILABLE.value, GpuStatus.DRAINING.value}
        if gpu.drain_requested_at is None or gpu.status in pool_states:
            now = utcnow()
            gpu.drain_requested_at = now
            if gpu.status in pool_states:
                gpu.status = GpuStatus.DRAINED.value
            gpu.updated_at = now
            db.commit()
        return True

    def mark_undrained(self, db: Session, gpu_id: str) -> bool:
        """解除管理员的下架意图；只有带着这一意图的卡会被改动。

        状态归位只处理管理员自己判下去的那一档：`drained → available`。
        `draining`（机器判决）与 `unhealthy`（健康判决）不由这条路径撤销——它们各有
        自己的所有者（分别是下一次成功上报与管理员的 `/unhealthy`）。
        返回值是「这张卡现在是否已无下架意图」，False 只可能是卡不存在。
        """
        gpu = db.get(Gpu, gpu_id)
        if gpu is None:
            return False
        if gpu.drain_requested_at is not None or gpu.status == GpuStatus.DRAINED.value:
            gpu.drain_requested_at = None
            if gpu.status == GpuStatus.DRAINED.value:
                gpu.status = GpuStatus.AVAILABLE.value
            gpu.updated_at = utcnow()
            db.commit()
        return True

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
            .values(
                status=case(
                    (
                        and_(
                            Gpu.status == GpuStatus.ALLOCATED.value,
                            Gpu.drain_requested_at.is_not(None),
                        ),
                        GpuStatus.DRAINED.value,
                    ),
                    (Gpu.status == GpuStatus.ALLOCATED.value, GpuStatus.AVAILABLE.value),
                    else_=Gpu.status,
                ),
                workspace_id=None,
                updated_at=utcnow(),
            )
        )
    else:
        bound_ids = db.scalars(
            select(Gpu.workspace_id).where(Gpu.workspace_id.is_not(None))
        ).all()
        orphan_ids.update(bid for bid in bound_ids if bid is not None)
        db.execute(
            update(Gpu)
            .where(Gpu.workspace_id.is_not(None))
            .values(
                status=case(
                    (
                        and_(
                            Gpu.status == GpuStatus.ALLOCATED.value,
                            Gpu.drain_requested_at.is_not(None),
                        ),
                        GpuStatus.DRAINED.value,
                    ),
                    (Gpu.status == GpuStatus.ALLOCATED.value, GpuStatus.AVAILABLE.value),
                    else_=Gpu.status,
                ),
                workspace_id=None,
                updated_at=utcnow(),
            )
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

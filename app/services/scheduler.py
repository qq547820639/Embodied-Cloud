"""GPU Scheduler：inventory 同步、原子分配/释放、crash recovery 支撑。

并发安全设计（见 docs/ARCHITECTURE.md §3）：
- 分配：`SELECT ... FOR UPDATE`（PostgreSQL 生效；SQLite 为 no-op）+
  `gpu_allocations.workspace_id` 唯一约束兜底 —— 并发下至多一个事务插入成功。
- 释放：幂等；stop/delete 后 GPU 回 AVAILABLE。
- UNHEALTHY 不参与调度；DRAINING 不再分配新 workspace。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Gpu, GpuAllocation, GpuHost, GpuStatus, WorkspaceStatus


def utcnow() -> datetime:
    return datetime.now(UTC)


# nvidia-smi `--query-gpu=memory.total` 上报单位为 MiB；标称「N GB」的卡实际可用
# MiB 常略低于 N*1024（如 24GB 卡上报 24564 MiB，比 24576 少 12 MiB）。分配时
# 给需求 MiB 留一个小容差，避免「24GB 卡无法满足 24GB 模板」的误判。
VRAM_TOLERANCE_MIB = 16


@dataclass
class GpuInfo:
    gpu_uuid: str
    model: str
    memory_total: int
    index: int = 0

class GpuScheduler:
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
        """幂等 upsert host + gpus；消失的 GPU 标记 DRAINING（不再分配）。"""
        host = db.get(GpuHost, host_id)
        if host is None:
            host = GpuHost(id=host_id, name=name, address=address, provider=provider)
            db.add(host)
        else:
            host.address = address
            host.provider = provider
            host.status = "online"
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
        """
        # 统一单位：模板需求 GB → MiB（1 GiB = 1024 MiB），再扣掉厂商预留容差
        required_mib = gpu_requirement_gb * 1024 - VRAM_TOLERANCE_MIB
        candidates = (
            db.scalars(
                select(Gpu)
                .where(
                    Gpu.status == GpuStatus.AVAILABLE.value,
                    Gpu.memory_total >= required_mib,
                )
                .order_by(Gpu.memory_total.asc())
                .with_for_update(skip_locked=True)
            )
        ).all()
        for gpu in candidates:
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
            except IntegrityError:
                # 并发竞争：另一事务已占用该 GPU → 回滚重试下一张
                db.rollback()
                continue
            return gpu
        raise RuntimeError(
            f"No GPU available with >= {gpu_requirement_gb} GB VRAM "
            f"(workspace {workspace_id[:8]})"
        )

    def release(self, db: Session, workspace_id: str) -> None:
        """幂等释放：GPU 回 AVAILABLE、清 workspace_id、删除运行时分配绑定记录。

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
        db.commit()

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # crash recovery
    # ------------------------------------------------------------------
    def release_all_for_workspaces(self, db: Session, workspace_ids: list[str]) -> None:
        """恢复期调用：释放一批 workspace 的全部 GPU 绑定（幂等）。"""
        for wid in workspace_ids:
            self.release(db, wid)


def recover_stuck_workspaces(db: Session) -> list[str]:
    """返回应标记 FAILED 的悬置 workspace id 列表（QUEUED/PROVISIONING）。"""
    from ..models import Workspace

    stuck = db.scalars(
        select(Workspace).where(
            Workspace.status.in_([WorkspaceStatus.QUEUED.value, WorkspaceStatus.PROVISIONING.value])
        )
    ).all()
    return [w.id for w in stuck]


def recover_stuck_gpu_allocations(db: Session) -> None:
    """释放所有非占用 workspace 的孤儿 GPU 绑定（幂等）。

    占用判据（避免误释放进行中生命周期，防止同一物理 GPU 被二次分配 → 一卡双跑）：
    - workspace 状态属于非终态 {QUEUED, PROVISIONING, RUNNING, STOPPING}
    - 或存在 active operation（workspace_operations.status ∈ {PENDING, RUNNING, RETRYING}）
    其余（workspace 不存在、终态 workspace 且无 active operation）的 GPU 绑定
    视为孤儿，予以释放。
    """
    from ..models import OperationStatus, Workspace, WorkspaceOperation

    occupied_ids = set(
        db.scalars(
            select(Workspace.id).where(
                Workspace.status.in_(
                    [
                        WorkspaceStatus.QUEUED.value,
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
    allocs = db.scalars(
        select(GpuAllocation).where(GpuAllocation.released_at.is_(None))
    ).all()
    for alloc in allocs:
        if alloc.workspace_id not in occupied_ids:
            alloc.released_at = utcnow()
            db.delete(alloc)
    # 归还所有非占用 workspace 的 GPU 绑定（occupied_ids 为空时清空所有绑定）
    if occupied_ids:
        db.execute(
            update(Gpu)
            .where(Gpu.workspace_id.notin_(occupied_ids))
            .values(status=GpuStatus.AVAILABLE.value, workspace_id=None, updated_at=utcnow())
        )
    else:
        db.execute(
            update(Gpu)
            .where(Gpu.workspace_id.is_not(None))
            .values(status=GpuStatus.AVAILABLE.value, workspace_id=None, updated_at=utcnow())
        )
    db.commit()

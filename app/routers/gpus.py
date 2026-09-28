"""GPU inventory（admin）。"""

from fastapi import APIRouter, HTTPException

from ..deps import DB, CurrentUser, scheduler
from ..models import Gpu, GpuHost, Role
from ..schemas import GpuHostOut, GpuOut

router = APIRouter(prefix="/gpus", tags=["gpus"])


def _admin(user) -> None:
    if user.role != Role.ADMIN.value:
        raise HTTPException(403, "admin role required")


@router.get("", response_model=list[GpuOut])
def list_gpus(db: DB, user: CurrentUser):
    _admin(user)
    return scheduler.list_gpus(db)


@router.get("/hosts", response_model=list[GpuHostOut])
def list_hosts(db: DB, user: CurrentUser):
    _admin(user)
    from sqlalchemy import select

    return list(db.scalars(select(GpuHost).order_by(GpuHost.name)))


@router.post("/{gpu_id}/unhealthy", status_code=204)
def mark_unhealthy(gpu_id: str, db: DB, user: CurrentUser):
    _admin(user)
    if db.get(Gpu, gpu_id) is None:
        raise HTTPException(404, "gpu not found")
    scheduler.mark_unhealthy(db, gpu_id)


@router.post("/{gpu_id}/drain", status_code=204)
def mark_drained(gpu_id: str, db: DB, user: CurrentUser):
    """管理员要求这张卡离开池子；只有管理员能解除（重新上报不会把它抬回来）。

    占用中的卡也判得动（N-125，闭登记项 N-124）：这一脚写的是意图证据列
    `gpus.drain_requested_at`，`status` 仍是 allocated——占用事实在 `gpus` 一侧只有这一个
    载体（对外的 `gpu_allocated` 指标就数它，`app/main.py:34-37`），抹掉它就留下一格仍
    声称持有卡、指标却把这张卡当空着的对账裂缝；等这一次占用结束（`release`）
    意图被兑现成 `drained`。N-123 那版用 409 拒绝占用中的卡，当时没有第二列可以说
    "意图已登记"，现在有了。204 仍然意味着主张为真：读端能拿到这一列（`GET /api/gpus`）。
    """
    _admin(user)
    if not scheduler.mark_drained(db, gpu_id):
        raise HTTPException(404, "gpu not found")


@router.post("/{gpu_id}/undrain", status_code=204)
def mark_undrained(gpu_id: str, db: DB, user: CurrentUser):
    """撤回下架意图（K8s 的 `uncordon`）：drained 回 available，占用中的意图一并清除。

    这一格必须存在：N-125 之后 `/drain` 会落在一张**正在被使用**的卡上，若没有撤回路径，
    一次误点就是一张永久掉的卡，只能改库。`unhealthy` 与缺席降级（`draining`）不在这里撤——
    它们各有自己的所有者（管理员的 `/unhealthy` 与下一次成功上报）。
    """
    _admin(user)
    if not scheduler.mark_undrained(db, gpu_id):
        raise HTTPException(404, "gpu not found")

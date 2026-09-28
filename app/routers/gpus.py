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
    """管理员把这张卡从池子里摘出去；只有管理员能解除（重新上报不会把它抬回来）。

    204 只意味着"这张卡现在确实是 drained"。占用中的卡不改写状态列（`allocated` 是本仓的
    占用权威），所以这里回 409 并点名当前状态——改前后置不满足也回 204，是「没报错即成功」
    那一族（N-109／N-113 同族）。
    """
    _admin(user)
    gpu = db.get(Gpu, gpu_id)
    if gpu is None:
        raise HTTPException(404, "gpu not found")
    if not scheduler.mark_drained(db, gpu_id):
        raise HTTPException(409, f"gpu {gpu_id} is {gpu.status}; drain 只作用于池子里的卡")

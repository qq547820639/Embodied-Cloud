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
def mark_draining(gpu_id: str, db: DB, user: CurrentUser):
    _admin(user)
    if db.get(Gpu, gpu_id) is None:
        raise HTTPException(404, "gpu not found")
    scheduler.mark_draining(db, gpu_id)

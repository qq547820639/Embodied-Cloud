"""Streaming 路由: 会话生命周期 + warm pool 观测/基准。

越权按 SECURITY.md T1 返回 404 (不泄露资源存在性); 服务校验失败返回 400 + 中文信息。
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ..deps import DB, CurrentUser, streaming_service, warm_pool
from ..models import Role
from ..schemas import StreamingOut
from ..services.warmpool import BENCHMARK_DEFAULT_ITERATIONS, BENCHMARK_MAX_ITERATIONS

router = APIRouter(prefix="/streaming", tags=["streaming"])


# ---------------------------------------------------------------------------
# 会话生命周期
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_id}/connect", response_model=StreamingOut)
def connect_session(session_id: str, db: DB, user: CurrentUser):
    try:
        return streaming_service.connect(db, session_id, user)
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sessions/{session_id}/disconnect", response_model=StreamingOut)
def disconnect_session(session_id: str, db: DB, user: CurrentUser):
    try:
        return streaming_service.disconnect(db, session_id, user)
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sessions/{session_id}/reconnect", response_model=StreamingOut)
def reconnect_session(session_id: str, db: DB, user: CurrentUser):
    try:
        return streaming_service.reconnect(db, session_id, user)
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{workspace_id}/start", response_model=StreamingOut, status_code=202)
def start_streaming(workspace_id: str, db: DB, user: CurrentUser):
    try:
        return streaming_service.start(db, workspace_id, user)
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/workspace/{workspace_id}", response_model=list[StreamingOut])
def list_streaming_sessions(workspace_id: str, db: DB, user: CurrentUser):
    try:
        return streaming_service.list_for_workspace(db, workspace_id, user)
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc


# ---------------------------------------------------------------------------
# Warm pool 观测 / 基准
#
# 两端都是 admin-only：metrics 是**跨全部模板**的池水位（普通用户不该看见别人的容量），
# benchmark 更直接——它每轮真的 create+start 一个 workspace。改造前两端只有 CurrentUser
# （实测普通用户拿到 200，body 里是 5 个模板的水位），且 iterations 没有上限。
# 角色检查沿用仓内既有惯例（app/routers/gpus.py:13、usage.py:18），不另造一套依赖。
# ---------------------------------------------------------------------------


def _admin(user) -> None:
    if user.role != Role.ADMIN.value:
        raise HTTPException(403, "admin role required")


@router.get("/warmpool/metrics")
def warmpool_metrics(db: DB, user: CurrentUser):
    """warm pool 观测：按 template × state 的 COUNT(*) 真实计数（admin）。"""
    _admin(user)
    return warm_pool.pool_metrics(db)


@router.get("/warmpool/benchmark")
def warmpool_benchmark(
    template_id: str,
    db: DB,
    user: CurrentUser,
    iterations: Annotated[int, Query(ge=1, le=BENCHMARK_MAX_ITERATIONS)] = (
        BENCHMARK_DEFAULT_ITERATIONS
    ),
):
    """冷启动基准（admin）：建了就要收，所以迭代数封顶在 warmpool 里那一份定义上。"""
    _admin(user)
    try:
        return warm_pool.benchmark_launch(db, template_id, iterations)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

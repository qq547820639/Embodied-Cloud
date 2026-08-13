"""Streaming 路由: 会话生命周期 + warm pool 观测/基准。

越权按 SECURITY.md T1 返回 404 (不泄露资源存在性); 服务校验失败返回 400 + 中文信息。
"""

from fastapi import APIRouter, HTTPException

from ..deps import DB, CurrentUser, streaming_service, warm_pool
from ..schemas import StreamingOut

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
# ---------------------------------------------------------------------------


@router.get("/warmpool/metrics")
def warmpool_metrics(db: DB, user: CurrentUser):
    return warm_pool.metrics(db)


@router.get("/warmpool/benchmark")
def warmpool_benchmark(template_id: str, db: DB, user: CurrentUser, iterations: int = 3):
    try:
        return warm_pool.benchmark_launch(db, template_id, iterations)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

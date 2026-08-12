"""Streaming 路由: 会话生命周期 + warm pool 观测/基准。

越权按 SECURITY.md T1 返回 404 (不泄露资源存在性); 服务校验失败返回 400 + 中文信息。
"""

from fastapi import APIRouter, HTTPException

from ..deps import DB, CurrentUser, SessionFactory, orchestrator, settings
from ..schemas import StreamingOut
from ..services.streaming import StreamingSessionService
from ..services.warmpool import WarmPoolManager

router = APIRouter(prefix="/streaming", tags=["streaming"])

_service = StreamingSessionService(SessionFactory)
_warmpool = WarmPoolManager(SessionFactory, orchestrator, settings)


# ---------------------------------------------------------------------------
# 会话生命周期
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_id}/connect", response_model=StreamingOut)
def connect_session(session_id: str, db: DB, user: CurrentUser):
    try:
        return _service.connect(db, session_id, user)
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sessions/{session_id}/disconnect", response_model=StreamingOut)
def disconnect_session(session_id: str, db: DB, user: CurrentUser):
    try:
        return _service.disconnect(db, session_id, user)
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sessions/{session_id}/reconnect", response_model=StreamingOut)
def reconnect_session(session_id: str, db: DB, user: CurrentUser):
    try:
        return _service.reconnect(db, session_id, user)
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{workspace_id}/start", response_model=StreamingOut, status_code=202)
def start_streaming(workspace_id: str, db: DB, user: CurrentUser):
    try:
        return _service.start(db, workspace_id, user)
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/workspace/{workspace_id}", response_model=list[StreamingOut])
def list_streaming_sessions(workspace_id: str, db: DB, user: CurrentUser):
    try:
        return _service.list_for_workspace(db, workspace_id, user)
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc


# ---------------------------------------------------------------------------
# Warm pool 观测 / 基准
# ---------------------------------------------------------------------------


@router.get("/warmpool/metrics")
def warmpool_metrics(db: DB, user: CurrentUser):
    return _warmpool.metrics(db)


@router.get("/warmpool/benchmark")
def warmpool_benchmark(template_id: str, db: DB, user: CurrentUser, iterations: int = 3):
    try:
        return _warmpool.benchmark_launch(db, template_id, iterations)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

"""Workspaces —— 全量 owner/org 隔离。

越权访问一律 404（不泄露资源存在性，见 SECURITY.md T1）。
"""


from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..deps import DB, CurrentUser, billing, credential_cipher, orchestrator, warm_pool, worker
from ..models import OperationType
from ..models import Role, Template, Workspace, WorkspaceStatus
from ..schemas import WorkspaceAccessOut, WorkspaceCreate, WorkspaceOut
from ..services.billing import BillingError

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _get_owned(db, workspace_id: str, user) -> Workspace:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(404, "workspace not found")
    if workspace.deleted_at is not None:
        # soft delete：普通 API 一律 404（管理员审计走 /admin/all）
        raise HTTPException(404, "workspace not found")
    if user.role != Role.ADMIN.value and workspace.user_id != user.id:
        raise HTTPException(404, "workspace not found")
    return workspace


@router.get("", response_model=list[WorkspaceOut])
def list_workspaces(db: DB, user: CurrentUser):
    stmt = (
        select(Workspace)
        .where(Workspace.deleted_at.is_(None))
        .order_by(Workspace.created_at.desc())
    )
    if user.role != Role.ADMIN.value:
        stmt = stmt.where(Workspace.user_id == user.id)
    return list(db.scalars(stmt))


@router.post("", response_model=WorkspaceOut, status_code=201)
def create_workspace(payload: WorkspaceCreate, db: DB, user: CurrentUser):
    template = db.get(Template, payload.template_id)
    if template is None or not template.enabled:
        raise HTTPException(404, "template not found")
    # BillingPolicy：launch 前额度/配额门禁（402 明确拒绝，不给 FAILED workspace）
    try:
        billing.check_launch_eligible(db, user, template)
    except BillingError as exc:
        raise HTTPException(402, str(exc)) from exc
    # §6：WarmPool claim 真实产品路径 —— Launch → claim(template)
    # 成功 → 直接返回已绑定用户的 claimed workspace；无 READY → fallback 正常 provision
    if payload.auto_start:
        claimed = warm_pool.claim(db, template.id, user, credential_cipher=credential_cipher)
        if claimed is not None:
            return claimed
    workspace = orchestrator.create(
        db,
        template,
        payload.name,
        user_id=user.id,
        organization_id=user.organization_id,
    )
    if payload.auto_start:
        orchestrator.start_async(workspace.id)
    return workspace


@router.get("/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(workspace_id: str, db: DB, user: CurrentUser):
    return _get_owned(db, workspace_id, user)


@router.get("/{workspace_id}/access", response_model=WorkspaceAccessOut)
def get_workspace_access(workspace_id: str, db: DB, user: CurrentUser):
    workspace = _get_owned(db, workspace_id, user)
    # §13：DB 存加密凭据；此处解密后仅返回给合法 owner（明文不落日志）。
    # fail closed：enc: 密文解密失败 → 500，绝不把密文当密码返回。
    from ..security import CredentialDecryptError

    try:
        password = credential_cipher.resolve(workspace.password)
    except CredentialDecryptError as exc:
        raise HTTPException(500, str(exc)) from exc
    return WorkspaceAccessOut(
        workspace_id=workspace.id,
        status=workspace.status,
        ide_url=workspace.ide_url,
        ide_password=password,
        stream_hint=workspace.stream_hint,
        signal_port=workspace.signal_port,
        media_port=workspace.media_port,
    )


@router.post("/{workspace_id}/start", response_model=WorkspaceOut)
def start_workspace(workspace_id: str, db: DB, user: CurrentUser):
    workspace = _get_owned(db, workspace_id, user)
    if workspace.status in {WorkspaceStatus.PROVISIONING.value, WorkspaceStatus.RUNNING.value}:
        return workspace
    workspace.status = WorkspaceStatus.QUEUED.value
    workspace.error_message = None
    db.commit()
    orchestrator.start_async(workspace.id)
    db.refresh(workspace)
    return workspace


@router.post("/{workspace_id}/stop", response_model=WorkspaceOut)
def stop_workspace(workspace_id: str, db: DB, user: CurrentUser):
    """§12：durable STOP —— enqueue STOP operation；worker 执行（fast-path 同步
    处理，任务持久化：控制面重启后 cleanup 不丢失）。"""
    workspace = _get_owned(db, workspace_id, user)
    op = worker.enqueue(workspace.id, OperationType.STOP)
    if op is not None:
        worker.tick_once()  # fast-path：立即执行（operation 已持久化）
    db.refresh(workspace)
    return workspace


@router.get("/{workspace_id}/logs", response_model=dict)
def workspace_logs(workspace_id: str, db: DB, user: CurrentUser, tail: int = 200):
    """runtime 日志（owner 隔离；provider.logs，无 runtime 时返回空）。"""
    workspace = _get_owned(db, workspace_id, user)
    tail = max(1, min(tail, 2000))
    logs = orchestrator.provider.logs(workspace, tail=tail)
    return {"workspace_id": workspace.id, "logs": logs or ""}


@router.delete("/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: str, db: DB, user: CurrentUser):
    """§12：durable DESTROY —— enqueue DESTROY operation；worker 执行
    （幂等 tombstone；重启不丢清理）。"""
    workspace = _get_owned(db, workspace_id, user)
    op = worker.enqueue(workspace.id, OperationType.DESTROY)
    if op is not None:
        worker.tick_once()  # fast-path：立即执行（operation 已持久化）


# ---------------------------------------------------------------------------
# 管理（admin）
# ---------------------------------------------------------------------------


@router.get("/admin/all", response_model=list[WorkspaceOut], include_in_schema=False)
def list_all_workspaces(db: DB, user: CurrentUser):
    """管理员审计：包含 deleted（tombstone）workspace。"""
    if user.role != Role.ADMIN.value:
        raise HTTPException(403, "admin role required")
    return list(db.scalars(select(Workspace).order_by(Workspace.created_at.desc())))

"""Deployment 端点: 创建/查询 + edge 部署流程模拟 (download→verify→run→complete).

状态机: pending → downloading → verified → running → success/failed.
owner 隔离与越权 404 语义与 workspaces router 一致 (SECURITY.md T1).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import make_engine, make_session_factory, session_dependency
from ..models import DeploymentRecord, EdgeAgent, Role, User, Workspace
from ..schemas import DeploymentCreate, DeploymentOut
from ..security import make_session_dependency
from ..services.deployment import DeploymentService

# 本地构造与本项目 deps 容器等价的依赖 (deps 模块存在既有 mypy 错误且不在本任务
# 修改范围, 故不直接导入; 运行时配置同源, SQLAlchemy 按 URL 共享连接池).
_settings = Settings()
_settings.ensure_dirs()
_session_factory = make_session_factory(make_engine(_settings))
DB = Annotated[Session, Depends(session_dependency(_session_factory))]
CurrentUser = Annotated[User, Depends(make_session_dependency(_session_factory, _settings))]

router = APIRouter(prefix="/deployments", tags=["deployments"])

deployment_service = DeploymentService(_session_factory, _settings.workspace_root)


class DeploymentRunIn(BaseModel):
    edge_agent_id: str | None = None


class DeploymentCompleteIn(BaseModel):
    success: bool
    error_message: str = ""


def _get_owned_deployment(db, user, deployment_id: str) -> DeploymentRecord:
    deployment = db.get(DeploymentRecord, deployment_id)
    if deployment is None:
        raise HTTPException(404, "deployment not found")
    workspace = db.get(Workspace, deployment.workspace_id)
    if workspace is None or (user.role != Role.ADMIN.value and workspace.user_id != user.id):
        raise HTTPException(404, "deployment not found")
    return deployment


@router.post("", response_model=DeploymentOut, status_code=201)
def create_deployment(payload: DeploymentCreate, db: DB, user: CurrentUser):
    workspace = db.get(Workspace, payload.workspace_id)
    if workspace is None or (user.role != Role.ADMIN.value and workspace.user_id != user.id):
        raise HTTPException(404, "workspace not found")
    # model_version 由 workspace 模板派生 (artifact 为准); DeploymentCreate.model_version 兼容保留
    artifact = deployment_service.create_artifact(db, user, workspace, payload.artifact_path)
    return deployment_service.deploy(db, user, workspace, artifact, payload.robot_type)


@router.get("", response_model=list[DeploymentOut])
def list_deployments(db: DB, user: CurrentUser, workspace_id: str | None = None):
    return deployment_service.list(db, user, workspace_id)


@router.get("/{deployment_id}", response_model=DeploymentOut)
def get_deployment(deployment_id: str, db: DB, user: CurrentUser):
    return _get_owned_deployment(db, user, deployment_id)


@router.post("/{deployment_id}/download", response_model=DeploymentOut)
def download_deployment(deployment_id: str, db: DB, user: CurrentUser):
    """download 只进入 DOWNLOADING（§5，P0）。

    绝不自动进入 VERIFIED：唯一 VERIFIED 路径 = Edge 下载真实 bytes →
    本地 SHA256 → POST /report-checksum 上报 → server 比较 expected。
    """
    deployment = _get_owned_deployment(db, user, deployment_id)
    deployment_service.download(db, deployment)
    return deployment


@router.post("/{deployment_id}/run", response_model=DeploymentOut)
def run_deployment(deployment_id: str, payload: DeploymentRunIn, db: DB, user: CurrentUser):
    """verify → running; body 可选 edge_agent_id 绑定执行 agent。

    §6（P0）租户校验：只能绑定**自己的** agent（admin 例外）；不能把
    deployment 派发给其他 tenant 的 agent。
    """
    deployment = _get_owned_deployment(db, user, deployment_id)
    agent = None
    if payload.edge_agent_id:
        agent = db.get(EdgeAgent, payload.edge_agent_id)
        if agent is None:
            raise HTTPException(404, "edge agent not found")
        if user.role != Role.ADMIN.value and agent.owner_user_id != user.id:
            raise HTTPException(404, "edge agent not found")
    return deployment_service.run_policy(db, deployment, agent)


@router.post("/{deployment_id}/complete", response_model=DeploymentOut)
def complete_deployment(deployment_id: str, payload: DeploymentCompleteIn, db: DB, user: CurrentUser):
    """running → success/failed."""
    deployment = _get_owned_deployment(db, user, deployment_id)
    return deployment_service.complete(db, deployment, payload.success, payload.error_message)


@router.post("/{deployment_id}/verify", response_model=DeploymentOut)
def verify_deployment(deployment_id: str, db: DB, user: CurrentUser):
    """幂等校验: artifact 当前 checksum 与记录一致 → verified, 否则 failed+error."""
    deployment = _get_owned_deployment(db, user, deployment_id)
    return deployment_service.verify_checksum(db, deployment)


class EdgeChecksumIn(BaseModel):
    actual_sha256: str


@router.post("/{deployment_id}/report-checksum", response_model=DeploymentOut)
def report_edge_checksum(
    deployment_id: str, payload: EdgeChecksumIn, db: DB, user: CurrentUser
):
    """§10：Edge 上报本地计算的 actual_sha256 —— **唯一** edge→server 校验路径。

    server 比较 actual == expected（deployment.checksum）：
    MATCH → VERIFIED；MISMATCH → FAILED。客户端**不能**直接提交 status=VERIFIED。
    """
    deployment = _get_owned_deployment(db, user, deployment_id)
    return deployment_service.report_checksum(db, deployment, payload.actual_sha256)

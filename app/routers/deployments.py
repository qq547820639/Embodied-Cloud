"""Deployment 端点: 创建/查询 + edge 部署流程模拟 (download→verify→run→complete)
+ 设备侧取件 (`GET /{id}/artifact`，§25 / ADR 0007).

状态机: pending → downloading → verified → running → success/failed.
owner 隔离与越权 404 语义与 workspaces router 一致 (SECURITY.md T1).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ..deps import DB, CurrentUser, deployment_service
from ..models import DeploymentRecord, DeploymentStatus, EdgeAgent, Role, Workspace
from ..schemas import DeploymentCreate, DeploymentOut
from ..services.artifact_store import ArtifactNotFoundError, ArtifactStoreUnavailableError
from ..services.edge import get_agent_from_header

router = APIRouter(prefix="/deployments", tags=["deployments"])


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


def agent_from_header(request: Request, db: DB) -> EdgeAgent:
    """X-Agent-Token → EdgeAgent；缺失/不匹配抛 401（见 services.edge）。"""
    return get_agent_from_header(request, db)


Agent = Annotated[EdgeAgent, Depends(agent_from_header)]


def get_deployment_for_agent(db, agent: EdgeAgent, deployment_id: str) -> DeploymentRecord:
    """Edge agent 上报用的 deployment 归属校验（§10）。

    仅校验租户所有权（agent.owner_user_id == workspace.user_id）；admin 豁免
    不适用于 agent（agent 无 role 概念）。越权一律 404，不泄露资源存在性
    （SECURITY.md T1）。
    """
    deployment = db.get(DeploymentRecord, deployment_id)
    if deployment is None:
        raise HTTPException(404, "deployment not found")
    workspace = db.get(Workspace, deployment.workspace_id)
    if workspace is None or agent.owner_user_id != workspace.user_id:
        raise HTTPException(404, "deployment not found")
    # 已绑定执行 agent（run 阶段设置 edge_agent_id）时，仅该 agent 可上报。
    if deployment.edge_agent_id is not None and deployment.edge_agent_id != agent.id:
        raise HTTPException(404, "deployment not found")
    return deployment


@router.post("", response_model=DeploymentOut, status_code=201)
def create_deployment(payload: DeploymentCreate, db: DB, user: CurrentUser):
    workspace = db.get(Workspace, payload.workspace_id)
    if workspace is None or (user.role != Role.ADMIN.value and workspace.user_id != user.id):
        raise HTTPException(404, "workspace not found")
    # model_version 由 workspace 模板派生 (artifact 为准); DeploymentCreate.model_version 兼容保留
    artifact = deployment_service.create_artifact(db, user, workspace, payload.artifact_path)
    # §25/ADR 0007：可在部署时就把设备绑上，agent 之后凭自己的 token 发现并取件。
    # 绑定与 /run 的绑定同一条规则：只能绑自己的 agent（admin 例外），越权 404。
    agent = None
    if payload.edge_agent_id:
        agent = db.get(EdgeAgent, payload.edge_agent_id)
        if agent is None:
            raise HTTPException(404, "edge agent not found")
        if user.role != Role.ADMIN.value and agent.owner_user_id != user.id:
            raise HTTPException(404, "edge agent not found")
    return deployment_service.deploy(
        db, user, workspace, artifact, payload.robot_type, edge_agent=agent
    )


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
    """幂等校验: artifact 当前 checksum 与记录一致 → verified，否则 failed+error.

    存储不可用（鉴权失败/桶不存在/网络故障）→ 503 且记录**留在 downloading**：
    重验是安全的，而 once-FAILED 是终态，不能由一次基础设施抖动判定。
    """
    deployment = _get_owned_deployment(db, user, deployment_id)
    try:
        return deployment_service.verify_checksum(db, deployment)
    except ArtifactStoreUnavailableError as exc:
        raise HTTPException(503, f"artifact store unavailable: {exc}") from exc


class EdgeChecksumIn(BaseModel):
    actual_sha256: str


@router.post("/{deployment_id}/report-checksum", response_model=DeploymentOut)
def report_edge_checksum(
    deployment_id: str, payload: EdgeChecksumIn, db: DB, agent: Agent
):
    """§10：Edge 上报本地计算的 actual_sha256 —— **唯一** edge→server 校验路径。

    上报方是 Edge agent，用 X-Agent-Token 认证（缺失/不匹配 401）。
    server 比较 actual == expected（deployment.checksum）：
    MATCH → VERIFIED；MISMATCH → FAILED。客户端**不能**直接提交 status=VERIFIED。
    """
    deployment = get_deployment_for_agent(db, agent, deployment_id)
    return deployment_service.report_checksum(db, deployment, payload.actual_sha256)


@router.get("/{deployment_id}/artifact")
def fetch_artifact_for_agent(deployment_id: str, db: DB, agent: Agent):
    """§25 / ADR 0007：agent 取件（`X-Agent-Token`），字节流 + `X-Artifact-Sha256`。

    鉴权面与 `report-checksum` 同一条：`get_deployment_for_agent` —— 租户不匹配、
    或该部署已绑给别的设备，一律 404（SECURITY.md T1 的不泄露存在性）。
    只接受 `downloading`：先 `begin`（设备侧承认"我开始取了"）再取件，与 §23 的
    "没下载不得判 VERIFIED" 用同一个状态前提。
    不下发 `Content-Disposition`：文件名由设备自己决定，响应头里不带用户可控文本。
    """
    deployment = get_deployment_for_agent(db, agent, deployment_id)
    if deployment.status != DeploymentStatus.DOWNLOADING.value:
        raise HTTPException(
            409,
            f"artifact is fetchable only while downloading, current status is {deployment.status}",
        )
    try:
        data, artifact = deployment_service.read_artifact(db, deployment)
    except ArtifactNotFoundError as exc:
        raise HTTPException(404, f"artifact object missing: {exc}") from exc
    except ArtifactStoreUnavailableError as exc:
        # 与 /verify 同一口径：存储故障不是"产物不存在"，也不落终态，503 可重试。
        raise HTTPException(503, f"artifact store unavailable: {exc}") from exc
    return Response(
        content=data,
        media_type=artifact.content_type or "application/octet-stream",
        headers={
            "X-Artifact-Sha256": artifact.checksum,
            "X-Artifact-Size": str(len(data)),
            "ETag": f'"{artifact.checksum}"',
        },
    )

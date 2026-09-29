"""Edge Agent 端点: 注册 (token 仅返回一次) + 心跳/遥测 (X-Agent-Token) + 查询
+ 设备侧工作发现与取件开门 (§25 / ADR 0007)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..deps import DB, CurrentUser, deployment_service, edge_service
from ..models import EdgeAgent
from ..schemas import DeploymentOut, EdgeAgentOut, EdgeAgentRegisterIn, EdgeHeartbeatIn, TelemetryOut
from ..services.edge import RUN_TELEMETRY_KIND, get_agent_from_header
from .deployments import get_deployment_for_agent

router = APIRouter(prefix="/edge", tags=["edge"])


class AgentRegisterOut(BaseModel):
    agent: EdgeAgentOut
    token: str


class TelemetryIn(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)


def agent_from_header(request: Request, db: DB) -> EdgeAgent:
    """X-Agent-Token → EdgeAgent; 缺失/不匹配抛 401 (见 services.edge)."""
    return get_agent_from_header(request, db)


Agent = Annotated[EdgeAgent, Depends(agent_from_header)]


@router.post("/agents/register", response_model=AgentRegisterOut, status_code=201)
def register_agent(payload: EdgeAgentRegisterIn, db: DB, user: CurrentUser):
    """§6（P0）：agent 注册必须绑定认证用户（禁止匿名无主注册）。

    安全 onboarding：authenticated owner registration（edge 侧后续可用
    short-lived pairing code 扩展）。
    """
    agent, token = edge_service.register(db, payload.name, payload.device_info, owner=user)
    return AgentRegisterOut(agent=edge_service.agent_out(db, agent), token=token)


@router.post("/agents/{agent_id}/heartbeat", response_model=EdgeAgentOut)
def heartbeat(agent_id: str, payload: EdgeHeartbeatIn, db: DB, agent: Agent):
    if agent.id != agent_id:
        raise HTTPException(404, "agent not found")
    agent = edge_service.heartbeat(db, agent, payload.device_info)
    return edge_service.agent_out(db, agent)


@router.post("/agents/{agent_id}/telemetry", response_model=TelemetryOut)
def telemetry(agent_id: str, payload: TelemetryIn, db: DB, agent: Agent):
    if agent.id != agent_id:
        raise HTTPException(404, "agent not found")
    event = edge_service.report_telemetry(db, agent, payload.kind, payload.payload)
    if payload.kind == RUN_TELEMETRY_KIND:
        # ADR 0007 的后半句「控制面据此收口」的落点：收口的是**这张卡名下的那条部署**，
        # 授权与幂等都在 `complete_from_agent_report` 的条件 UPDATE 里，不在这里判。
        deployment_service.complete_from_agent_report(db, agent, payload.payload)
    return event


@router.get("/agents/{agent_id}/deployments/assigned", response_model=list[DeploymentOut])
def assigned_deployments(agent_id: str, db: DB, agent: Agent):
    """§25 / ADR 0007：设备侧的工作发现——只返回**绑定给自己**的部署。

    发现走独立 GET 而不是塞进 heartbeat：心跳是"我还活着"的单向登记，让它顺带
    返回任务会把分派语义变成写路径（这正是 ADR 0007 列为前置问题的选项之一）。
    设备身份仍以 token 为准：`agent_id` 与 token 不自指 → 404。
    """
    if agent.id != agent_id:
        raise HTTPException(404, "agent not found")
    return deployment_service.list_assigned(db, agent)


@router.post("/agents/{agent_id}/deployments/{deployment_id}/begin", response_model=DeploymentOut)
def begin_assigned_deployment(agent_id: str, deployment_id: str, db: DB, agent: Agent):
    """设备侧承认"我开始取这件了"：pending → downloading（条件 UPDATE，重复调用幂等）。

    为什么要有这一步：`report_checksum` 只接受 DOWNLOADING（§23 防绕过），
    所以"取件"必须由设备自己开门，而不是控制面替它开门。
    """
    if agent.id != agent_id:
        raise HTTPException(404, "agent not found")
    deployment = get_deployment_for_agent(db, agent, deployment_id)
    return deployment_service.begin_agent_download(db, agent, deployment)


@router.get("/agents", response_model=list[EdgeAgentOut])
def list_agents(db: DB, user: CurrentUser):
    """租户 scope：普通用户只见自己的 agent；admin 全量。

    逐行走 `agent_out` 而不是 `model_validate`：`current_deployment_id` 是派生值，
    在这张列表面上漏算一次，读端看到的 null 就与"这台设备没在跑东西"同形。
    """
    return [edge_service.agent_out(db, a) for a in edge_service.list_agents(db, user)]


@router.get("/agents/{agent_id}", response_model=EdgeAgentOut)
def get_agent(agent_id: str, db: DB, user: CurrentUser):
    """租户 scope：越权 404（不泄露存在性）。"""
    agent = edge_service.get_agent(db, agent_id, user)
    if agent is None:
        raise HTTPException(404, "agent not found")
    return edge_service.agent_out(db, agent)


@router.get("/agents/{agent_id}/telemetry", response_model=list[TelemetryOut])
def list_telemetry(agent_id: str, db: DB, user: CurrentUser, limit: int = 50):
    """某设备的遥测回读（租户 scope：越权 404）。

    `report_telemetry` 一直在写这张表，此前没有任何读路径；设备的运行结果
    （§25 的 `edge-run`）要有用，必须能被用户/运维看见。
    """
    agent = edge_service.get_agent(db, agent_id, user)
    if agent is None:
        raise HTTPException(404, "agent not found")
    return edge_service.list_telemetry(db, agent, limit=max(1, min(limit, 200)))

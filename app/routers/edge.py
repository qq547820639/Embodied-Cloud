"""Edge Agent 端点: 注册 (token 仅返回一次) + 心跳/遥测 (X-Agent-Token) + 查询."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..deps import DB, CurrentUser, edge_service
from ..models import EdgeAgent
from ..schemas import EdgeAgentOut, EdgeAgentRegisterIn, EdgeHeartbeatIn, TelemetryOut
from ..services.edge import get_agent_from_header

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
    return AgentRegisterOut(agent=EdgeAgentOut.model_validate(agent), token=token)


@router.post("/agents/{agent_id}/heartbeat", response_model=EdgeAgentOut)
def heartbeat(agent_id: str, payload: EdgeHeartbeatIn, db: DB, agent: Agent):
    if agent.id != agent_id:
        raise HTTPException(404, "agent not found")
    return edge_service.heartbeat(db, agent, payload.device_info)


@router.post("/agents/{agent_id}/telemetry", response_model=TelemetryOut)
def telemetry(agent_id: str, payload: TelemetryIn, db: DB, agent: Agent):
    if agent.id != agent_id:
        raise HTTPException(404, "agent not found")
    return edge_service.report_telemetry(db, agent, payload.kind, payload.payload)


@router.get("/agents", response_model=list[EdgeAgentOut])
def list_agents(db: DB, user: CurrentUser):
    """租户 scope：普通用户只见自己的 agent；admin 全量。"""
    return edge_service.list_agents(db, user)


@router.get("/agents/{agent_id}", response_model=EdgeAgentOut)
def get_agent(agent_id: str, db: DB, user: CurrentUser):
    """租户 scope：越权 404（不泄露存在性）。"""
    agent = edge_service.get_agent(db, agent_id, user)
    if agent is None:
        raise HTTPException(404, "agent not found")
    return agent

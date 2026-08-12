"""Edge Agent 端点: 注册 (token 仅返回一次) + 心跳/遥测 (X-Agent-Token) + 查询."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import make_engine, make_session_factory, session_dependency
from ..models import EdgeAgent, User
from ..schemas import EdgeAgentOut, EdgeAgentRegisterIn, EdgeHeartbeatIn, TelemetryOut
from ..security import make_session_dependency
from ..services.edge import EdgeService, get_agent_from_header

# 本地构造与本项目 deps 容器等价的依赖 (deps 模块存在既有 mypy 错误且不在本任务
# 修改范围, 故不直接导入; 运行时配置同源, SQLAlchemy 按 URL 共享连接池).
_settings = Settings()
_settings.ensure_dirs()
_session_factory = make_session_factory(make_engine(_settings))
DB = Annotated[Session, Depends(session_dependency(_session_factory))]
CurrentUser = Annotated[User, Depends(make_session_dependency(_session_factory, _settings))]

router = APIRouter(prefix="/edge", tags=["edge"])

edge_service = EdgeService(_session_factory)


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
def register_agent(payload: EdgeAgentRegisterIn, db: DB):
    agent, token = edge_service.register(db, payload.name, payload.device_info)
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
    # 单租户演示控制面: 注册的 agent 用户级可见 (admin 亦可见全部)
    return edge_service.list_agents(db)


@router.get("/agents/{agent_id}", response_model=EdgeAgentOut)
def get_agent(agent_id: str, db: DB, user: CurrentUser):
    agent = edge_service.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    return agent

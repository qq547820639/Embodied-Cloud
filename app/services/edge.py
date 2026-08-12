"""EdgeService: 边缘设备注册/心跳/遥测 + X-Agent-Token 认证.

原始 token 仅 register 时返回一次; 之后边缘端通过 `X-Agent-Token` header
认证 (服务端只存 hash, 见 security.hash_token).
"""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..models import AgentStatus, EdgeAgent, Role, TelemetryEvent, User
from ..security import generate_token, hash_token


def utcnow() -> datetime:
    return datetime.now(UTC)


class EdgeService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def register(
        self,
        db: Session,
        name: str,
        device_info: dict | None = None,
        owner: User | None = None,
    ) -> tuple[EdgeAgent, str]:
        """注册边缘设备（§6，P0）：必须绑定 owner（API 层强制认证）。

        返回 (agent, 原始 token)，原始 token 仅此一次返回。
        """
        token = generate_token()
        agent = EdgeAgent(
            id=str(uuid.uuid4()),
            name=name,
            status=AgentStatus.REGISTERED.value,
            device_info=device_info or {},
            token_hash=hash_token(token),
            owner_user_id=owner.id if owner is not None else None,
            organization_id=owner.organization_id if owner is not None else None,
        )
        db.add(agent)
        db.commit()
        db.refresh(agent)
        return agent, token

    def heartbeat(self, db: Session, agent: EdgeAgent, device_info: dict | None = None) -> EdgeAgent:
        agent.status = AgentStatus.ONLINE.value
        agent.last_heartbeat = utcnow()
        if device_info:
            agent.device_info = {**agent.device_info, **device_info}
        db.commit()
        db.refresh(agent)
        return agent

    def report_telemetry(
        self, db: Session, agent: EdgeAgent, kind: str, payload: dict | None = None
    ) -> TelemetryEvent:
        event = TelemetryEvent(
            id=str(uuid.uuid4()),
            edge_agent_id=agent.id,
            kind=kind,
            payload=payload or {},
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    def list_agents(self, db: Session, user: User) -> list[EdgeAgent]:
        """租户 scope：普通用户只见自己的 agent；admin 可见全部。"""
        stmt = select(EdgeAgent).order_by(EdgeAgent.created_at.desc())
        if user.role != Role.ADMIN.value:
            stmt = stmt.where(EdgeAgent.owner_user_id == user.id)
        return list(db.scalars(stmt))

    def get_agent(self, db: Session, agent_id: str, user: User) -> EdgeAgent | None:
        """租户 scope：越权一律视为不存在（404 语义）。"""
        agent = db.get(EdgeAgent, agent_id)
        if agent is None:
            return None
        if user.role != Role.ADMIN.value and agent.owner_user_id != user.id:
            return None
        return agent


def get_agent_from_header(request: Request, db: Session) -> EdgeAgent:
    """从 `X-Agent-Token` header 解析 EdgeAgent; 缺失/不匹配 → 401."""
    token = request.headers.get("X-Agent-Token", "")
    if not token:
        raise HTTPException(401, "missing agent token")
    agent = db.scalar(select(EdgeAgent).where(EdgeAgent.token_hash == hash_token(token)))
    if agent is None:
        raise HTTPException(401, "invalid agent token")
    return agent

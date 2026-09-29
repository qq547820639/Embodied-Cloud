"""EdgeService: 边缘设备注册/心跳/遥测 + X-Agent-Token 认证.

原始 token 仅 register 时返回一次; 之后边缘端通过 `X-Agent-Token` header
认证 (服务端只存 hash, 见 security.hash_token).
"""

import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from ..models import (
    AgentStatus,
    DeploymentRecord,
    DeploymentStatus,
    EdgeAgent,
    Role,
    TelemetryEvent,
    User,
)
from ..schemas import EdgeAgentOut
from ..security import generate_token, hash_token
from ..utils import utcnow

#: 设备回报"这一格跑完了"的那类遥测的线协议名。
#: 与控制面包 `edge_agent/agent.py` 里同名常量是一对**跨包**重复——设备包刻意不 import
#: 服务端（`edge_agent/drivers.py` 的模块 docstring 写了理由），所以相等关系由常驻判据
#: 钉住（tests/test_edge_run_closes_deployment.py），而不是靠共享模块。
RUN_TELEMETRY_KIND = "edge-run"
#: 设备在**按下驱动之前**发的那一条：「我要开始跑这一格了」。
#: 它与 `edge-run` 是一对，但不是一个——`edge-run` 是结果（并把 `running` 收成终态），
#: 这条只是自报进度，**不改任何权威列**（N-139 要的就是这条投影，见 `announced_run_id`）。
RUN_START_TELEMETRY_KIND = "edge-run-started"
#: 往回扫多少条开跑声明。超出的形状（设备连着报了几十次开跑，却没有一次落在当前
#: 这批 `running` 里）本身就说明状态该查，不由这里猜一个答案。
_STARTED_SCAN = 20


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

    def expire_stale_agents(
        self, db: Session, *, offline_after_seconds: int, now: datetime | None = None
    ) -> int:
        """把超过 `offline_after_seconds` 没心跳的 `online` 设备改成 `offline`，返回改动条数。

        `online` 是一条**存在性主张**，只能由"最近收到过心跳"这一事实背书。此前全仓没有任何
        一处写 `OFFLINE`（`AgentStatus.OFFLINE` 零写入者、`last_heartbeat` 零读者），所以断掉的
        设备永远显示在线：运维看到的是一台可以派活的机器人，而控制面已经再没听到过它的声音。

        只碰 ONLINE：`REGISTERED`（从没通过话）不许被改写成"曾经在线后掉线"，已经 `OFFLINE`
        的也不必重复写。比较用 `last_heartbeat <` ——`last_heartbeat IS NULL` 的行（从没心跳）
        在这条 WHERE 下自然不被选中，不需要额外的判空。
        这里**不**顺手 gate 派工：本设计是设备侧拉取（`GET /deployments/assigned`），把任务派给
        一台暂时离线的设备是正常用法（它上线后自己取），所以状态列只负责说真话。

        同一条判决把这台设备名下还挂着 `running` 的部署一起收口（N-133，闭登记项 N-115）：
        `running` 也是一条存在性主张，它的唯一凭证就是"这台设备还会回来报结果"。设备被判离线
        之后再没有人替它报，那一行就永远停在 `running`——运维看到的是一个还在跑的机器人任务，
        而控制面已经再没听到过它的声音（N-109 的 `reported=false` 档、设备掉了而部署没重下、
        以及人工在库外把部署推到 running，三种形状都落在这里）。判成 `failed` 而不是回退到
        `pending`：借鉴 K8s Job 的 `activeDeadlineSeconds`／SLURM `--time` 那一档——超时的运行是
        一次**已结束的失败**，重跑要人重新下部署，不由控制面私自重来一遍。
        终态一旦写下就不许被后到的读数复活：设备恢复后补报的 `ok=true` 走
        `complete_from_agent_report`，那条 UPDATE 的 WHERE 钉着 `status == running`，rowcount 0，
        遥测事件照旧留档。未绑定设备（`edge_agent_id IS NULL`）的 `running` **不判**：
        没有"最后一次被看见"的证据列可依据，把"没人报"读成"设备没了"就是 ADR 0008 禁止的
        把未知当缺席——那一半另登登记项。
        """
        threshold = (now or utcnow()) - timedelta(seconds=offline_after_seconds)
        stale = list(
            db.scalars(
                select(EdgeAgent).where(
                    EdgeAgent.status == AgentStatus.ONLINE.value,
                    EdgeAgent.last_heartbeat < threshold,
                )
            )
        )
        for agent in stale:
            agent.status = AgentStatus.OFFLINE.value
        if stale:
            db.execute(
                update(DeploymentRecord)
                .where(
                    DeploymentRecord.edge_agent_id.in_([agent.id for agent in stale]),
                    DeploymentRecord.status == DeploymentStatus.RUNNING.value,
                )
                .values(
                    status=DeploymentStatus.FAILED.value,
                    error_message="device went offline before reporting the run",
                )
                # SQL 层比较，避开 ORM 的 in-Python evaluator（同 complete_from_agent_report）
                .execution_options(synchronize_session=False)
            )
            db.commit()
        return len(stale)

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

    def list_telemetry(
        self, db: Session, agent: EdgeAgent, limit: int = 50
    ) -> list[TelemetryEvent]:
        """读某一设备的遥测（调用方必须已经过 `get_agent` 的租户校验）。

        补的是"只写不读"：`report_telemetry` 从 v0.4 起就在写这张表，但全仓
        没有任何读路径——设备回报的运行结果落在库里没人看得见。
        """
        stmt = (
            select(TelemetryEvent)
            .where(TelemetryEvent.edge_agent_id == agent.id)
            .order_by(TelemetryEvent.created_at.desc())
            .limit(limit)
        )
        return list(db.scalars(stmt))

    def announced_run_id(self, db: Session, agent_id: str) -> str | None:
        """这台设备自己声明的「此刻在跑哪一条部署」——只从 append-only 遥测派生。

        形状是**两个集合的差**，不是"最新一条"：`开跑过` − `已经报完`。
        为什么不看 `deployments.status == running`：那个状态是控制面的人为闸（
        ADR 0007 把 `verified → running` 归控制面），而设备的物理运行常常发生在有人按下
        `/run` **之前**（`_handle` 里报完摘要就直接上机）。拿一个人为闸去决定
        "设备说它在跑什么"这个问题，答案会常年是 null，而那是假读数。
        反向的闸要留：哪条记录已经被任何权威收口了（`success`／`failed`——
        设备自报 N-113、掉线收口 N-133、超时判决 N-136 三条路都算），那条声明就作废，
        否则这里会永远挂着一件已经结束的事。

        为什么不新开一列 `edge_agents.current_deployment_id`：那一列会变成第二个「运行进展」
        的权威，还得靠设备记得清它；而这里读的是设备已经写进 `telemetry_events` 的事实，
        控制面的权威状态列一个字没动。

        不可判的几种形状一律回 None（宁可不答也不替设备猜）：没有未闭合的声明、
        声明指的行根本不归这台设备（越权声明不参与判定）、有多条未闭合的声明
        （这台设备是单线程的，两条并存本身就说明状态该查）、声明指的那行已被
        任何权威收口，以及要往回翻超过 `_STARTED_SCAN` 条才找得到（那说明声明早就没人管了）。
        """
        started = self._announced_ids(db, agent_id, RUN_START_TELEMETRY_KIND)
        finished = self._announced_ids(db, agent_id, RUN_TELEMETRY_KIND)
        open_ids = started - finished
        if not open_ids:
            return None
        # 只认**绑给自己**的那些行：一台设备替别人的部署声明在跑，既不该显示在它的格上，
        # 也不该因此把自己那格清空（越权的声明直接不参与判定）。
        mine = set(
            db.scalars(
                select(DeploymentRecord.id).where(
                    DeploymentRecord.id.in_(open_ids),
                    DeploymentRecord.edge_agent_id == agent_id,
                )
            )
        )
        if not mine:
            return None
        closed = set(
            db.scalars(
                select(DeploymentRecord.id).where(
                    DeploymentRecord.id.in_(mine),
                    DeploymentRecord.status.in_(
                        [DeploymentStatus.SUCCESS.value, DeploymentStatus.FAILED.value]
                    ),
                )
            )
        )
        alive = mine - closed
        if len(alive) != 1:
            return None
        return next(iter(alive))

    def _announced_ids(self, db: Session, agent_id: str, kind: str) -> set[str]:
        """某一类运行声明点名的 deployment_id 集合（JSON 路径在两侧写法不同，解析放 Python）.

        `payload ->> 'deployment_id'`（PostgreSQL）与 `json_extract(payload, '$.deployment_id')`
        （SQLite）不是同一句话，ADR 0005 要求这条通路两侧语义对等，所以这里只按
        `kind` 在 SQL 侧筛行、取回 payload 在 Python 里读那一个键——与
        `DeploymentService.fail_overdue_runs` 里"逐行时间换算放 Python"同一个取舍。
        """
        rows: list[dict] = list(
            db.scalars(
                select(TelemetryEvent.payload)
                .where(
                    TelemetryEvent.edge_agent_id == agent_id,
                    TelemetryEvent.kind == kind,
                )
                .order_by(TelemetryEvent.created_at.desc())
                .limit(_STARTED_SCAN)
            )
        )
        return {str((payload or {}).get("deployment_id") or "") for payload in rows} - {""}

    def agent_out(self, db: Session, agent: EdgeAgent) -> EdgeAgentOut:
        """`EdgeAgentOut` 的唯一生产者——四个面（register／heartbeat／list／detail）都走这里。

        `current_deployment_id` 是**派生**值而不是 ORM 列，所以 `model_validate(agent)` 不会带上它。
        若放某个面各自 validate 原始对象，那一面这一格就永远是 null，而 null 在读端与
        "这台设备没在跑东西"完全同形（N-135 给 `last_synced_at` 立的规矩在这里同样成立）。
        """
        out = EdgeAgentOut.model_validate(agent)
        return out.model_copy(
            update={"current_deployment_id": self.announced_run_id(db, agent.id)}
        )


def get_agent_from_header(request: Request, db: Session) -> EdgeAgent:
    """从 `X-Agent-Token` header 解析 EdgeAgent; 缺失/不匹配 → 401."""
    token = request.headers.get("X-Agent-Token", "")
    if not token:
        raise HTTPException(401, "missing agent token")
    agent = db.scalar(select(EdgeAgent).where(EdgeAgent.token_hash == hash_token(token)))
    if agent is None:
        raise HTTPException(401, "invalid agent token")
    return agent

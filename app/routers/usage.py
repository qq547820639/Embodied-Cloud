"""Usage + Ledger —— 基于不可变账本，owner 隔离。"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..deps import DB, CurrentUser, ledger
from ..models import CreditLedger, LedgerType, Role, Template, Workspace, WorkspaceStatus
from ..schemas import LedgerEntryOut, RechargeIn, UsageOut

router = APIRouter(tags=["usage"])


def _user_workspaces(db, user) -> list[Workspace]:
    stmt = select(Workspace).where(Workspace.deleted_at.is_(None))
    if user.role != Role.ADMIN.value:
        stmt = stmt.where(Workspace.user_id == user.id)
    return list(db.scalars(stmt))


@router.get("/usage", response_model=UsageOut)
def usage(db: DB, user: CurrentUser):
    workspaces = _user_workspaces(db, user)
    now = datetime.now(UTC)
    running = [w for w in workspaces if w.status == WorkspaceStatus.RUNNING.value]
    seconds_by_workspace: dict[str, int] = {}
    for w in workspaces:
        live = 0
        if w.status == WorkspaceStatus.RUNNING.value and w.started_at:
            started = w.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            live = max(0, int((now - started).total_seconds()))
        seconds_by_workspace[w.id] = w.accumulated_seconds + live
    seconds = sum(seconds_by_workspace.values())
    template_rates = {t.id: t.estimated_hourly_cost_cny for t in db.scalars(select(Template))}
    estimated = sum(
        (seconds_by_workspace[w.id] / 3600.0) * template_rates.get(w.template_id, 0.0)
        for w in workspaces
    )
    # 任何用户（含 standalone / 无 organization）都必须返回其个人 ledger balance；
    # CreditLedger 按 user_id 聚合，与 org 归属无关。
    balance = ledger.balance(db, user.id)
    return UsageOut(
        running_workspaces=len(running),
        total_workspaces=len(workspaces),
        accumulated_gpu_seconds=seconds,
        estimated_cost_cny=round(estimated, 2),
        credits_balance=balance,
    )


@router.get("/ledger", response_model=list[LedgerEntryOut])
def ledger_history(db: DB, user: CurrentUser):
    stmt = select(CreditLedger).order_by(CreditLedger.created_at.desc()).limit(200)
    if user.role != Role.ADMIN.value:
        stmt = select(CreditLedger).where(CreditLedger.user_id == user.id).order_by(
            CreditLedger.created_at.desc()
        ).limit(200)
    return list(db.scalars(stmt))


@router.post("/ledger/recharge", response_model=LedgerEntryOut)
def recharge(payload: RechargeIn, db: DB, user: CurrentUser):
    """充值（demo 语义：直接记账）。幂等：同 idempotency_key 不重复入账。"""
    entry = ledger.record(
        db,
        type=LedgerType.RECHARGE,
        amount=payload.amount,
        user_id=user.id,
        organization_id=user.organization_id,
        description="recharge",
        idempotency_key=payload.idempotency_key or f"recharge:{user.id}:{uuid.uuid4()}",
    )
    return entry


@router.post("/admin/ledger/adjustment", response_model=LedgerEntryOut, include_in_schema=False)
def admin_adjustment(amount: int, description: str, user: CurrentUser, db: DB):
    if user.role != Role.ADMIN.value:
        raise HTTPException(403, "admin role required")
    return ledger.record(
        db,
        type=LedgerType.ADJUSTMENT,
        amount=amount,
        user_id=user.id,
        description=description,
        idempotency_key=f"adj:{user.id}:{uuid.uuid4()}",
    )

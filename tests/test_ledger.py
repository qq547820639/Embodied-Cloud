"""Credit Ledger：不可变、幂等、auditable；同一 usage 不重复扣款。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, CreditLedger, LedgerType, Template, Workspace
from app.services.ledger import CreditLedgerService

ENGINE = create_engine("sqlite:///./test-ledger.db", connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


def _template() -> Template:
    return Template(
        id="cartpole", slug="cartpole", name="Cartpole", version="0.1.0",
        description="d", category="RL", runtime="isaaclab",
        entrypoint="x", outputs=[], metadata_json={}, launch_command="x",
    )


def test_ledger_is_immutable_and_balance_is_sum():
    service = CreditLedgerService(Factory)
    with Factory() as db:
        service.record(db, type=LedgerType.RECHARGE, amount=10000, user_id="u1", description="top up")
        service.record(db, type=LedgerType.USAGE, amount=-3600, user_id="u1", description="usage")
        service.record(db, type=LedgerType.PROMOTION, amount=500, user_id="u1", description="promo")
        assert service.balance(db, "u1") == 10000 - 3600 + 500

        rows = db.scalars(select(CreditLedger).where(CreditLedger.user_id == "u1")).all()
        assert len(rows) == 3
        # 审计：每行有 idempotency_key + created_at
        for r in rows:
            assert r.idempotency_key
            assert r.created_at is not None


def test_record_is_idempotent_by_key():
    service = CreditLedgerService(Factory)
    with Factory() as db:
        first = service.record(db, type=LedgerType.RECHARGE, amount=500, user_id="u1", idempotency_key="k1")
        second = service.record(db, type=LedgerType.RECHARGE, amount=500, user_id="u1", idempotency_key="k1")
        assert first.id == second.id  # 同 key 返回同一记录，不重复入账
        assert service.balance(db, "u1") == 500
        assert db.scalar(select(func.count(CreditLedger.id))) == 1


def test_usage_settlement_idempotent_per_run():
    """同一运行段（同一 started_at）重复结算不重复扣款；新运行段正常计费。"""
    service = CreditLedgerService(Factory)
    with Factory() as db:
        db.add(_template())
        db.commit()
        ws = Workspace(
            id="ws-1", name="ws", template_id="cartpole", user_id="u1",
            provider="mock", status="running",
            started_at=datetime.now(UTC),
        )
        db.add(ws)
        db.commit()

        # 第一次结算 60s
        e1 = service.settle_workspace_run(db, ws, 60, ws.started_at.isoformat())
        assert e1 is not None and e1.amount == -60
        # 进程崩溃重放：同一 started_at 再结算 → 幂等返回同记录
        e2 = service.settle_workspace_run(db, ws, 60, ws.started_at.isoformat())
        assert e2 is not None and e2.id == e1.id
        # 仅一条 USAGE 交易
        assert service.balance(db, "u1") == -60

        # 新运行段（新 started_at）→ 正常新增扣款
        new_start = "2026-08-12T10:00:00+00:00"
        e3 = service.settle_workspace_run(db, ws, 120, new_start)
        assert e3 is not None and e3.amount == -120
        assert service.balance(db, "u1") == -180


def test_zero_seconds_no_transaction():
    service = CreditLedgerService(Factory)
    with Factory() as db:
        db.add(_template())
        db.commit()
        ws = Workspace(id="ws-0", name="ws", template_id="cartpole", user_id="u1", provider="mock", status="stopped")
        db.add(ws)
        db.commit()
        assert service.settle_workspace_run(db, ws, 0, "2026-01-01T00:00:00+00:00") is None
        assert service.balance(db, "u1") == 0

"""BillingPolicy 与结算正确性（§17）。

- test_negative_balance_policy: 余额为负 → 拒绝启动（402）
- test_course_quota: quota_seconds 真实执行（用量 ≥ 配额 → 拒绝）
- test_billing_restart_idempotency: 同一运行段重复结算不重复扣费
- test_duplicate_stop_no_double_charge: 重复 stop 不双重扣费
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import app
from app.models import (
    Course,
    CourseMember,
    CreditLedger,
    Lab,
    LedgerType,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceStatus,
)
from app.services.billing import BillingPolicy
from app.services.ledger import CreditLedgerService
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler

ENGINE = create_engine("sqlite:///./test-billing.db", connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


def _seed_gpu_template(db) -> Template:
    GpuScheduler(Factory).sync_host(
        db,
        host_id="host-1",
        name="h1",
        address="127.0.0.1",
        provider="mock",
        gpus=[GpuInfo(gpu_uuid="gpu-1", model="RTX", memory_total=24564, index=0)],
    )
    t = Template(
        id="cartpole",
        slug="cartpole",
        name="cartpole",
        description="test",
        category="test",
        runtime="mock",
        launch_command="echo ok",
        enabled=True,
        recommended_vram_gb=16,
        estimated_hourly_cost_cny=6.0,
    )
    db.add(t)
    db.commit()
    return t


def _make_user(db, user_id: str = "u1", role: str = Role.USER.value) -> User:
    user = User(id=user_id, email=f"{user_id}@example.com", username=user_id, password_hash="x", role=role)  # noqa: S106 测试数据)
    db.add(user)
    db.commit()
    return user


def _orchestrator_with_billing(provider=None) -> tuple[WorkspaceOrchestrator, BillingPolicy]:
    provider = provider or MockProvider("http://127.0.0.1:8000")
    ledger = CreditLedgerService(Factory)
    billing = BillingPolicy(Factory, ledger)
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-billing-ws"), billing=billing  # noqa: S108
    )
    return orchestrator, billing


def _settle_usage(db, user_id: str, seconds: int, workspace_id: str, started_iso: str):
    """直接记一笔 usage 扣费（模拟结算）。"""
    ledger = CreditLedgerService(Factory)
    ledger.record(
        db,
        type=LedgerType.USAGE,
        amount=-seconds,
        user_id=user_id,
        description="usage",
        workspace_id=workspace_id,
        gpu_seconds=seconds,
        idempotency_key=f"usage:{workspace_id}:{started_iso}",
    )


# ---------------------------------------------------------------------------
# negative balance policy
# ---------------------------------------------------------------------------


def test_negative_balance_policy_rejects_launch():
    _, billing = _orchestrator_with_billing()
    with Factory() as db:
        _seed_gpu_template(db)
        user = _make_user(db)
        # 充值 100 → 结算扣 150 → 余额 -50
        ledger = CreditLedgerService(Factory)
        ledger.record(db, type=LedgerType.RECHARGE, amount=100, user_id=user.id, idempotency_key="r1")
        _settle_usage(db, user.id, 150, "ws-paid", "2026-08-12T00:00:00+00:00")
        assert billing.ledger.balance(db, user.id) == -50

        template = db.get(Template, "cartpole")
        # 拒绝启动
        with pytest.raises(Exception) as exc_info:
            billing.check_launch_eligible(db, user, template)
        assert "insufficient credits" in str(exc_info.value)

    # 充值补齐后恢复可启动
    with Factory() as db:
        ledger = CreditLedgerService(Factory)
        ledger.record(db, type=LedgerType.RECHARGE, amount=100, user_id="u1", idempotency_key="r2")
        assert billing.ledger.balance(db, "u1") == 50
        billing.check_launch_eligible(db, db.get(User, "u1"), db.get(Template, "cartpole"))  # 不抛


def test_api_rejects_workspace_create_with_negative_balance():
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/register",
            json={"email": "neg@example.com", "username": "neg", "password": "password123"},
        )
        token = resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        user_id = resp.json()["user"]["id"]

        # 充值 100 后消费 150 → 余额 -50
        client.post("/api/ledger/recharge", json={"amount": 100}, headers=headers)
        from app.deps import SessionFactory

        with SessionFactory() as db:
            ws = Workspace(id="ws-neg", name="n", template_id="cartpole", provider="mock", user_id=user_id)
            db.add(ws)
            db.commit()
            ledger = CreditLedgerService(SessionFactory)
            ledger.record(
                db, type=LedgerType.USAGE, amount=-150, user_id=user_id,
                workspace_id="ws-neg", idempotency_key="usage:ws-neg:t0",
            )

        # 余额为负 → 402
        resp = client.post(
            "/api/workspaces", json={"template_id": "cartpole", "auto_start": True}, headers=headers
        )
        assert resp.status_code == 402
        assert "insufficient credits" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# course quota
# ---------------------------------------------------------------------------


def test_course_quota_enforced():
    _, billing = _orchestrator_with_billing()
    with Factory() as db:
        template = _seed_gpu_template(db)
        user = _make_user(db, user_id="student-1", role=Role.STUDENT.value)
        _make_user(db, user_id="teacher-1", role=Role.INSTRUCTOR.value)  # course owner
        course = Course(id="c1", owner_id="teacher-1", name="course", slug="course-1")
        db.add(course)
        db.flush()
        db.add(
            CourseMember(
                id="cm-1", course_id="c1", user_id="student-1", role=Role.STUDENT.value
            )
        )
        lab = Lab(id="lab-1", course_id="c1", template_id="cartpole", name="lab", quota_seconds=100)
        db.add(lab)
        db.commit()

        # 已用 50s → 可启动
        ws = Workspace(
            id="ws-1", name="w", template_id="cartpole", provider="mock",
            user_id="student-1", status=WorkspaceStatus.STOPPED.value, accumulated_seconds=50,
        )
        db.add(ws)
        db.commit()
        billing.check_launch_eligible(db, user, template, lab=lab)  # 不抛

        # 已用 100s（= 配额）→ 拒绝
        ws.accumulated_seconds = 100
        db.commit()
        with pytest.raises(Exception) as exc_info:
            billing.check_launch_eligible(db, user, template, lab=lab)
        assert "quota exhausted" in str(exc_info.value)

        # 已用 150s > 配额 → 拒绝
        ws.accumulated_seconds = 150
        db.commit()
        with pytest.raises(Exception) as exc_info:
            billing.check_launch_eligible(db, user, template, lab=lab)
        assert "quota exhausted" in str(exc_info.value)


# ---------------------------------------------------------------------------
# settlement correctness
# ---------------------------------------------------------------------------


def test_billing_restart_idempotency():
    """同一运行段（同 started_at）重复结算 → 只扣一次（restart-safe）。"""
    ledger = CreditLedgerService(Factory)
    with Factory() as db:
        _seed_gpu_template(db)
        user = _make_user(db)
        workspace = Workspace(
            id="ws-1", name="w", template_id="cartpole", provider="mock",
            user_id=user.id, status=WorkspaceStatus.STOPPED.value,
        )
        db.add(workspace)
        db.commit()

        started_iso = "2026-08-12T08:00:00+00:00"
        entry1 = ledger.settle_workspace_run(db, workspace, 60, started_iso)
        # 模拟控制面重启后重放同一结算
        entry2 = ledger.settle_workspace_run(db, workspace, 60, started_iso)
        assert entry1 is not None and entry2 is not None
        assert entry1.id == entry2.id  # 幂等：返回同一条记录

        entries = db.scalars(
            select(CreditLedger).where(CreditLedger.type == LedgerType.USAGE.value)
        ).all()
        assert len(entries) == 1
        assert entries[0].gpu_seconds == 60
        assert entries[0].amount == -60
        assert ledger.balance(db, user.id) == -60  # 只扣一次


def test_duplicate_stop_no_double_charge():
    """同一 workspace 重复 stop：结算只发生一次，不双重扣费。"""
    orchestrator, _ = _orchestrator_with_billing()
    with Factory() as db:
        _seed_gpu_template(db)
        user = _make_user(db)
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id=user.id)
        wid = ws.id
        orchestrator._start(wid)  # 同步 provisioning（测试路径）

    with Factory() as db:
        # 等 RUNNING 后设置 started_at 在过去（确保可结算秒数）
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value
        from datetime import UTC, datetime, timedelta

        ws.started_at = datetime.now(UTC) - timedelta(seconds=30)
        db.commit()

    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator.stop(db, ws)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator.stop(db, ws)  # 重复 stop

    with Factory() as db:
        entries = db.scalars(
            select(CreditLedger).where(CreditLedger.workspace_id == wid)
        ).all()
        usage = [e for e in entries if e.type == LedgerType.USAGE.value]
        assert len(usage) == 1  # 不双重扣费
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPED.value

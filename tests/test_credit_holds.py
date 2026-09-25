"""§18 CreditHold 预授权语义（默认档，SQLite）。

覆盖：圈额度 / 可用额扣减 / 结算转正 / 失败退回 / 超时回收 / 豁免与开关。
并发双花那一半在 tests/test_postgres_concurrency.py（SQLite 没有真行锁，
`with_for_update` 被方言整条丢弃 —— 见 tests/test_sqlite_semantic_baseline.py）。
"""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    BillingAccount,
    BillingSubject,
    CreditHold,
    CreditLedger,
    HoldStatus,
    LedgerType,
    Organization,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceStatus,
)
from app.services.billing import BillingError, BillingPolicy
from app.services.ledger import CreditLedgerService
from app.utils import utcnow

# 与本模块同名的一次性文件库（与仓库其它离线档一致的做法），非跨会话共享
DB_PATH = Path(__file__).resolve().parents[1] / "test-credit-holds.db"
ENGINE = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)
    # 不 unlink：模块级 ENGINE 仍连着这个 inode，删文件会让下一条用例的
    # create_all 撞上 "attempt to write a readonly database"


def make_policy(**overrides) -> BillingPolicy:
    kwargs = {"enforce_preauthorization": True, "minimum_launch_minutes": 5, "hold_ttl_minutes": 30}
    kwargs.update(overrides)
    return BillingPolicy(Factory, CreditLedgerService(Factory), **kwargs)


def seed_user(credits: int = 1000, *, org: bool = False, role: str = Role.USER.value) -> tuple[str, str | None]:
    with Factory() as db:
        organization_id = None
        if org:
            organization = Organization(id="org-1", name="Lab")
            db.add(organization)
            organization_id = organization.id
        user = User(
            id="user-1",
            email="holder@example.org",
            username="holder",
            password_hash="x",  # noqa: S106 测试用假口令
            role=role,
            organization_id=organization_id,
        )
        db.add(user)
        db.commit()
        if credits:
            CreditLedgerService(Factory).record(
                db,
                type=LedgerType.RECHARGE,
                amount=credits,
                user_id=user.id,
                idempotency_key=f"recharge:{user.id}",
            )
        return user.id, organization_id


def seed_workspace(user_id: str, workspace_id: str = "ws-1", status: str = WorkspaceStatus.QUEUED.value) -> str:
    with Factory() as db:
        db.add(
            Workspace(
                id=workspace_id,
                name="hold test",
                template_id="cartpole",
                provider="mock",
                status=status,
                user_id=user_id,
            )
        )
        db.commit()
    return workspace_id


def get_user(user_id: str) -> User:
    with Factory() as db:
        return db.get(User, user_id)


def holds(db: Session) -> list[CreditHold]:
    return list(db.scalars(select(CreditHold)))


# ---------------------------------------------------------------------------
# 圈额度 / 可用额
# ---------------------------------------------------------------------------


def test_reserve_creates_pending_hold_and_reduces_available_credits():
    user_id, _ = seed_user(credits=1000)
    ws = seed_workspace(user_id)
    policy = make_policy()
    with Factory() as db:
        user = db.get(User, user_id)
        assert policy.available_credits(db, user) == 1000
        hold = policy.reserve_launch(db, user, ws)
        assert hold is not None
        assert hold.status == HoldStatus.PENDING.value
        assert hold.amount == 300  # 5 min × 60s，与结算口径同单位（credit = GPU 秒）
        assert policy.available_credits(db, user) == 700
        # hold 不进账本：账本仍只有那笔充值
        assert len(list(db.scalars(select(CreditLedger)))) == 1


def test_reserve_is_idempotent_per_workspace():
    user_id, _ = seed_user(credits=1000)
    ws = seed_workspace(user_id)
    policy = make_policy()
    with Factory() as db:
        user = db.get(User, user_id)
        first = policy.reserve_launch(db, user, ws)
        second = policy.reserve_launch(db, user, ws)
        assert first.id == second.id
        assert len(holds(db)) == 1
        assert policy.available_credits(db, user) == 700


def test_launch_gate_uses_held_amount_and_refuses_when_spent():
    """1000 credits 只够圈 3 次（300×3=900）；第 4 次必须被拒。

    旧行为是"启动后再变负数"，这里是启动前就拒 —— 正是 §18 的落点。
    """
    user_id, _ = seed_user(credits=1000)
    policy = make_policy()
    for i in range(3):
        ws = seed_workspace(user_id, f"ws-{i}")
        with Factory() as db:
            assert policy.reserve_launch(db, db.get(User, user_id), ws) is not None
    ws4 = seed_workspace(user_id, "ws-4")
    with Factory() as db:
        with pytest.raises(BillingError, match="insufficient credits to reserve"):
            policy.reserve_launch(db, db.get(User, user_id), ws4)
        assert len(holds(db)) == 3, "被拒的启动不该留下 hold 行"


def test_disabled_preauthorization_creates_no_rows():
    """本地/演示默认关闭：不得产生任何账户/hold 行（否则 mock 档会被门禁挡住）。"""
    user_id, _ = seed_user(credits=0)
    ws = seed_workspace(user_id)
    policy = make_policy(enforce_preauthorization=False)
    with Factory() as db:
        assert policy.reserve_launch(db, db.get(User, user_id), ws) is None
        assert holds(db) == []
        assert list(db.scalars(select(BillingAccount))) == []


def test_admin_and_instructor_are_exempt():
    for role in (Role.ADMIN.value, Role.INSTRUCTOR.value):
        Base.metadata.drop_all(ENGINE)
        Base.metadata.create_all(ENGINE)
        user_id, _ = seed_user(credits=0, role=role)
        ws = seed_workspace(user_id)
        policy = make_policy()
        with Factory() as db:
            assert policy.reserve_launch(db, db.get(User, user_id), ws) is None
            assert holds(db) == []


# ---------------------------------------------------------------------------
# capture / release / 过期
# ---------------------------------------------------------------------------


def test_capture_turns_hold_into_spend_and_frees_the_remainder():
    user_id, _ = seed_user(credits=1000)
    ws = seed_workspace(user_id)
    policy = make_policy()
    with Factory() as db:
        user = db.get(User, user_id)
        hold = policy.reserve_launch(db, user, ws)
        captured = policy.capture_hold(
            db, ws, usage_seconds=40, ledger_usage_key=f"usage:{ws}:x"
        )
        assert captured is not None and captured.status == HoldStatus.CAPTURED.value
        assert captured.captured_amount == 40
        # capture 后不再占用额度；真实消费由账本体现（300 圈住、40 实收 → 740 可用）
        ledger = CreditLedgerService(Factory)
        ledger.record(
            db,
            type=LedgerType.USAGE,
            amount=-40,
            user_id=user_id,
            idempotency_key=f"usage:{ws}:x",
        )
        assert policy.available_credits(db, user) == 1000 - 40
        assert policy.pending_hold_total(db, [hold.account_id]) == 0


def test_release_returns_credits_without_touching_the_ledger():
    user_id, _ = seed_user(credits=1000)
    ws = seed_workspace(user_id)
    policy = make_policy()
    with Factory() as db:
        user = db.get(User, user_id)
        policy.reserve_launch(db, user, ws)
        assert policy.available_credits(db, user) == 700
        released = policy.release_hold(db, ws, reason="provision failed")
        assert released is not None and released.status == HoldStatus.RELEASED.value
        assert released.released_at is not None
        assert policy.available_credits(db, user) == 1000
        assert len(list(db.scalars(select(CreditLedger)))) == 1, "release 不得写账本"
        # 幂等：没有 pending hold 时返回 None
        assert policy.release_hold(db, ws, reason="again") is None


def test_expired_hold_is_reclaimed_unless_workspace_is_running():
    user_id, _ = seed_user(credits=1000)
    ws_stuck = seed_workspace(user_id, "ws-stuck", status=WorkspaceStatus.QUEUED.value)
    ws_running = seed_workspace(user_id, "ws-run", status=WorkspaceStatus.RUNNING.value)
    policy = make_policy()
    with Factory() as db:
        user = db.get(User, user_id)
        for ws in (ws_stuck, ws_running):
            hold = policy.reserve_launch(db, user, ws)
            # 直接改库造"已过期"前提，不靠 sleep
            hold.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()

    with Factory() as db:
        assert policy.release_expired_holds(db) == 1
        by_ws = {h.workspace_id: h.status for h in holds(db)}
        assert by_ws[ws_stuck] == HoldStatus.RELEASED.value
        assert by_ws[ws_running] == HoldStatus.PENDING.value, "RUNNING 段的 hold 不能扫成 released"


def test_sweep_only_touches_its_own_expiry_and_is_repeatable():
    user_id, _ = seed_user(credits=1000)
    ws = seed_workspace(user_id)
    policy = make_policy()
    with Factory() as db:
        policy.reserve_launch(db, db.get(User, user_id), ws)
    with Factory() as db:
        assert policy.release_expired_holds(db) == 0  # 未过期
        row = db.scalar(select(CreditHold))
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    with Factory() as db:
        assert policy.release_expired_holds(db) == 1
    with Factory() as db:
        assert policy.release_expired_holds(db) == 0, "扫描必须幂等（已 released 不再计数）"


# ---------------------------------------------------------------------------
# 账户归属（§17）
# ---------------------------------------------------------------------------


def test_org_credits_are_visible_but_hold_is_taken_once():
    """组织账户补足可用额；hold 记在个人账户上，但可用额合并计算。"""
    user_id, org_id = seed_user(credits=100, org=True)
    with Factory() as db:
        CreditLedgerService(Factory).record(
            db,
            type=LedgerType.RECHARGE,
            amount=5000,
            organization_id=org_id,
            idempotency_key="recharge:org-1",
        )
    ws = seed_workspace(user_id)
    policy = make_policy()
    with Factory() as db:
        user = db.get(User, user_id)
        assert policy.available_credits(db, user) == 5100
        accounts = policy.user_accounts(db, user)
        assert {(a.subject_type, a.subject_id) for a in accounts} == {
            (BillingSubject.USER.value, user_id),
            (BillingSubject.ORGANIZATION.value, org_id),
        }
        hold = policy.reserve_launch(db, user, ws)
        assert hold.account_id == accounts[0].id  # 个人账户
        assert policy.available_credits(db, user) == 5100 - 300


def test_account_rows_are_reused_not_recreated():
    user_id, _ = seed_user(credits=10)
    policy = make_policy()
    with Factory() as db:
        user = db.get(User, user_id)
        a1 = policy.user_accounts(db, user)
        a2 = policy.user_accounts(db, user)
        assert [a.id for a in a1] == [a.id for a in a2]
        assert len(list(db.scalars(select(BillingAccount)))) == 1


# ---------------------------------------------------------------------------
# 真实启动路径接线（provision→reserve、stop→capture、fail→release）
# ---------------------------------------------------------------------------


def _seed_runtime_env(gpu_vram_mib: int = 24576, *, vram_gb: int = 16) -> None:
    from app.services.scheduler import GpuInfo, GpuScheduler

    with Factory() as db:
        GpuScheduler(Factory).sync_host(
            db,
            host_id="host-1",
            name="h1",
            address="127.0.0.1",
            provider="mock",
            gpus=[GpuInfo(gpu_uuid="GPU-hold-1", model="RTX", memory_total=gpu_vram_mib, index=0)],
        )
        db.add(
            Template(
                id="cartpole",
                slug="cartpole",
                name="Cartpole",
                description="t",
                category="rl",
                runtime="mock",
                launch_command="echo ok",
                enabled=True,
                recommended_vram_gb=vram_gb,
                gpu_requirement_gb=vram_gb,
                estimated_hourly_cost_cny=1.0,
            )
        )
        db.commit()


def _orchestrator(policy: BillingPolicy):
    from app.services.orchestrator import WorkspaceOrchestrator
    from app.services.providers.mock import MockProvider

    return WorkspaceOrchestrator(
        Factory,
        MockProvider("http://127.0.0.1:8000"),
        Path("/tmp/ec-credit-holds"),  # noqa: S108 测试沙箱目录
        ledger=CreditLedgerService(Factory),
        billing=policy,
    )


def test_provision_reserves_and_stop_captures_through_the_real_path():
    """走 worker 的真实执行链，而不是直接调 policy 方法。"""
    from app.models import OperationType
    from app.services.worker import OperationWorker

    _seed_runtime_env()
    user_id, _ = seed_user(credits=5000)
    policy = make_policy()
    orchestrator = _orchestrator(policy)
    worker = OperationWorker(Factory, orchestrator)
    with Factory() as db:
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id=user_id)
        workspace_id = ws.id
    assert worker.enqueue(workspace_id, OperationType.PROVISION) is not None
    assert worker.tick_once() == 1

    with Factory() as db:
        hold = db.scalar(select(CreditHold).where(CreditHold.workspace_id == workspace_id))
        assert hold is not None and hold.status == HoldStatus.PENDING.value
        assert db.get(Workspace, workspace_id).status == WorkspaceStatus.RUNNING.value

    with Factory() as db:
        # 把运行段拉成 120 秒（不靠 sleep）：结算必须按 120 credits 收口
        ws_row = db.get(Workspace, workspace_id)
        ws_row.started_at = utcnow() - timedelta(seconds=120)
        db.commit()
    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, workspace_id))
        hold = db.scalar(select(CreditHold).where(CreditHold.workspace_id == workspace_id))
        assert hold.status == HoldStatus.CAPTURED.value, f"stop 未把 hold 转正：{hold.status}"
        assert hold.captured_amount == 120
        usage = db.scalar(select(CreditLedger).where(CreditLedger.type == LedgerType.USAGE.value))
        assert usage is not None
        assert hold.ledger_usage_key == usage.idempotency_key, "hold 与账本条目必须互链"
        assert policy.available_credits(db, db.get(User, user_id)) == 5000 - abs(usage.amount)


def test_failed_provision_releases_the_hold():
    """显存不够 → provision 失败 → 圈住的额度必须原样退回。"""
    from app.models import OperationType
    from app.services.worker import OperationWorker

    _seed_runtime_env(gpu_vram_mib=24576, vram_gb=16)
    user_id, _ = seed_user(credits=5000)
    policy = make_policy()
    orchestrator = _orchestrator(policy)
    with Factory() as db:
        big = Template(
            id="huge",
            slug="huge",
            name="Huge",
            description="t",
            category="rl",
            runtime="mock",
            launch_command="echo ok",
            enabled=True,
            recommended_vram_gb=64,
            gpu_requirement_gb=64,
            estimated_hourly_cost_cny=1.0,
        )
        db.add(big)
        db.commit()
    worker = OperationWorker(Factory, orchestrator)
    with Factory() as db:
        ws = orchestrator.create(db, db.get(Template, "huge"), user_id=user_id)
        workspace_id = ws.id
    worker.enqueue(workspace_id, OperationType.PROVISION)
    worker.tick_once()

    with Factory() as db:
        hold = db.scalar(select(CreditHold).where(CreditHold.workspace_id == workspace_id))
        assert hold is not None
        assert hold.status == HoldStatus.RELEASED.value, f"失败启动的 hold 未退回：{hold.status}"
        assert hold.reason == "provision failed"
        assert policy.available_credits(db, db.get(User, user_id)) == 5000
        # 失败不产生任何消费
        assert list(db.scalars(select(CreditLedger).where(CreditLedger.type == LedgerType.USAGE.value))) == []


def test_zero_second_run_still_closes_the_hold():
    """不足 1 秒的运行段不产生 usage 条目，但 hold 必须当场 capture。

    否则每个秒级启动都会把圈住的额度留到超时扫描才回收（少报可用额）。
    """
    from app.models import OperationType
    from app.services.worker import OperationWorker

    _seed_runtime_env()
    user_id, _ = seed_user(credits=5000)
    policy = make_policy()
    orchestrator = _orchestrator(policy)
    worker = OperationWorker(Factory, orchestrator)
    with Factory() as db:
        workspace_id = orchestrator.create(db, db.get(Template, "cartpole"), user_id=user_id).id
    worker.enqueue(workspace_id, OperationType.PROVISION)
    worker.tick_once()
    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, workspace_id))
    with Factory() as db:
        hold = db.scalar(select(CreditHold).where(CreditHold.workspace_id == workspace_id))
        assert hold is not None and hold.status == HoldStatus.CAPTURED.value, (
            f"0 秒段未收口 hold：{hold.status if hold else None}"
        )
        assert hold.captured_amount == 0
        assert hold.ledger_usage_key is None
        assert list(db.scalars(select(CreditLedger).where(CreditLedger.type == LedgerType.USAGE.value))) == []
        # 额度已释放回可用（没有消费，也没有悬空预留）
        assert policy.available_credits(db, db.get(User, user_id)) == 5000

"""BillingPolicy：workspace launch 前的额度/配额门禁（§17）。

- 个人 credits / 组织 credits：有效余额（个人+组织）为负 → 拒绝启动
- course quota_seconds：学生在 lab 的已用 GPU 秒数 ≥ 配额 → 拒绝
- admin override：admin 跳过门禁
- 结算本身保持 append-only CreditLedger + 幂等 key（restart-safe、无双重扣费）
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..models import (
    BillingAccount,
    BillingSubject,
    CreditHold,
    HoldStatus,
    Lab,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceStatus,
)
from ..utils import utcnow
from .ledger import CreditLedgerService, booked_usage_keys, workspace_seconds_used


class BillingError(RuntimeError):
    """额度不足/配额用尽；路由层映射为 HTTP 402。"""


class BillingPolicy:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        ledger: CreditLedgerService | None = None,
        minimum_launch_minutes: int = 5,
        enforce_preauthorization: bool = False,
        hold_ttl_minutes: int = 60,
    ):
        self.session_factory = session_factory
        self.ledger = ledger or CreditLedgerService(session_factory)
        self.minimum_launch_minutes = max(1, minimum_launch_minutes)
        self.enforce_preauthorization = enforce_preauthorization
        self.hold_ttl_minutes = max(1, hold_ttl_minutes)

    # ------------------------------------------------------------------
    def check_launch_eligible(
        self,
        db: Session,
        user: User,
        template: Template,
        lab: Lab | None = None,
    ) -> None:
        """启动前门禁。不满足时抛 BillingError（拒绝昂贵 runtime）。"""
        if user.role == Role.ADMIN.value:
            return  # admin override
        if user.role == Role.INSTRUCTOR.value:
            return  # 教师不受个人额度/配额限制（MVP 策略）

        # 1) 有效余额（个人 + 组织，再扣掉已被 pending hold 圈住的额度）为负 → 拒绝
        personal = self.ledger.balance(db, user.id)
        org = self.ledger.organization_balance(db, user.organization_id) if user.organization_id else 0
        available = self.available_credits(db, user)
        if available < 0:
            raise BillingError(
                f"insufficient credits: personal={personal}, organization={org} "
                f"(workspace {template.id})"
            )

        # 2) §12 预授权：生产开启时余额必须 ≥ 最低启动授权
        #    （minimum_launch_minutes × 60 credits，与结算口径 1s=1credit 一致）
        if self.enforce_preauthorization:
            required = self.minimum_launch_minutes * 60
            if available < required:
                raise BillingError(
                    f"insufficient credits for launch: {available} available, "
                    f"minimum {required} required "
                    f"({self.minimum_launch_minutes} min preauthorization)"
                )

        # 3) course quota：学生在 lab 的实际用量（秒）≥ 配额 → 拒绝
        if lab is not None:
            used = self.course_usage_seconds(db, user.id, lab)
            if used >= lab.quota_seconds:
                raise BillingError(
                    f"course quota exhausted: {used}/{lab.quota_seconds}s used "
                    f"(lab {lab.id[:8]})"
                )


    # ------------------------------------------------------------------
    # §17 计费主体 / §18 预授权（CreditHold）
    # ------------------------------------------------------------------

    def account_for(
        self,
        db: Session,
        *,
        subject_type: str,
        subject_id: str,
        owner_user_id: str | None = None,
    ) -> BillingAccount:
        """取（或建）一个计费主体行。它是"谁付钱"的唯一入口，也是预授权的锁根。"""
        account = db.scalar(
            select(BillingAccount).where(
                BillingAccount.subject_type == subject_type,
                BillingAccount.subject_id == subject_id,
            )
        )
        if account is not None:
            return account
        account = BillingAccount(
            id=str(uuid4()),
            subject_type=subject_type,
            subject_id=subject_id,
            owner_user_id=owner_user_id,
        )
        db.add(account)
        try:
            db.commit()
        except IntegrityError:
            # 同一用户的首批并发启动会同时走到这里（uq_billing_account_subject 挡下后来的）
            # → 收敛到已存在的账户行，而不是把 IntegrityError 抛成 500
            db.rollback()
            existing = db.scalar(
                select(BillingAccount).where(
                    BillingAccount.subject_type == subject_type,
                    BillingAccount.subject_id == subject_id,
                )
            )
            if existing is not None:
                return existing
            raise
        return account

    def user_accounts(self, db: Session, user: User) -> list[BillingAccount]:
        """该用户可用钱的账户集合：个人账户 +（若有）组织账户。"""
        accounts = [
            self.account_for(
                db, subject_type=BillingSubject.USER.value, subject_id=user.id, owner_user_id=user.id
            )
        ]
        if user.organization_id:
            accounts.append(
                self.account_for(
                    db,
                    subject_type=BillingSubject.ORGANIZATION.value,
                    subject_id=user.organization_id,
                    owner_user_id=user.id,
                )
            )
        return accounts

    def lock_accounts(self, db: Session, accounts: list[BillingAccount]) -> None:
        """算可用额之前先锁住主体行（PostgreSQL 真行锁；SQLite 下为 no-op，
        由 uq_holds_pending_per_workspace 保证"同一 workspace 只圈一次"）。"""
        for account in accounts:
            db.execute(
                select(BillingAccount).where(BillingAccount.id == account.id).with_for_update()
            )

    def pending_hold_total(self, db: Session, account_ids: list[str]) -> int:
        if not account_ids:
            return 0
        total = db.scalar(
            select(func.coalesce(func.sum(CreditHold.amount), 0)).where(
                CreditHold.account_id.in_(account_ids),
                CreditHold.status == HoldStatus.PENDING.value,
            )
        )
        return int(total or 0)

    def gross_credits(self, db: Session, user: User) -> int:
        """两个池子的合计：个人账户收该用户的全部行，组织账户只收无主行。

        全仓只有这一处做这个加法。改前它抄了三遍（`available_credits`、`reserve_launch`、
        配额 monitor 的投影余额），任何一侧改动都会让三个判据口径分叉；两池相加的
        不相交性由 `CreditLedgerService.organization_balance` 的谓词保证（成员行不再
        被当成组织余额重算）。
        """
        gross = self.ledger.balance(db, user.id)
        if user.organization_id:
            gross += self.ledger.organization_balance(db, user.organization_id)
        return gross

    def available_credits(self, db: Session, user: User) -> int:
        """可花额度 = 个人 + 组织账本余额 − 已被 hold 圈住的额度。

        hold 不是消费（账本里没有它），所以只有这里扣；结算后 capture 的 hold
        不再计入，消费由 usage 账本条目体现，二者不会重复扣一次。
        """
        accounts = self.user_accounts(db, user)
        return self.gross_credits(db, user) - self.pending_hold_total(db, [a.id for a in accounts])

    def reserve_launch(
        self,
        db: Session,
        user: User,
        workspace_id: str,
        *,
        minutes: int | None = None,
    ) -> CreditHold | None:
        """圈住本次启动的最低额度；已有 pending hold 时幂等返回它。

        未开启预授权（本地/演示）→ None，不产生任何行。
        不足 → BillingError（路由层 402）。
        """
        if not self.enforce_preauthorization:
            return None
        if user.role in {Role.ADMIN.value, Role.INSTRUCTOR.value}:
            return None

        existing = db.scalar(
            select(CreditHold).where(
                CreditHold.workspace_id == workspace_id,
                CreditHold.status == HoldStatus.PENDING.value,
            )
        )
        if existing is not None:
            return existing

        accounts = self.user_accounts(db, user)
        self.lock_accounts(db, accounts)
        required = (minutes if minutes is not None else self.minimum_launch_minutes) * 60
        available = self.gross_credits(db, user) - self.pending_hold_total(
            db, [a.id for a in accounts]
        )
        if available < required:
            raise BillingError(
                f"insufficient credits to reserve launch: {available} available, "
                f"{required} required ({required // 60} min hold)"
            )

        # 记账账户：优先个人（组织额度只是补足可见性），保持与结算口径一致
        account = accounts[0]
        hold = CreditHold(
            id=str(uuid4()),
            account_id=account.id,
            workspace_id=workspace_id,
            amount=required,
            status=HoldStatus.PENDING.value,
            idempotency_key=self._hold_key(db, workspace_id),
            expires_at=utcnow() + timedelta(minutes=self.hold_ttl_minutes),
            reason="launch preauthorization",
        )
        db.add(hold)
        try:
            db.commit()
        except IntegrityError:
            # 并发：另一事务已为本轮圈住额度 → 收敛到那条 pending，而不是把异常抛给 provision。
            # 兜底查询必须带 pending 过滤：改前只按 key 查、不看状态，撞键时把上一轮已
            # capture/release 的行当成本轮授权发出去（而键又按 workspace 全局唯一 ⇒ 预授权
            # 对每个 workspace 一辈子只生效一次）。找不到 pending 就照原样抛——
            # 宁可 provision 失败重试，也不能把一笔已花掉的额度当成新的授权。
            db.rollback()
            won = db.scalar(
                select(CreditHold).where(
                    CreditHold.workspace_id == workspace_id,
                    CreditHold.status == HoldStatus.PENDING.value,
                )
            )
            if won is not None:
                return won
            raise
        return hold

    def _hold_key(self, db: Session, workspace_id: str) -> str:
        """本轮启动的幂等键：`hold:{workspace_id}:{该 workspace 已有 hold 数}`。

        改前用 `hold:{workspace_id}`，而 `CreditHold.idempotency_key` 是全局唯一列
        （`app/models.py:447`）⇒ 第一次启动那条被 capture/release 之后，同一 workspace
        再次启动必然撞唯一键。轮次号从既有行数推导而不是外部传入：同一轮内的崩溃重放
        看到的是同一个数（更早的 pending 查询已经把它接住），新一轮才是新键；键按
        0,1,2… 递增发出，所以只要行不删（hold 表是 append-only 的），新键必然空闲。
        "同一 workspace 至多一个 pending"由 `uq_holds_pending_per_workspace`
        （`app/models.py:459-465`）保证，这里不负责串行化。
        """
        prior = int(
            db.scalar(
                select(func.count(CreditHold.id)).where(
                    CreditHold.workspace_id == workspace_id
                )
            )
            or 0
        )
        return f"hold:{workspace_id}:{prior}"

    def capture_hold(
        self,
        db: Session,
        workspace_id: str,
        *,
        usage_seconds: int,
        ledger_usage_key: str | None,
    ) -> CreditHold | None:
        """结算时把 pending hold 转正（余量随 released_at 语义自动回到可用额）。"""
        hold = db.scalar(
            select(CreditHold).where(
                CreditHold.workspace_id == workspace_id,
                CreditHold.status == HoldStatus.PENDING.value,
            )
        )
        if hold is None:
            return None
        hold.status = HoldStatus.CAPTURED.value
        hold.captured_amount = usage_seconds
        hold.ledger_usage_key = ledger_usage_key
        hold.captured_at = utcnow()
        db.commit()
        return hold

    def release_hold(self, db: Session, workspace_id: str, *, reason: str) -> CreditHold | None:
        """启动失败 / 结算前销毁：额度退回可用，不产生任何账本条目。"""
        hold = db.scalar(
            select(CreditHold).where(
                CreditHold.workspace_id == workspace_id,
                CreditHold.status == HoldStatus.PENDING.value,
            )
        )
        if hold is None:
            return None
        hold.status = HoldStatus.RELEASED.value
        hold.released_at = utcnow()
        hold.reason = reason
        db.commit()
        return hold

    def release_expired_holds(self, db: Session) -> int:
        """回收超时 pending hold（控制面崩溃残留）。

        RUNNING 的 workspace 不回收：它的 hold 会在下一次结算时 capture。
        """
        stale = list(
            db.scalars(
                select(CreditHold).where(
                    CreditHold.status == HoldStatus.PENDING.value,
                    CreditHold.expires_at < utcnow(),
                )
            )
        )
        released = 0
        for hold in stale:
            ws = db.get(Workspace, hold.workspace_id)
            if ws is not None and ws.status == WorkspaceStatus.RUNNING.value:
                continue
            hold.status = HoldStatus.RELEASED.value
            hold.released_at = utcnow()
            hold.reason = "hold expired before settle"
            released += 1
        if released:
            db.commit()
        return released

    def check_course_quota(self, db: Session, user: User, template: Template) -> None:
        """provision 重试路径的 course quota 门禁（无 lab 上下文）。

        `check_launch_eligible` 仅在显式传入 lab 时才检查 course quota；workspace
        只记录 template_id 不记录 lab_id，因此重试/直接执行 PROVISION 时按
        template 匹配所有 lab，任一 lab 用量 ≥ 配额即拒绝（与 monitor_runtime_quotas
        的判定口径一致）。admin/instructor 不受限制。
        """
        if user.role in {Role.ADMIN.value, Role.INSTRUCTOR.value}:
            return
        labs = db.scalars(
            select(Lab).where(Lab.template_id == template.id)
        ).all()
        for lab in labs:
            used = self.course_usage_seconds(db, user.id, lab)
            if used >= lab.quota_seconds:
                raise BillingError(
                    f"course quota exhausted: {used}/{lab.quota_seconds}s used "
                    f"(lab {lab.id[:8]})"
                )

    def course_usage_seconds(self, db: Session, user_id: str, lab: Lab) -> int:
        """该学生在 lab.template_id 下所有（非 tombstone）workspace 的累计 GPU 秒数。

        含运行中 workspace 的 live 秒数（配额必须真实执行，不能只算已结算段），但
        **只含还没进账本的那一段**：口径与展示端同源，都在 `workspace_seconds_used`
        里（N-64 的投影 + 未入账 live）。QUOTA 门禁（`check_launch_eligible` 的
        `used >= quota_seconds`）与配额监控的停机判定读的就是这个数，所以"结算已落库
        而 `started_at` 未清"的窗口一度把它读成账本的两倍。
        """
        workspaces = list(
            db.scalars(
                select(Workspace).where(
                    Workspace.user_id == user_id,
                    Workspace.template_id == lab.template_id,
                    Workspace.deleted_at.is_(None),
                )
            )
        )
        now = datetime.now(UTC)
        # 一段 `IN` 查询拿回这批 workspace 的 USAGE 幂等键，逐 workspace 只查内存集合
        booked_keys = booked_usage_keys(
            db,
            [w.id for w in workspaces if w.status == WorkspaceStatus.RUNNING.value],
        )
        return sum(
            workspace_seconds_used(db, w, now=now, booked_keys=booked_keys)
            for w in workspaces
        )

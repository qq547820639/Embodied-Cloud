"""BillingPolicy：workspace launch 前的额度/配额门禁（§17）。

- 个人 credits / 组织 credits：有效余额（个人+组织）为负 → 拒绝启动
- course quota_seconds：学生在 lab 的已用 GPU 秒数 ≥ 配额 → 拒绝
- admin override：admin 跳过门禁
- 结算本身保持 append-only CreditLedger + 幂等 key（restart-safe、无双重扣费）
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..models import Lab, Role, Template, User, Workspace, WorkspaceStatus
from .ledger import CreditLedgerService


class BillingError(RuntimeError):
    """额度不足/配额用尽；路由层映射为 HTTP 402。"""


class BillingPolicy:
    def __init__(self, session_factory: sessionmaker[Session], ledger: CreditLedgerService | None = None):
        self.session_factory = session_factory
        self.ledger = ledger or CreditLedgerService(session_factory)

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

        # 1) 有效余额（个人 + 组织）为负 → 拒绝
        personal = self.ledger.balance(db, user.id)
        org = self.ledger.organization_balance(db, user.organization_id) if user.organization_id else 0
        if personal + org < 0:
            raise BillingError(
                f"insufficient credits: personal={personal}, organization={org} "
                f"(workspace {template.id})"
            )

        # 2) course quota：学生在 lab 的实际用量（秒）≥ 配额 → 拒绝
        if lab is not None:
            used = self.course_usage_seconds(db, user.id, lab)
            if used >= lab.quota_seconds:
                raise BillingError(
                    f"course quota exhausted: {used}/{lab.quota_seconds}s used "
                    f"(lab {lab.id[:8]})"
                )

    def course_usage_seconds(self, db: Session, user_id: str, lab: Lab) -> int:
        """该学生在 lab.template_id 下所有（非 tombstone）workspace 的累计 GPU 秒数。

        含运行中 workspace 的 live 秒数（配额必须真实执行，不能只算已结算段）。
        """
        workspaces = db.scalars(
            select(Workspace).where(
                Workspace.user_id == user_id,
                Workspace.template_id == lab.template_id,
                Workspace.deleted_at.is_(None),
            )
        )
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        total = 0
        for w in workspaces:
            total += w.accumulated_seconds or 0
            if w.status == WorkspaceStatus.RUNNING.value and w.started_at:
                started = w.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                total += max(0, int((now - started).total_seconds()))
        return total

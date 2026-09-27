"""不可变 Credit Ledger：append-only 账本 + 幂等结算。

原则（见 docs/PRODUCT_SPEC.md / SECURITY.md T4）：
- 永不 UPDATE/DELETE 已入账 transaction；balance 始终 = SUM(amount)。
- 每笔交易带唯一 idempotency_key；重放不会产生重复交易。
- GPU 计费单位：实际运行秒数；同一 workspace 的同一运行段只结算一次。
- `workspaces.accumulated_seconds` 与 balance 同理，是 SUM(gpu_seconds) 的投影
  （`settled_gpu_seconds`），不由写侧累加维护：幂等键只保证同一运行段不重复扣款，
  重复的那次 `+=` 照样会让计数器比账本大。
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import CreditLedger, LedgerType, Workspace


class LedgerError(RuntimeError):
    pass


class CreditLedgerService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def record(
        self,
        db: Session,
        *,
        type: LedgerType | str,
        amount: int,
        user_id: str | None = None,
        organization_id: str | None = None,
        description: str = "",
        workspace_id: str | None = None,
        template_id: str | None = None,
        gpu_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> CreditLedger:
        """追加一笔交易。幂等：idempotency_key 已存在则返回已有记录，不重复入账。"""
        key = idempotency_key or f"{type}:{uuid.uuid4()}"
        existing = db.scalar(select(CreditLedger).where(CreditLedger.idempotency_key == key))
        if existing is not None:
            return existing
        entry = CreditLedger(
            id=str(uuid.uuid4()),
            user_id=user_id,
            organization_id=organization_id,
            type=str(type),
            amount=amount,
            description=description,
            workspace_id=workspace_id,
            template_id=template_id,
            gpu_seconds=gpu_seconds,
            idempotency_key=key,
        )
        db.add(entry)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(select(CreditLedger).where(CreditLedger.idempotency_key == key))
            if existing is not None:
                return existing
            raise
        return entry

    def balance(self, db: Session, user_id: str) -> int:
        total = db.scalar(
            select(func.coalesce(func.sum(CreditLedger.amount), 0)).where(
                CreditLedger.user_id == user_id
            )
        )
        return int(total or 0)

    def organization_balance(self, db: Session, organization_id: str) -> int:
        """组织池余额：**只数没有个人归属的行**（`user_id IS NULL`）。

        账本行同时带 `user_id` 与 `organization_id` 是常态（`settle_workspace_run` 与
        充值/调整都两个一起写），所以"按 organization_id 求和"会把成员的个人行再数一遍：
        一个人的充值被算进同组织其他人的可用额（实测：给 a1 充 1000 ⇒ a1 可用 2000、
        从未出钱的 b1 可用 1000）。分池口径与 `test_credit_holds.py` 里"组织账户补足
        可用额"那条既有意图一致——组织自己的入账本来就只带 `organization_id`。
        个人池因此是"该用户的全部行"，两池不相交。
        """
        total = db.scalar(
            select(func.coalesce(func.sum(CreditLedger.amount), 0)).where(
                CreditLedger.organization_id == organization_id,
                CreditLedger.user_id.is_(None),
            )
        )
        return int(total or 0)

    def settled_gpu_seconds(self, db: Session, workspace_id: str) -> int:
        """该 workspace 已入账的 GPU 秒数：SUM(gpu_seconds)，只数 USAGE 行。

        `workspaces.accumulated_seconds` 由它派生而不是累加（N-64）。本方法是账本
        这一侧唯一的读数口径，`settle_workspace_run` 是唯一的 USAGE 写入方。
        """
        total = db.scalar(
            select(func.coalesce(func.sum(CreditLedger.gpu_seconds), 0)).where(
                CreditLedger.workspace_id == workspace_id,
                CreditLedger.type == str(LedgerType.USAGE),
            )
        )
        return int(total or 0)

    def settle_workspace_run(
        self,
        db: Session,
        workspace: Workspace,
        seconds: int,
        started_at_iso: str,
    ) -> CreditLedger | None:
        """结算一次运行段的 GPU 秒数（幂等）。

        idempotency_key = f"usage:{workspace.id}:{started_at_iso}" —— 同一运行段
        重复结算（进程崩溃重放）只会命中已有记录，不会重复扣款。
        """
        if seconds <= 0:
            return None
        # 1 秒 = 1 credit（展示口径；金额计算不依赖模板费率，未来若引入按费换算再扩展）
        amount = -seconds
        return self.record(
            db,
            type=LedgerType.USAGE,
            amount=amount,
            user_id=workspace.user_id,
            organization_id=workspace.organization_id,
            description=f"GPU usage {seconds}s @ 1 credit/second",
            workspace_id=workspace.id,
            template_id=workspace.template_id,
            gpu_seconds=seconds,
            idempotency_key=f"usage:{workspace.id}:{started_at_iso}",
        )

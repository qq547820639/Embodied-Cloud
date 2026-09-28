"""不可变 Credit Ledger：append-only 账本 + 幂等结算。

原则（见 docs/PRODUCT_SPEC.md / SECURITY.md T4）：
- 永不 UPDATE/DELETE 已入账 transaction；balance 始终 = SUM(amount)。
- 每笔交易带唯一 idempotency_key；重放不会产生重复交易。
- GPU 计费单位：实际运行秒数；同一 workspace 的同一运行段只结算一次。
- `workspaces.accumulated_seconds` 与 balance 同理，是 SUM(gpu_seconds) 的投影
  （`settled_gpu_seconds`），不由写侧累加维护：幂等键只保证同一运行段不重复扣款，
  重复的那次 `+=` 照样会让计数器比账本大。
- 投影之外还活着的只有**当前这一段**：它的身份是 `usage_idempotency_key` 那把键，
  入账了就不再往外报 live 秒（`workspace_seconds_used` 是全仓唯一的"已用秒数"口径，
  展示端与 QUOTA 门禁同源）。一个运行段被写侧与读侧各认一次的窗口是真实存在的
  （`destroy()` 先结算、`provider.destroy` 抛错上抛给 DESTROY 重试，状态仍是 RUNNING
  且 `started_at` 未清），那时候两头加就是双计。
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import CreditLedger, LedgerType, Workspace, WorkspaceStatus


class LedgerError(RuntimeError):
    pass


def usage_idempotency_key(workspace_id: str, started_at_iso: str) -> str:
    """USAGE 行幂等键的**唯一模板**（全仓只这一处构造 `usage:` 串）。

    一个运行段的身份就是这把键：`started_at_iso` 一律取 `workspace.started_at` 的
    **原始** `isoformat()`，两边（写侧 `settle_workspace_run` 与读侧
    `usage_segment_booked`）都必须原样传，绝不做时区归一 —— 落库回读的 SQLite
    DATETIME 不带偏移（`…T02:41:14.397612`），归一过的串会写成另一把键，读侧就
    永远点不中自己那一段（键不匹配 = "没入账"，live 照加，双计原样留在）。
    """
    return f"usage:{workspace_id}:{started_at_iso}"


def usage_segment_booked(
    db: Session,
    workspace_id: str,
    started_at: datetime | None,
    *,
    booked_keys: set[str] | None = None,
) -> bool:
    """当前这一段运行（`started_at`）是否已经写进账本。

    写侧无 `started_at` 直接 False；`booked_keys` 是由 `booked_usage_keys()` 一次
    `workspace_id IN (…)` 批量取回的既有 USAGE 键集合（列表端点用它避免 N+1），
    不给时退化为对唯一键 `idempotency_key` 的点查。
    """
    if started_at is None:
        return False
    key = usage_idempotency_key(workspace_id, started_at.isoformat())
    if booked_keys is not None:
        return key in booked_keys
    return (
        db.scalar(select(CreditLedger.id).where(CreditLedger.idempotency_key == key))
        is not None
    )


def booked_usage_keys(db: Session, workspace_ids: Iterable[str]) -> set[str]:
    """一次查询取回这批 workspace 的全部 USAGE 幂等键（不逐行查，一个 `IN` 一句）。"""
    ids = [wid for wid in workspace_ids if wid]
    if not ids:
        return set()
    rows = db.scalars(
        select(CreditLedger.idempotency_key).where(
            CreditLedger.workspace_id.in_(ids),
            CreditLedger.type == str(LedgerType.USAGE),
        )
    )
    return set(rows)


def workspace_seconds_used(
    db: Session,
    workspace: Workspace,
    *,
    now: datetime | None = None,
    booked_keys: set[str] | None = None,
) -> int:
    """某个 workspace 的"已用 GPU 秒"唯一口径：账本投影 + **未入账**的 live 段。

    `workspaces.accumulated_seconds` 是账本的投影（N-64），每次结算后被 SET 成
    `settled_gpu_seconds()`；运行中的那一段还没进投影，所以要加 live 项——但**只在该
    段确实还没入账时**加。否则"已结算但没收尾"的窗口（`destroy()` 先结算、
    `provider.destroy` 抛错上抛让 DESTROY 重试，状态留 RUNNING、`started_at` 不清零）
    会把同一段计两次：投影里 30 + live 又 30 = 60（实测配额读到 60、账本 SUM 只有 30）。

    live 的准入条件全仓只这一处：`status == RUNNING` 且 `started_at is not None`
    且本段未入账。两个既有读者（`app/routers/usage.py` 展示/估价、
    `BillingPolicy.course_usage_seconds` QUOTA 门禁与配额监控的停机判定）此前各自
    抄了一遍同一条表达式，改完它们都调这里。

    亚秒段（`elapsed < 1` → `settle_workspace_run` 对 `seconds <= 0` 返回 None）不写
    任何 USAGE 行，所以"有没有那一行"就是正确的谓词：这种段确实没入账，live 照加。
    """
    total = int(workspace.accumulated_seconds or 0)
    if (
        workspace.status == WorkspaceStatus.RUNNING.value
        and workspace.started_at is not None
        and not usage_segment_booked(
            db, workspace.id, workspace.started_at, booked_keys=booked_keys
        )
    ):
        started = workspace.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        reference = now if now is not None else datetime.now(UTC)
        total += max(0, int((reference - started).total_seconds()))
    return total


def mark_usage_segments(db: Session, workspaces: Iterable[Workspace]) -> list[Workspace]:
    """给每一行打上「当前这一段已进账本」，供 `WorkspaceOut.usage_segment_booked` 序列化。

    这一句是全仓唯一的书写点：读者是**前端**（`app/static/app.js` 的 `accumulated_seconds
    + live`，为了让数字在两秒一轮的轮询里继续跳）。服务端口径（`workspace_seconds_used`）
    管不到它，所以把谓词本身发过去，而不是让前端再猜一次。

    属性不是模型列，不会被 flush；列表端点用一条 `IN` 取回键集合，逐行只查内存。
    """
    rows = list(workspaces)
    keys = booked_usage_keys(
        db, [w.id for w in rows if w.status == WorkspaceStatus.RUNNING.value]
    )
    for w in rows:
        w.usage_segment_booked = w.status == WorkspaceStatus.RUNNING.value and (
            usage_segment_booked(db, w.id, w.started_at, booked_keys=keys)
        )
    return rows


def mark_usage_segment(db: Session, workspace: Workspace) -> Workspace:
    """单行版：打标记后原样返回，路由里写 `return mark_usage_segment(db, ws)` 即可。"""
    return mark_usage_segments(db, [workspace])[0]


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

    def segment_booked(
        self,
        db: Session,
        workspace_id: str,
        started_at: datetime | None,
        *,
        booked_keys: set[str] | None = None,
    ) -> bool:
        """该 workspace 当前这一段运行（`started_at`）是否已进账本。

        读者侧唯一的键组合点（除写侧 `settle_workspace_run` 之外），实现转给模块级
        `usage_segment_booked`；`started_at is None` → False。
        """
        return usage_segment_booked(
            db, workspace_id, started_at, booked_keys=booked_keys
        )

    def settle_workspace_run(
        self,
        db: Session,
        workspace: Workspace,
        seconds: int,
        started_at_iso: str,
    ) -> CreditLedger | None:
        """结算一次运行段的 GPU 秒数（幂等）。

        idempotency_key 由 `usage_idempotency_key(workspace.id, started_at_iso)` 给出
        —— 同一运行段重复结算（进程崩溃重放）只会命中已有记录，不会重复扣款。
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
            idempotency_key=usage_idempotency_key(workspace.id, started_at_iso),
        )

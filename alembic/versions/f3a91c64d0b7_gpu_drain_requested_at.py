"""gpus 增加 drain_requested_at：把「管理员要摘走这张卡」从 status 里分出来

本仓把两个问题挤在同一列：`gpus.status` 既回答「这张卡被占了没有」（`app/main.py:36`
的占用计数、`app/services/scheduler.py` 的候选筛选与释放路径都读它），又回答「管理员
有没有把它从池子里摘走」。一个列容不下两个主人：`POST /api/gpus/{id}/drain` 要写
「摘走」就必然覆盖「占用」，于是 N-123 只能把它限定为只作用于池子里的卡，占用中的卡
一律回 409（N-124）。分栏按 K8s 的 `spec.unschedulable` 与 `.status` 分工——意图单独
立一列，观察值留在 `status`：非 NULL 即「管理员已提出 drain 要求」这一事实的凭证，
释放路径等工作跑完再把它落成 `status=drained`。

列做成 nullable 而不是 `NOT NULL DEFAULT now()`：迁移那一刻给存量行填上时间戳，
等于宣称管理员曾要求把每一张在册的卡都摘走——那是凭空造出一批从未发生过的判决，
而且这个判决还带日期、带出处，事后无从与真实请求区分。留 NULL 就是「没人提过」，
第一次真 drain 自然盖上章。SQLite/PostgreSQL 两侧都用 `batch_alter_table`，
与 ADR 0005（`docs/adr/0005-sqlite-postgres-semantic-parity.md`）的语义对等约定一致。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a91c64d0b7"
down_revision: str | None = "4f2b7c9a1e60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("gpus") as batch_op:
        batch_op.add_column(
            sa.Column("drain_requested_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("gpus") as batch_op:
        batch_op.drop_column("drain_requested_at")

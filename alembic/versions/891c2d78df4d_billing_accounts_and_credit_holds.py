"""billing accounts and credit holds

§17 计费主体（billing_accounts）+ §18 启动预授权（credit_holds）。

- billing_accounts 是"谁付钱"的单一入口，也是预授权前的锁根；账本仍按
  user_id/organization_id 记历史事实，本迁移不改写任何历史行。
- credit_holds 可变（pending→captured/released），并带一个双方言部分唯一索引
  uq_holds_pending_per_workspace：并发双启动只能圈住一次额度。
- 回填：为现存 users / organizations 各建一个账户行（幂等键 = subject 唯一约束）。
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "891c2d78df4d"
down_revision: str | None = "d83ea2eda568"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "billing_accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_billing_accounts_owner_user"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject_type", "subject_id", name="uq_billing_account_subject"),
    )
    op.create_index(
        op.f("ix_billing_accounts_owner_user_id"), "billing_accounts", ["owner_user_id"], unique=False
    )

    op.create_table(
        "credit_holds",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("captured_amount", sa.Integer(), nullable=True),
        sa.Column("ledger_usage_key", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["billing_accounts.id"], name="fk_credit_holds_account"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_credit_holds_idempotency"),
    )
    op.create_index(op.f("ix_credit_holds_account_id"), "credit_holds", ["account_id"], unique=False)
    op.create_index(op.f("ix_credit_holds_workspace_id"), "credit_holds", ["workspace_id"], unique=False)
    op.create_index("ix_holds_account_status", "credit_holds", ["account_id", "status"], unique=False)
    # 一个 workspace 至多一个 pending hold（双方言部分唯一索引）
    # 一次调用带两个方言谓词：分开写会让"只认 postgresql_where"的那次在
    # SQLite 上建成非部分索引（实测报 index already exists）
    op.create_index(
        "uq_holds_pending_per_workspace",
        "credit_holds",
        ["workspace_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )

    # 回填计费主体：现存用户 + 组织
    bind = op.get_bind()
    now = sa.text("CURRENT_TIMESTAMP")
    for table, subject_type in (("users", "user"), ("organizations", "organization")):
        rows = bind.execute(sa.text(f"SELECT id FROM {table}")).fetchall()  # noqa: S608 表名来自本迁移常量
        for (subject_id,) in rows:
            bind.execute(
                sa.text(
                    "INSERT INTO billing_accounts (id, subject_type, subject_id, owner_user_id, created_at) "
                    "VALUES (:id, :t, :s, :o, :c)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "t": subject_type,
                    "s": subject_id,
                    "o": subject_id if subject_type == "user" else None,
                    "c": now.text,
                },
            )


def downgrade() -> None:
    for name, table in (
        ("uq_holds_pending_per_workspace", "credit_holds"),
        ("ix_holds_account_status", "credit_holds"),
        ("ix_credit_holds_workspace_id", "credit_holds"),
        ("ix_credit_holds_account_id", "credit_holds"),
        ("ix_billing_accounts_owner_user_id", "billing_accounts"),
    ):
        op.drop_index(name, table_name=table)
    op.drop_table("credit_holds")
    op.drop_table("billing_accounts")

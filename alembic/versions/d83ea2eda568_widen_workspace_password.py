"""widen workspaces.password + verify courses.slug uniqueness

Revision ID: d83ea2eda568
Revises: 87c6c7d2d168
Create Date: 2026-08-14 02:00:00.000000

变更说明：
- workspaces.password 由 String(128) 放宽到 String(512)：Fernet 密文对较长明文
  密码会超过 128 字符（PostgreSQL VARCHAR 截断报错）。
- courses.slug 的全局唯一约束已在初始 schema（572b9ffa9375，`sa.UniqueConstraint('slug')`）
  中建立（SQLite 落地为 sqlite_autoindex_courses_1）；`create_course` 的并发兜底
  已改为捕获 IntegrityError → 409，因此本迁移无需（也不应）重复新增 slug 约束。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd83ea2eda568'
down_revision: Union[str, None] = '87c6c7d2d168'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite 改列宽需 batch_alter_table（table recreate）模式；PostgreSQL 走原生
    # ALTER COLUMN TYPE。env.py 未全局开启 render_as_batch，故在此显式 batch。
    with op.batch_alter_table('workspaces') as batch_op:
        batch_op.alter_column(
            'password',
            existing_type=sa.String(length=128),
            type_=sa.String(length=512),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table('workspaces') as batch_op:
        batch_op.alter_column(
            'password',
            existing_type=sa.String(length=512),
            type_=sa.String(length=128),
            existing_nullable=True,
        )

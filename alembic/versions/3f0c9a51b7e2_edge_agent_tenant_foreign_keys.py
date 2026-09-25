"""补建 edge_agents 的租户外键

`c7c6f510d21f` 为 §6（P0）租户所有权给 edge_agents 加了 owner_user_id /
organization_id 两列，`app/models.py` 同时声明了 ForeignKey，但迁移里从来没有创建
这两个约束 —— 于是 SQLite 与 PostgreSQL 的实际 schema 里都不存在它们：跨租户归属
只是一条"看起来有约束"的应用层约定，任何一条脏外键（用户删了、组织改挂）都能静默
写进去。

本迁移只补约束、不动列、不改数据。SQLite 无法 ALTER 加约束，走 batch recreate。

注意（运维）：如果既有库里已经存在指向已删除 user/organization 的 edge_agents 行，
PostgreSQL 侧 ADD CONSTRAINT 会校验存量并失败 —— 这是有意让脏数据显形，
处理方式是先修数据再升级。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "3f0c9a51b7e2"
down_revision: str | None = "891c2d78df4d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("edge_agents") as batch_op:
        batch_op.create_foreign_key(
            "fk_edge_agents_owner_user_id", "users", ["owner_user_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_edge_agents_organization_id", "organizations", ["organization_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("edge_agents") as batch_op:
        batch_op.drop_constraint("fk_edge_agents_owner_user_id", type_="foreignkey")
        batch_op.drop_constraint("fk_edge_agents_organization_id", type_="foreignkey")

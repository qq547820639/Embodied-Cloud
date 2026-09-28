"""gpu_hosts 增加 last_synced_at：给 `status="online"` 一个证据列

`GpuHost.status` 的默认值是 `online`，而全仓唯一的写入点是
`GpuScheduler.sync_host`（`app/services/scheduler.py:102`），它又只在开机
`bootstrap_gpu_inventory` 时被调用一次（`app/deps.py:179`）。于是"这台主机在线"
在进程的一生里再没有被重新证明过：节点离开集群之后，`GET /api/gpus/hosts` 仍对
管理员报 online，而 `sync_host` 里那段"未再上报的 GPU → DRAINING"的收敛也没有
任何驱动者（与本仓 N-93/N-98 的「有收敛、无驱动者」和 N-108 的「存在性主张没有
事实背书」同形）。

列做成 nullable 而不是 `NOT NULL DEFAULT now()`：迁移那一刻给存量行填一个假时间戳，
等于把所有历史主机同时判成"刚刚同步过"，把窗口拉长一倍以上；留 NULL 则由
`expire_stale_hosts` 明确跳过（从没同步过 ⇒ 无从判失效），下一次真同步自然盖上章。
SQLite/PostgreSQL 两侧都用 `batch_alter_table`，与 ADR 0005 的语义对等约定一致。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4f2b7c9a1e60"
down_revision: str | None = "b7e4c1a09f52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("gpu_hosts") as batch_op:
        batch_op.add_column(
            sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("gpu_hosts") as batch_op:
        batch_op.drop_column("last_synced_at")

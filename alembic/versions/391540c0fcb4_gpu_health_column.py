"""gpus 增加 health 列：把「这张卡被判定不健康」从 status 里分出来

`gpus.status` 此前要同时回答两个问题：「这张卡被占了没有」（`app/main.py:34-37` 的
`gpu_allocated` 指标就数 `allocated` 这一个值）与「这张卡被判定不健康没有」。一列容不下
两个主人：`mark_unhealthy` 没有状态前置，管理员对一张**正在被使用**的卡点 `/unhealthy`，
`status` 就从 `allocated` 变成 `unhealthy`——占用事实当场在 `gpus` 这一侧消失，而那一格的
`workspaces.gpu_id` 仍指着它、`gpus.workspace_id` 也仍指着那一格，两张表各说各话。这就是
登记项 N-126：N-125 那一味药（把人的判决从状态列里搬出去，`gpus.drain_requested_at`）只
喂给了 drain，没喂给 health。本迁移按 ADR 0010（
`docs/adr/0010-gpu-status-is-three-dimensions.md`）§3 定下的形状落地：`status` 从此只答
调度可用性（available / allocated / draining / drained），健康单独一列，「在用」与
「被判定坏」这两件事才可能第一次同时为真。

列做成 nullable 而不是 `NOT NULL DEFAULT 'healthy'`：迁移那一刻给存量行回填 'healthy'，
等于宣称管理员曾把每一张在册的卡都判成健康——那是凭空造出一批从未发生过的判决，事后
无从与真实请求区分。留 NULL 就是「从没人判过这张卡的健康」，`healthy` 则是「管理员亲口
判过、且判成了健康」（只有 `POST /api/gpus/{gpu_id}/healthy` 这条人的路径写得进去，
ADR 0010 §3 第 4 条）。分配器的谓词是 `health != unhealthy`（ADR 0010 §3 第 3 条，与容量
余量同一条谓词），NULL 与 'healthy' 在它眼里同样可分配：两者的差别是证据，不是行为。
回填抹掉的正是这个差别。

`upgrade` 把存量 `status='unhealthy'` 的行改写成 `status='drained'`＋`health='unhealthy'`：
`unhealthy` 已不再是 `GpuStatus` 的取值（见 `app/models.py` 的 `GpuStatus` 与 `GpuHealth`
两个类），留在 `status` 里就是一个枚举中不存在的值被读侧原样透出去。挑 `drained` 承接，
是因为它是当前四个取值里唯一表达「这张卡不为调度所用，而且这是一个人的判决」的那一个——
正是这些行原本想说的事；真实理由（不健康）由 `health` 列继续带着，改写不丢判决。
`downgrade` 先把 `health='unhealthy'` 的行抬回 `status='unhealthy'`、再删索引与列，两次
翻译都以 `health` 为准，不依赖被改写过的行自己记得原来的值。

索引 `ix_gpus_health` 与 `ix_gpus_status` 同形（ADR 0010 §3 第 1 条）。SQLite/PostgreSQL
两侧都用 `batch_alter_table`，与 ADR 0005
（`docs/adr/0005-sqlite-postgres-semantic-parity.md`）的语义对等约定一致。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "391540c0fcb4"
down_revision: str | None = "f3a91c64d0b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("gpus") as batch_op:
        batch_op.add_column(sa.Column("health", sa.String(length=16), nullable=True))
        batch_op.create_index("ix_gpus_health", ["health"], unique=False)
    # 存量判决翻译：unhealthy 不再是 status 的取值（GpuStatus 只有四个值），
    # 把它挪进 health，status 侧由 drained 承接「不为调度所用＋人的判决」。
    op.execute("UPDATE gpus SET status = 'drained', health = 'unhealthy' WHERE status = 'unhealthy'")


def downgrade() -> None:
    # 反向翻译先行：以 health 列为准抬回 unhealthy，删列之后就再也读不到这个判决了。
    op.execute("UPDATE gpus SET status = 'unhealthy' WHERE health = 'unhealthy'")
    with op.batch_alter_table("gpus") as batch_op:
        batch_op.drop_index("ix_gpus_health")
        batch_op.drop_column("health")

"""deployments 增加运行时长预算与起跑时刻（N-134）

`running` 此前只有一种死法：设备自己回来报结果（N-113 的 `complete_from_agent_report`），
或者设备被判离线时由那趟 sweep 顺带收口（N-133 的 `expire_stale_agents`）。后者用的是
一个全局数——`edge_agent_offline_after_seconds`，默认 90 s——它量的是"这台设备最后一次
心跳距今多久"，而运行需要的是一个属于自己的数："这条运行开了多久"。两个不同的事实共用
一个数，结果双向都错：一次合法要跑 20 分钟的巡检会被 90 s 静默判死，而一次 5 秒的推理
拿到 90 s 几乎等于不设防。登记项 N-134 要补的就是这一格。

形状借 K8s Job：`spec.activeDeadlineSeconds` 是**每个 Job 一列**、相对它自己的
`.status.startTime` 量，超时的运行判成一次已结束的失败（本机装的 kubernetes client
31.0.0 的字段注释原文见 `app/services/deployment.py` 里 `fail_overdue_runs` 的说明段）。
本迁移落对应的两列：`run_deadline_seconds` 是这条部署自己的预算，`run_started_at` 是它的
startTime——由 `run_policy` 在 verified → running 那一刻盖章，`updated_at` 不能替它，
因为 `updated_at` 带 `onupdate`，任何一次与运行无关的写都会把它顶新，等于把重置时钟的
权力交给所有写者。

两列都 nullable 而不是 `0`／`now` 回填：
- `run_deadline_seconds IS NULL` 是"没人给这条运行定过预算"，控制面因此**不判**它。
  给个默认值等于替存量与新建的每一次部署做一个人没做过的决定，那才是凭空造判决。
- `run_started_at IS NULL` 是"这行还没进过 running"，以及迁移之前已经在 running 的存量行
  （它们没有盖章时刻）。回填一个猜测的时刻——哪怕是 `updated_at`——会让一条本该
  "无证据、不判"的行变成"有证据、被杀死"或"有证据、永远安全"，两种都误导运维。
  ADR 0008 的口径同样适用：未知不等于缺席，这里也不等于超时。
代价如实记下：存量那几条已经 `running` 的行在本迁移之后仍然没有预算读者，它们仍要等
N-133 的设备判活或人来 `/complete` 收口；新的 running 才有这条通路。

不做索引：判据先按 `status == running` 选候选，而 `deployments.status` 上并没有独立的
选择性可言（活跃行本就少），逐行的预算换算在 Python 侧做（两侧方言的"时间加秒"写法不同：
SQLite 的 `datetime(col,'+N seconds')` 对 PostgreSQL 的 `col + make_interval(secs => N)`，
而 ADR 0005 要求这条通路两侧语义对等）。将来运行表大了再按 `status`＋`run_started_at`
补一条复合索引，那时才有可证的读形状。

`downgrade` 直接删列：这两列只被控制面的时效判决读，删掉之后 `fail_overdue_runs` 不再
可能成立，没有需要翻译回来的判决。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7d41e9f2c63"
down_revision: str | None = "391540c0fcb4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("deployments") as batch_op:
        batch_op.add_column(sa.Column("run_deadline_seconds", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("run_started_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("deployments") as batch_op:
        batch_op.drop_column("run_started_at")
        batch_op.drop_column("run_deadline_seconds")

"""补齐 12 处"真指针"外键

ADR 0005 把引用完整性分成两类：`c7c6f510d21f` 那种"模型声明了、迁移没建"是缺陷（已由
`3f0c9a51b7e2` 修复并被对账门盯住）；本迁移处理另一类——**列里存的是别的表的主键，
但模型从来没用 ForeignKey 声明**。

判定依据（本轮实测，不是猜）：
- 全仓删除能力普查（AST + 裸 SQL `DELETE FROM` 三种形态）：唯一的硬删路径是
  `GpuAllocation`（scheduler 释放分配），而没有任何表按 id 引用它。
- 其余被引用的表（workspaces / gpus / gpu_hosts / template_versions / artifacts /
  edge_agents / organizations / users）只有软删（workspace tombstone）或压根不删。
  → 这些指针加约束不会挡任何现存流程，只挡脏写。

有意**不加**的同类列（写清理由，避免下轮又当成漏网）：
- `credit_ledger.workspace_id` / `credit_ledger.template_id`：账本是审计事实，模板下线、
  workspace 归档后仍必须能还原"当时扣了多少、按什么价"；且 template_id 存的是 slug
  而非主键。
- `billing_accounts.subject_id`：多态（user 或 organization），没有单一目标表。
- `templates.current_version_id`：与 `template_versions.template_id` 互为回指，加 FK
  会把两表的建/删顺序锁成环；该不变量由 `release_version` 的代码路径与
  `test_template_versions.py` 常驻用例守。
- `workspaces.template_id` / `labs.template_id`：同样存 slug（非主键），要加 FK 得先给
  `templates.slug` 上唯一约束并改列语义，属另一项设计裁决。
- `workspaces.gpu_id`：与 `gpus.workspace_id` 互为回指。两边都上外键会让
  `compare_metadata` 报 "unresolvable cycles between gpus, workspaces" 并
  **静默跳过该环内的全部外键比较**（对账门当场变瞎）。因此只保留
  `gpus.workspace_id`（调度器的分配权威记录）一条方向。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7e4c1a09f52"
down_revision: str | None = "3f0c9a51b7e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (表, 列, 目标表.目标列, 约束名)
FKS: list[tuple[str, str, str, str]] = [
    ("gpu_allocations", "workspace_id", "workspaces.id", "fk_gpuallocation_workspace_id"),
    ("gpu_allocations", "host_id", "gpu_hosts.id", "fk_gpuallocation_host_id"),
    ("gpus", "workspace_id", "workspaces.id", "fk_gpu_workspace_id"),
    ("credit_holds", "workspace_id", "workspaces.id", "fk_credithold_workspace_id"),
    ("deployments", "workspace_id", "workspaces.id", "fk_deploymentrecord_workspace_id"),
    ("deployments", "edge_agent_id", "edge_agents.id", "fk_deploymentrecord_edge_agent_id"),
    ("telemetry_events", "edge_agent_id", "edge_agents.id", "fk_telemetryevent_edge_agent_id"),
    ("workspaces", "template_version_id", "template_versions.id", "fk_workspace_template_version_id"),
    ("artifacts", "workspace_id", "workspaces.id", "fk_artifact_workspace_id"),
    ("streaming_sessions", "workspace_id", "workspaces.id", "fk_streamingsession_workspace_id"),
    ("submissions", "workspace_id", "workspaces.id", "fk_submission_workspace_id"),
]


def upgrade() -> None:
    for table in dict.fromkeys(t for t, *_ in FKS):  # 去重且保序：每表只 batch 一次
        with op.batch_alter_table(table) as batch_op:
            for t, col, ref, name in FKS:
                if t != table:
                    continue
                target_table, target_col = ref.split(".")
                batch_op.create_foreign_key(name, target_table, [col], [target_col])


def downgrade() -> None:
    for table in dict.fromkeys(t for t, *_ in FKS):
        with op.batch_alter_table(table) as batch_op:
            for t, _col, _ref, name in FKS:
                if t == table:
                    batch_op.drop_constraint(name, type_="foreignkey")

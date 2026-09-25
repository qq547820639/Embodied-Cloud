# ADR 0005: SQLite 与 PostgreSQL 的语义差 —— 补齐能补的，把补不了的钉成断言

状态：Accepted（2026-09-26）

## 背景

开发/CI 默认档是 SQLite，生产是 PostgreSQL。三类差异会让"本地全绿"和生产行为脱节：

1. **行锁**：`FOR UPDATE` / `SKIP LOCKED` 在 SQLite 方言里被整个丢弃。实测证据是
   `tests/test_sqlite_semantic_baseline.py` 编译同一条语句比对两方言渲染结果 —— SQLite
   输出里没有 `FOR UPDATE`，PostgreSQL 输出里 `FOR UPDATE SKIP LOCKED` 都在。
2. **外键校验**：SQLite 的开关是**每连接**的，默认关闭。本仓实测：`make_engine` 之前
   `PRAGMA foreign_keys` 读到 0，一张 `host_id` 指向不存在主机的 GPU 行可以写进去，
   并被调度器当作候选选中。
3. **模型声明 ≠ 迁移产物**：`c7c6f510d21f` 给 `edge_agents` 加了
   `owner_user_id` / `organization_id` 两列，`app/models.py` 声明了 `ForeignKey`，
   但**迁移从未创建这两个约束**。于是它们在 SQLite 和 PostgreSQL 上都不存在。
   `docs/review/architecture-walkthrough-2026-08-13.md` 第 248 行当时的判断是
   "PostgreSQL 由 autogenerate 原生支持" —— 该判断没有落进迁移链，因此不成立：
   生产 schema 由迁移链决定，不是由模型决定。

## 决策

1. `make_engine`（`app/db.py`）对 SQLite 连接挂 `connect` 钩子执行
   `PRAGMA foreign_keys=ON`。必须逐连接，因为是 per-connection 开关。
2. **不假装 SQLite 能提供行锁**。`GpuScheduler.allocate` 与预授权的锁语义只在
   `pg_integration` 档验证；默认档改为钉住"这个保证在当前库上不存在"这件事本身
   （第 1 条基线用例），这样一旦方言或配置变化会立刻可见，而不是悄悄失效。
3. 第 3 类差异由对账门兜住：`tests/test_migrations.py` 用 Alembic 自己的
   `compare_metadata` 把"迁移产出的库"与 `Base.metadata` 逐项对齐，非空即红。
   判据不来自手抄清单，因此新增列/约束/索引忘了写迁移会直接暴露。
   `3f0c9a51b7e2` 是该门补上的第一批约束（SQLite 走 `batch_alter_table` recreate）。
4. 文档口径：**生产只支持 PostgreSQL**，SQLite 定位为开发/演示库。

## 后果

- 打开外键校验后，"先写子行、后提交父行"的写法会红：本轮先红的是测试夹具里的
  seed（插入 GPU 时其 host 尚未提交），修正为父行先提交。生产写序另判：
  `scheduler.sync_host` 本来就用 `db.flush()` 把父行落在子行之前，无缺陷，
  该事实由 `test_scheduler_seed_order_works_under_enforced_fk` 钉住（去掉那次
  flush 即红）。
- 只跑默认档得到的任何"并发已验证"结论都不成立；涉及 `FOR UPDATE` 的改动必须跑
  `make test-pg`。
- 存量库里若已有指向已删除 user/organization 的 `edge_agents` 行，`3f0c9a51b7e2`
  在 PostgreSQL 侧会因校验存量而失败 —— 有意让脏数据显形，先修数据再升级。
- 对账门跑在 SQLite 上（默认档，无需 docker）：`compare_metadata` 能读出 SQLite 表
  定义里的 FK，因此第 3 类漂移在 CI 主线上就可拦，不必等 PG 档。

调研说明：`PRAGMA foreign_keys`、`batch_alter_table`、`compare_metadata` 均为所用
框架的既有能力，本轮未做外部方案检索（影响范围明确的局部修复）；三条差异的判定
全部来自本仓常驻用例的实测输出，不引用未实际查阅的外部文档。


## 追加裁决（v0.5.0 收口）：其余 19 处未声明外键的 `*_id` 列

判据不是"看着像外键就加"，而是先量删除能力：AST + 裸 SQL `DELETE FROM` 三种形态的普查
显示，全仓唯一的硬删路径是 `GpuAllocation`（scheduler 释放分配），且没有任何表按 id
引用它；其余被引用的表要么只软删（workspace tombstone），要么根本不删。
→ 对"列里存的是别表主键"的指针加约束，不会挡任何现存流程，只挡脏写。

**已加（11 处，迁移 `b7e4c1a09f52`）**：`gpu_allocations.{workspace_id,host_id}`、
`gpus.workspace_id`、`credit_holds.workspace_id`、`deployments.{workspace_id,edge_agent_id}`、
`telemetry_events.edge_agent_id`、`workspaces.template_version_id`、`artifacts.workspace_id`、
`streaming_sessions.workspace_id`、`submissions.workspace_id`。

**有意不加（8 处）**，逐条给理由，避免下一轮当成漏网：
- `workspaces.gpu_id`：与 `gpus.workspace_id` 互为回指。两边都加会让 `compare_metadata`
  发 "unresolvable cycles between gpus, workspaces" 并**静默跳过该环内全部外键比较**
  （实测：加上之后对账门当场变瞎）。互指对因此只保留一条方向，且该警告已被
  `tests/test_migrations.py` 收成判红条件。
- `templates.current_version_id`：与 `template_versions.template_id` 互指，同理。
- `workspaces.template_id` / `labs.template_id` / `deployments.artifact_id`：`template_id`
  存的是 slug 而非主键（`String(64)`）。`templates.slug` 有唯一约束，技术上能指，但会把
  "模板改 slug"变成一次跨表改写；要先决定 slug 是否可变更，属独立的引用形态裁决。
  `artifact_id` 同类（可加，但与它们一并在"引用形态统一"那轮处理，避免同一表被两拨
  迁移来回 batch 重建）。
- `credit_ledger.{workspace_id,template_id}`：账本是审计事实，模板下线、工作区归档后仍
  必须能还原"当时扣了多少、按什么价"；且 `template_id` 同样存 slug。
- `billing_accounts.subject_id`：多态（user 或 organization），没有单一目标表。

## 追加后果：`allocate()` 的错误分类被这次改动暴露

给 `gpu_allocations.workspace_id` 加外键后，PG 档 6 个分配用例开始报
"No GPU available"。真实机制是：`allocate()` 把**任何** `IntegrityError` 都当成
"卡被别人抢了 → 回滚重试"，于是外键挡下的"workspace 行不存在"被误报成容量不足——
把数据缺陷伪装成资源不足。已改为按 SQLSTATE（23505 唯一冲突＝竞争，可重试）与 SQLite
的 `UNIQUE constraint failed` 文案分类，非唯一冲突直接抛出真实原因；
`test_missing_workspace_is_not_reported_as_no_capacity` 双向钉住（退回盲重试即红，
读数为 `No GPU available with >= 0 GB VRAM (workspace ws-does-)`）。
PG 档同时补上真实 workspace 父行：分配用例此前一直用 `ws-keep` 这类字面量，这在生产
路径上不可能发生（provision 先落 workspace 再分配）。

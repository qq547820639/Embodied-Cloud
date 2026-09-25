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

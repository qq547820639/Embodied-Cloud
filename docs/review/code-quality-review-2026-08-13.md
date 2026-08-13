# 代码质量审查报告

> **⚠️ 历史快照**：本文是 2026-08-13 对 v0.3.0 的审查记录。文中所列 P0/P1/P2 缺陷绝大多数已在后续提交（b02811a → 05a2643 以及 v0.4.0 迭代）修复并附回归测试；行号也已漂移。作为当时审查的历史证据保留，最新状态以代码、`docs/CURRENT_STATE.md` 与 `docs/ACCEPTANCE_GATES.md` 为准。


- **审查对象**：Embodied Cloud 具身智能云平台后端（FastAPI + SQLAlchemy 2.0 + Alembic）
- **审查范围**：实现层面（关键类/函数、代码质量、潜在缺陷）；与架构走读（目录/模块职责/依赖）并行，本文聚焦「实现好不好」
- **审查方式**：全量走读 `app/`（41 个 Python 文件，约 6770 行）+ 抽查 7 个关键测试文件
- **审查日期**：2026-08-13
- **审查人**：工程师（寇豆码）

---

## 1. 代码组织方式评价

### 1.1 总体印象

代码整体质量**明显高于一般 FastAPI 原型**：分层清晰（router 薄壳 → service 业务 → provider 适配）、模型/枚举集中、异常边界与并发安全有明确的 ADR 依据（`docs/adr/*`、`SECURITY.md`、`PRODUCT_SPEC.md` 交叉引用密集），几乎每个非平凡决策都有 `§N` 段落号注释锚定到设计文档。这是「结构对不对」由架构师把关、而本文能聚焦「实现好不好」的良好前提。

### 1.2 命名规范（良好）

- 枚举统一使用 `StrEnum`（`WorkspaceStatus`、`OperationStatus`、`WarmPoolState` 等），值为小写字符串，与 DB 存储一致。
- 服务类命名清晰：`GpuScheduler` / `WorkspaceOrchestrator` / `OperationWorker` / `CreditLedgerService` / `BillingPolicy` / `WarmPoolManager` / `DeploymentService` / `StreamingSessionService`。
- 私有方法以下划线前缀区分（`_execute_provision` / `_try_claim` / `_fenced_update` / `_finalize_stop`）。
- 缺陷：`utcnow()` 在 `models.py`、`scheduler.py`、`worker.py`、`orchestrator.py`、`ledger.py`、`edge.py`、`streaming.py`、`warmpool.py`、`course.py` 中**重复定义了 9 次**（内容完全相同），属典型复制粘贴，应收敛到单一工具模块（如 `app/timeutil.py`）。

### 1.3 模块划分（良好）

- `routers/` 全部是薄壳：只做参数校验、owner 隔离（越权统一 404）、调用 service、异常到 HTTP 状态码的映射。
- `services/` 承载业务；`services/providers/` 通过 `Protocol`（`WorkspaceProvider`）抽象了 Mock/Docker/K8s 三实现，契约清晰（`ResourceReservation` 单一 GPU 决策来源）。
- `deps.py` 集中构造应用级单例并解决 router 循环导入问题，是合理的 composition root。

### 1.4 代码风格一致性（良好，有少量例外）

- 类型注解完整、docstring 覆盖度高、复杂逻辑有注释；`ruff` 配置严格（S 系列安全规则全开，仅按需 `noqa`）。
- **例外 1**：`deployments.py` 路由自行构造了一套 `Settings/engine/SessionFactory/DB/CurrentUser/deployment_service`（见问题 P1-2），打破了「依赖从 deps.py 统一注入」的约定，是唯一明显的风格破窗。
- **例外 2**：`monitor_runtime_quotas` 内部硬编码 `BillingPolicy(minimum_launch_minutes=5, enforce_preauthorization=False)`，与 deps.py 注入的全局 `billing` 脱节（见 P1-3）。
- **例外 3**：函数内 `import`（`from ..metrics import ...`、`from sqlalchemy.engine import CursorResult` 等）频繁出现，用于解循环依赖/懒加载，属有意的工程折中，但个别可上提。

### 1.5 小结

命名、分层、注释、类型标注均为中上水平；主要扣分点是**重复代码（utcnow/结算逻辑/双套 DI）**与**个别硬编码逃逸**，未发现 Mixin 滥用或巨型 God Class（`orchestrator.py` 537 行是本项目最大文件，仍在可控范围）。

---

## 2. 关键类 / 函数实现走读（7 条链路）

### 链路 1：任务调度生命周期（OperationWorker + execute_operation）

- **所在文件**：`app/services/worker.py`、`app/services/orchestrator.py:113-143`
- **职责**：把 workspace 生命周期操作（PROVISION/START/STOP/DESTROY/RECONCILE）落库为 `WorkspaceOperation`，由后台 worker 线程 claim → 执行 → 写终态，支持崩溃恢复。
- **实现要点**：
  - `enqueue_operation` 依赖**数据库部分唯一索引** `uq_ops_active_per_workspace`（`models.py:405-411`）拒绝同 workspace 并发 active op，不依赖「先查再插」——这是正确的并发姿势。
  - `_try_claim` 用原子 `UPDATE ... WHERE id AND status=<读到的状态> AND lease 条件`，`rowcount==1` 才判定 claim 成功（`worker.py:243-292`）。
  - `renew_lease` / `_fenced_update` 全部带 `fencing_token + lease 未过期` 条件，防过期 worker 写终态（`worker.py:294-336`）。
- **质量评价**：**优秀**。lease/fencing 是全文正确性最高的一段，且测试覆盖到位（`test_worker_fencing.py` 覆盖长操作续期、过期 reclaim 后旧 worker 无法写终态、并发 claim、崩溃 reclaim）。唯一可改进点：`tick_once` 全程持有一个 DB session（含外部 provision 数分钟），且业务步骤与 heartbeat 分属不同 session，跨事务一致性强依赖 `expire_on_commit=False` 的语义（见 P2-3）。

### 链路 2：Provision 与补偿回滚（`_execute_provision`）

- **所在文件**：`app/services/orchestrator.py:148-260`
- **职责**：把 workspace 从 QUEUED 带到 RUNNING：billing 门禁 → 原子分配 GPU → provider.provision → wait_ready 门禁 → 写 RUNNING；任一环节失败走 `_fail`（FAILED + 释放 GPU + 清空字段）。
- **实现要点**：
  - 采用**补偿式事务**而非单事务：先 commit QUEUED/PROVISIONING 中间态，外部副作用失败后 `provider.destroy` 补偿 + `scheduler.release` 归还（`orchestrator.py:220-225`）。
  - readiness gate：`provider.wait_ready` 返回 False 视为失败并回滚，避免「容器起了但没就绪」的假 RUNNING（`orchestrator.py:213-219`）。
  - `reservation.metadata` 注入 operation/fencing 上下文，供 provider 打 label 便于审计/adopt（`orchestrator.py:187-203`）。
- **质量评价**：**优秀**。补偿域、幂等重试（`_container_exists` adopt 不建第二份容器）、失败字段清理都考虑到了，`test_provision_rollback.py` 用 `@pytest.mark.parametrize` 对「分配后/容器创建后/端点创建后/健康检查中」四种失败点逐一断言无孤儿 GPU/容器/PVC/端口。小瑕疵见 P2-4（`_fail` 中 release 失败时 rollback 会回退 FAILED 状态写入）。

### 链路 3：GPU 调度（GpuScheduler.allocate/release）

- **所在文件**：`app/services/scheduler.py:92-154`
- **职责**：GPU inventory 同步（幂等 upsert）、按显存需求原子分配、幂等释放、crash recovery 支撑。
- **实现要点**：
  - `allocate` 用 `SELECT ... FOR UPDATE SKIP LOCKED`（PostgreSQL 生效）选候选，逐个尝试插入 `GpuAllocation`，靠 `gpu_allocations.gpu_id` 唯一约束兜底并发（`IntegrityError → rollback → 下一张`）（`scheduler.py:108-126`）。
  - `release` 删除分配行而非软标记（因为 `gpu_id` 唯一约束用于「同一 GPU 至多一个活动分配」，保留历史会阻塞重分配），注释说明审计走 ledger（`scheduler.py:132-154`）。
- **质量评价**：**良好，但有硬伤**。并发设计在 PostgreSQL 下成立；但在默认 SQLite 下 `FOR UPDATE SKIP LOCKED` 是 no-op，实际并发正确性完全依赖 SQLite 的库级写锁 + 唯一约束重试。更关键的是**显存单位换算错误**（`scheduler.py:102` 用 `gpu_requirement_gb * 1024` 与 MiB 比较），以及 `recover_stuck_gpu_allocations` 的 PROVISIONING 竞态（详见 P0-1、P1-7）。

### 链路 4：计费入账（CreditLedgerService + BillingPolicy + 配额监控）

- **所在文件**：`app/services/ledger.py`、`app/services/billing.py`、`app/services/orchestrator.py:479-533`
- **职责**：不可变账本（append-only，永不 UPDATE/DELETE）、幂等结算、启动门禁、运行中透支监控。
- **实现要点**：
  - `record` 以 `idempotency_key` 唯一约束实现幂等，并发重放命中 `IntegrityError → 重查返回已有记录`（`ledger.py:45-71`）。
  - 运行段结算幂等键 `usage:{workspace_id}:{started_at_iso}`，同一运行段重复结算不重复扣费（`ledger.py:89-118`）。
  - `check_launch_eligible`：admin/instructor 放行 → 个人+组织余额为负拒绝 → 预授权门槛 → course 配额（`billing.py:34-75`）。
  - `monitor_runtime_quotas` 周期性扫描 RUNNING，投影余额 <0 或 course 配额用尽则优雅停止（`orchestrator.py:479-533`）。
- **质量评价**：**良好，账本模型本身很扎实**（测试 `test_billing_policy.py` 覆盖负余额拒绝、配额、幂等、重复 stop 不重复扣费、预授权门槛）。但实现层存在三处不一致/瑕疵：金额恒定 1 credit/秒（`rate` 只进描述文本）、monitor 内部重建 policy 且只算个人余额（P1-3、P2-1、P2-4）、`_execute_provision` 重试路径无 lab 上下文导致 course 配额门禁可被绕过（P2-10）。

### 链路 5：停止 / 删除（stop / destroy + durable operation）

- **所在文件**：`app/services/orchestrator.py:265-359`
- **职责**：STOP = streaming 终结 → runtime.stop → 结算 → 释放 GPU → STOPPED；DESTROY = 同前 + provider.destroy + tombstone（软删除）。
- **实现要点**：
  - 幂等：`stop` 对已 STOPPED/FAILED 补做 `_finalize_stop`；结算段以 `started_at` 幂等键防重复扣费；`destroy` 对已 tombstone 直接返回（`orchestrator.py:270-275、337-338`）。
  - 全部步骤可重试：`stop` 中途失败置 FAILED，再次 stop 可继续完成 cleanup（`orchestrator.py:285-287`）。
  - soft delete tombstone 保留行供审计/安全调查（`orchestrator.py:355-359`）。
- **质量评价**：**良好，但错误吞并削弱了可靠性**。`destroy` 中 `terminate_for_workspace` 与 `_settle_running_segment` 的异常被 `except: db.rollback()` 吞掉后继续置 DELETED（`orchestrator.py:339-347`），意味着**结算失败会被静默丢弃**（P1-1）；同时 provider.destroy 失败也不阻断（与 P0-2 关联）。

### 链路 6：Warm Pool claim 与凭据轮换

- **所在文件**：`app/services/warmpool.py:98-207`、`app/security.py:126-179`
- **职责**：预热 READY runtime（无用户归属/无数据），用户启动时原子 claim → 绑定用户 → 轮换凭据 → 交付；轮换失败则 DRAINING/FAILED + 完整补偿。
- **实现要点**：
  - claim 原子性：`UPDATE ... WHERE warm_pool_state='ready'` + rowcount 校验，并发至多一个成功（`warmpool.py:114-143`）。
  - 凭据：控制面 DB 只存 Fernet 密文，runtime 侧保留明文副本；`resolve` fail-closed（密文解密失败抛异常，绝不当明文返回）（`security.py:133-157`）。
  - 轮换失败补偿：terminate streaming → provider.destroy → 释放 GPU → 清凭据/owner/端口 → DRAINING（0 orphan）（`warmpool.py:163-197`）。
- **质量评价**：**优秀**。`test_warmpool_claim.py` 覆盖并发 claim、轮换成功/失败、失败后 runtime+GPU 清理、不可二次 claim，是该模块正确性的有力证据。可改进：K8s `rotate_credentials` patch 后立即返回 True，不等待滚动重启完成，存在旧凭据短暂仍可登录的窗口（P2-9）。

### 链路 7：部署回滚 / 校验（DeploymentService 状态机 + ArtifactStore 防穿越 + K8s provision 补偿）

- **所在文件**：`app/services/deployment.py`、`app/services/artifact_store.py`、`app/services/providers/k8s.py:149-283`
- **职责**：artifact 登记（对象存储）→ 部署状态机（pending→downloading→verified→running→success/failed）→ Edge 端 checksum 上报校验 → K8s 资源创建与补偿清理。
- **实现要点**：
  - 防绕过：必须先 `download`（DOWNLOADING）才能 verify/report-checksum，PENDING 直接校验拒绝；FAILED 终态防 replay 复活（`deployment.py:207-237`）。
  - 路径穿越防护：`_safe_key` 拒绝绝对路径与 `..`（`artifact_store.py:26-33`）；`create_artifact` 用 `resolve() + is_relative_to` 双重校验（`deployment.py:82-87`）。
  - K8s provision 补偿：PVC 成功但 Service/Deployment 失败时逐一 `_try_delete_*`（404 视为成功），避免孤儿 volume（`k8s.py:193-273`）。
- **质量评价**：**优秀**。校验/防绕过/补偿是三处最容易出安全与资源泄漏问题的点，均处理得当，`test_deployment_bypass.py`、`test_deployment_verification.py`、`test_edge_checksum.py` 有针对性覆盖。

---

## 3. 问题清单

> 级别定义：**P0** 严重（影响正确性/安全，需立即修复）；**P1** 重要（影响可靠性/可维护性）；**P2** 建议（质量/健壮性改进）。

### P0（2 项）

#### P0-1 GPU 双分配竞态：`recover_stuck_gpu_allocations` 释放仍在 PROVISIONING 的 GPU
- **位置**：`app/services/scheduler.py:197-226`（调用点 `app/services/orchestrator.py:459`）
- **问题描述**：`recover_stuck_gpu_allocations` 以「workspace 状态 == RUNNING」作为唯一有效占用判据，释放所有非 RUNNING workspace 的 GPU。但 PROVISIONING（已分配 GPU、正在建容器）与 STOPPING（已分配、正在停）状态的 workspace 也合法持有 GPU。该函数在 `reconcile_all` 末尾无条件调用；多 worker 部署下，worker A 执行 RECONCILE 时，worker B 可能正在执行 PROVISION，此时 PROVISIONING workspace 的 GPU 被释放并置 AVAILABLE，随后被新 workspace 再次分配 → **同一物理 GPU 同时跑两个 workspace**（一卡双跑）。
- **影响分析**：违反项目自身最高不变式「ONE RESOURCE = ONE SOURCE OF TRUTH」，GPU 超额订阅，可能引起显存 OOM、训练互相踩踏、计费错误，属正确性 + 安全双重问题。
- **建议修复方向**：`recover_stuck_gpu_allocations` 只释放「无 active operation（PENDING/RUNNING/RETRYING）」的 workspace 的 GPU（复用 `orchestrator._has_active_operation` 逻辑），或显式排除 PROVISIONING/STOPPING 状态；并补一个并发回归测试。

#### P0-2 Docker provider 清理静默失败 → 孤儿容器 + GPU 复用
- **位置**：`app/services/providers/docker.py:208-216`（`stop`/`start`/`destroy` 均 `check=False` 且不检查 returncode）
- **问题描述**：`destroy` 执行 `docker rm -f` 后忽略返回值；当 `docker rm -f` 因 daemon 不可用/权限/其它原因真正失败时，orchestrator 仍标记 DELETED 并释放 GPU。孤儿容器仍以 `--gpus device=N` 运行，该 GPU 随后被分配给新 workspace → 同 P0-1 的一卡双跑。
- **影响分析**：静默失败导致资源泄漏 + 双分配；且与幂等语义混淆——「容器不存在」与「删除失败」都被当作成功处理。K8s provider 的 `destroy` 已正确区分 404 与其它错误（`k8s.py:313-336`），Docker 未做到。
- **建议修复方向**：`docker rm -f` 失败且非「No such container」时抛出异常，让 orchestration 层的补偿域感知失败；至少应在 `destroy` 失败时**不释放 GPU / 不置 DELETED**，交由 reconcile 重试。

### P1（7 项）

#### P1-1 destroy 吞掉计费结算异常 → 计费丢失
- **位置**：`app/services/orchestrator.py:339-347`
- **问题描述**：`destroy` 中 `terminate_for_workspace` 与 `_settle_running_segment` 均被 `except Exception: db.rollback()` 吞掉，随后无条件 `provider.destroy` + `scheduler.release` + 置 DELETED。若结算因 DB 异常失败，该运行段的 usage 将永久丢失。
- **影响分析**：对计费系统而言，静默丢账比多扣费更难发现。虽然「清理必须推进」的动机合理，但应以「记录待补结算/告警」的方式兜底，而非直接丢弃。
- **建议修复方向**：结算失败时记录 error（如写入 `workspace.error_message` 或独立 `billing_pending` 标记）+ 告警日志，保证可审计、可补偿。

#### P1-2 deployments 路由重复构造整套 DI
- **位置**：`app/routers/deployments.py:20-30`
- **问题描述**：该路由自己 `Settings()` + `make_engine` + `make_session_factory` + 独立 `DB`/`CurrentUser`/`deployment_service`，注释自认「deps 模块存在既有 mypy 错误故不导入」。这产生了第二条 SQLAlchemy engine/连接池、第二套认证依赖函数。
- **影响分析**：破坏单一 composition root，埋下「两边行为漂移」的隐患（例如将来 deps 改了 session/security 配置而此路由不跟随）；且额外连接池浪费资源。属可维护性风险。
- **建议修复方向**：修复 deps 的 mypy 错误并从 `..deps` 导入统一的 `DB`/`CurrentUser`/`orchestrator`；`deployment_service` 与 `artifact_store` 的构造上提到 `deps.py`。

#### P1-3 `monitor_runtime_quotas` 硬编码 policy 且忽略组织余额
- **位置**：`app/services/orchestrator.py:498-516`
- **问题描述**：监控函数内部 `BillingPolicy(self.session_factory, self.ledger, minimum_launch_minutes=5, enforce_preauthorization=False)` 新建实例，与 deps 注入的全局 `billing`（携带真实配置）脱节；同时 `projected = self.ledger.balance(db, user.id) - live` 只算个人余额，而 `check_launch_eligible` 用的是「个人 + 组织」余额。
- **影响分析**：生产若修改 `billing_minimum_launch_minutes`/`enforce_preauthorization`，监控口径不随之变化；有组织额度的用户会被监控误判透支而停止。门禁与监控口径不一致，易造成行为矛盾。
- **建议修复方向**：直接使用 `self.billing`；投影余额改用 `billing` 内一致的「个人+组织」口径（抽取公共 `available_balance()` 方法）。

#### P1-4 warm pool 预热阻塞单 worker 循环
- **位置**：`app/services/warmpool.py:57-93`（尤其 `:82` `self.orchestrator._start(workspace.id)`）、`app/deps.py:61-66`
- **问题描述**：`maintain` 对每个缺失的 warm workspace 同步调用 `orchestrator._start`（阻塞，可能是分钟级 provision）。而 maintain 是挂在 `OperationWorker` 周期任务上的——**单 worker 循环**在预热期间被完全占用，无法处理 STOP/DESTROY/PROVISION 等操作。
- **影响分析**：预热规模稍大即造成全生命周期操作排队/延迟；且 warm 预热不在 operation 体系内，无 lease/heartbeat/fencing 保护，崩溃后状态可能悬在 PREWARMING。
- **建议修复方向**：预热改为 enqueue PROVISION operation 走统一 worker/lease 体系，或为预热单独开限流线程池；至少在 maintain 内以非阻塞方式入队。

#### P1-5 warm pool 把普通 QUEUED workspace 误计入池
- **位置**：`app/services/warmpool.py:321-335`（`_count_legacy_pool`），消费于 `:66-67`
- **问题描述**：`_count_legacy_pool` 把所有 `warm_pool_state is None` 且状态为 CREATED/QUEUED 的 workspace 都当作「旧语义池内成员」，而普通用户的刚创建/排队 workspace 也满足该条件。`missing = size - (ready+prewarming+legacy)` 因此被低估。
- **影响分析**：池在有普通排队任务时欠预热，claim 命中率下降，退化为普通 provision，warm pool 加速效果被稀释。
- **建议修复方向**：legacy 判定加 `user_id IS NULL`（无归属）或更严格的 name/前缀约束，避免与用户 workspace 混淆。

#### P1-6 GPU 显存单位换算错误（24GB 模板无法被 24GB 卡满足）
- **位置**：`app/services/scheduler.py:102`
- **问题描述**：`Gpu.memory_total >= gpu_requirement_gb * 1024`。`memory_total` 单位是 MiB（nvidia-smi 上报 24564 = 24GB 卡），而 `gpu_requirement_gb * 1024` 中 24GB → 24576，导致 24564 < 24576，**真实 24GB 卡无法满足 gpu_requirement_gb=24 的模板**（`franka-pick-place`、`domain-randomization`）。
- **影响分析**：单机只有 24GB 卡时，这些模板会报「No GPU available」而拒绝启动，尽管显存实际够用；mock 环境因有 48GB 卡而掩盖了此问题。
- **建议修复方向**：统一单位（`gpu_requirement_gb * 1000` 或统一用 MiB），并补一条「24564 MiB 应满足 24GB 需求」的边界测试。

#### P1-7 事务边界以补偿替代原子，`_fail` 中 rollback 会回退 FAILED 状态
- **位置**：`app/services/orchestrator.py:148-260`（尤其 `:250-260`）
- **问题描述**：provision 流程分多段 commit（QUEUED → PROVISIONING → RUNNING），依赖失败补偿。`_fail` 先置 `workspace.status=FAILED`，再 `scheduler.release`；若 release 抛异常触发 `db.rollback()`，会把刚写入的 FAILED 回退为 PROVISIONING，随后 clear GPU 字段并向上 re-raise，最终由 worker `finish_failure` 提交——但提交的状态可能不是 FAILED。
- **影响分析**：极端 DB 异常下 workspace 可能卡在 PROVISIONING 且 GPU 字段已清空，需依赖 reconcile 兜底。补偿模式本身正确，但 `_fail` 内的 rollback 粒度不当。
- **建议修复方向**：`_fail` 中释放 GPU 使用独立 try/except，失败时记录而非 rollback 整个会话；或将「置 FAILED」与「释放 GPU」拆分为可独立重试的步骤。

### P2（建议，16 项）

| # | 位置 | 问题 | 建议 |
|---|------|------|------|
| P2-1 | `app/services/ledger.py:104-113` | `rate = template.estimated_hourly_cost_cny` 只用于描述文本，实际金额恒为 `-seconds`（1 credit/秒），费率未参与计费，变量形同虚设 | 明确计费口径：要么按 `rate/3600` 折算，要么删除 `rate` 相关代码并更新注释 |
| P2-2 | `app/services/scheduler.py:185-194` | `recover_stuck_workspaces` 从未被调用（grep 全库无引用），死代码 | 删除或接入 reconcile |
| P2-3 | `app/services/orchestrator.py:292-328` | `_finalize_stop` 与 `_settle_running_segment` 结算逻辑重复（累计秒数 + settle + 清 started_at） | 抽公共 `_settle_segment(db, ws)` 复用，避免漂移 |
| P2-4 | `app/services/orchestrator.py:516` vs `billing.py:48-50` | 余额口径不一致：门禁=个人+组织，monitor=仅个人 | 与 P1-3 一并修复 |
| P2-5 | `app/models.py:270` | `Workspace.password` 为 `String(128)`，Fernet 密文（含 `enc:` 前缀）约 120~124 字符，余量极紧，未来密码/密钥格式变化易溢出 | 放宽到 `String(255)` 或显式断言长度 |
| P2-6 | `app/config.py:46`、`app/security.py:160-166` | `password_pepper` 默认空、`_derive_key` 使用硬编码 dev key；生产仅校验 credential key，未校验 pepper / `auto_create_tables` | 增加生产启动校验（provider != mock 时强制 pepper 非空、`auto_create_tables=False`） |
| P2-7 | `app/main.py:105-133` | `demo_workspace` 无鉴权，任何人均可访问并看到 workspace 名称/template 启动命令 | mock 演示页也应加鉴权或仅 localhost 暴露，避免信息泄露 |
| P2-8 | `app/services/ports.py:16-21` | `allocate_tcp_port` 先查后取（TOCTOU），check 与 docker run 之间端口可能被抢占 | 接受为单机可信模式（ADR 已说明），或改为 docker 失败后重试 |
| P2-9 | `app/services/providers/k8s.py:436-473` | `rotate_credentials` patch 后立即返回 True，不等待滚动重启，旧凭据在新 Pod 就绪前仍可登录 | 轮换后 poll Deployment ready（新 pod 就绪）再返回 True |
| P2-10 | `app/services/course.py:297-320` vs `orchestrator.py:158-161` | `launch_lab` 走门禁含 lab 配额，但 `_execute_provision` 重试路径 `check_launch_eligible` 不带 lab → course 配额门禁可被绕过（仅靠 monitor 兜底） | retry 路径补传 lab 上下文，或门禁独立于 provision 重试 |
| P2-11 | `app/services/course.py:235-276` | `course_completions` 对 students × assignments 逐条查询，N+1（且是三循环） | 用 join + group_by 一次查出 |
| P2-12 | `app/services/course.py:126-128`、`deployment.py:120-149` | `create_course` slug 唯一性、`deploy` 幂等检查均「先查再插」无唯一约束兜底，并发可双插入/500 | 依赖 DB 唯一约束 + 捕获 IntegrityError |
| P2-13 | `app/services/artifact_store.py:113-117` | `S3CompatibleArtifactStore.exists` 吞掉所有异常（含鉴权/网络错误）返回 False，把「故障」当「不存在」 | 区分 404 与其它异常，网络/鉴权错误应上抛 |
| P2-14 | `app/services/course.py:323-348` | `upsert_submission` 不校验 `workspace_id` 是否属于提交者 | 加 owner 校验（复用 `_get_owned` 语义） |
| P2-15 | `app/services/streaming.py:19-20` | 默认端口 49100/47998 为裸魔法数字 | 上提到 config |
| P2-16 | `app/logging_setup.py:23-43` | `RedactingFormatter` 只脱敏 `extra` 字段，不脱敏 `message` 内嵌的敏感内容（如 `logger.info("... %s", password)`） | 对 message 做关键词扫描脱敏，或规范「敏感值一律走 extra 字段」 |

---

## 4. 测试覆盖对照

### 4.1 总体判断

37 个测试文件 + `conftest.py` + `k8s_fakes.py`，**覆盖广度优秀**：几乎每个 service 都有对应测试，且质量高于普通单测——大量使用 fake provider / fake k8s client、参数化失败注入、多线程并发断言、幂等断言。

### 4.2 各子系统覆盖对照

| 子系统 | 测试文件 | 覆盖质量 |
|--------|----------|----------|
| Worker / lease / fencing | `test_worker.py`、`test_worker_fencing.py`、`test_durable_ops.py` | ★★★★★ 并发 claim、lease 续期、reclaim、双终态拒绝全覆盖 |
| Provision / 补偿回滚 | `test_provision_rollback.py`、`test_runtime_readiness.py` | ★★★★★ 分阶段失败注入 + 无孤儿断言 |
| 计费 / 账本 | `test_billing_policy.py`、`test_ledger.py` | ★★★★☆ 幂等/配额/预授权全，缺 destroy 结算失败路径 |
| Warm pool | `test_warmpool.py`、`test_warmpool_claim.py` | ★★★★★ 并发 claim、轮换失败补偿全覆盖 |
| GPU 调度 | `test_scheduler.py`、`test_gpu_single_authority.py`、`test_k8s_inventory.py` | ★★★★☆ 缺 PROVISIONING 并发释放竞态、显存单位边界 |
| K8s provider | `test_k8s_provider.py`、`test_k8s_node_truth.py`、`test_k8s_integration.py`、`test_k8s_inventory.py` | ★★★★☆ fake client 注入成熟，integration 需真实集群（marker 隔离） |
| Docker provider | `test_docker_provider.py` | ★★★☆☆ 缺 destroy 失败静默吞掉路径 |
| 部署 / Edge | `test_deployment*.py`、`test_edge*.py`、`test_artifact_store.py` | ★★★★☆ 防绕过/checksum/ownership 全覆盖 |
| 流生命周期 | `test_streaming.py`、`test_streaming_lifecycle.py` | ★★★★☆ |
| 凭据加密 | `test_workspace_credential.py` | ★★★★☆ |
| 课程 | `test_courses.py` | ★★★☆☆ 功能覆盖，缺 N+1/配额绕过 |
| 版本一致性 / 可观测 / 迁移 | `test_version_consistency.py`、`test_observability.py`、`test_migrations.py` | ★★★★☆ |

### 4.3 覆盖缺口（与本文问题清单对应）

1. **无 `recover_stuck_gpu_allocations` 的 PROVISIONING 并发测试**（P0-1 无回归保护）。
2. **无 Docker `destroy`/`stop` 失败注入测试**（P0-2 无回归保护）。
3. **无 destroy 结算失败不丢账的测试**（P1-1）。
4. **无显存单位边界测试**（24564 MiB 应满足 24GB 需求，P1-6）。
5. **无 `cli.py`（bootstrap-admin / make-session / show-usage）测试**。
6. **无 `logging_setup.RedactingFormatter` 的 message 脱敏单测**（现有 `test_observability` 偏指标/健康检查）。
7. **无安全专项测试文件**（密码哈希/令牌哈希的暴力破解参数、session 过期边界依赖 `test_api.py` 间接覆盖，缺独立 `test_security.py`）。

---

## 5. 改进建议总结（按优先级排序）

### 立即修复（P0，影响正确性/安全）

1. **修 `recover_stuck_gpu_allocations` 的 GPU 双分配竞态**：只回收「无 active operation」的 workspace 的 GPU，排除 PROVISIONING/STOPPING；补并发回归测试。
2. **修 Docker `destroy`/`stop` 静默失败**：区分「容器不存在」（幂等成功）与「删除失败」（上抛/阻断 GPU 释放与 DELETED），与 K8s 的 404 语义对齐。

### 短期修复（P1，影响可靠性/可维护性）

3. **destroy 结算失败改为可审计**（记录 + 告警，不静默丢弃）。
4. **收敛 deployments 路由的重复 DI**，回到 `deps.py` 单一 composition root。
5. **统一 `monitor_runtime_quotas` 的计费口径**（复用全局 `billing` + 个人/组织余额）。
6. **warm pool 预热去阻塞**（走 operation 体系或独立限流线程池），并修正 legacy 池计数误纳入普通 workspace。
7. **修正 GPU 显存单位换算**（GB/MiB 统一），补边界测试。
8. **收窄 `_fail` 的 rollback 粒度**，避免回退 FAILED 状态。

### 建议改进（P2，质量/健壮性）

9. 收敛 `utcnow()` 9 处重复定义、`_finalize_stop`/`_settle_running_segment` 重复逻辑。
10. 清理死代码（`recover_stuck_workspaces`、`rate` 变量），明确计费口径。
11. 补 CLI / 日志脱敏 / 安全专项测试；补上述 P0/P1 的失败注入与边界测试。
12. 修 `course_completions` N+1、`create_course`/`deploy` 的唯一约束兜底、`S3.exists` 异常区分、`rotate_credentials` 等待滚动重启完成。
13. 生产启动校验补 pepper / `auto_create_tables`；`demo_workspace` 加鉴权；`password` 列放宽长度。

---

## 附：审查结论

- 这是一个**工程成熟度相当高的原型**：并发安全（lease/fencing）、幂等、补偿回滚、owner 隔离、凭据加密、防绕过校验等关键环节都有意识、有注释、有测试，且测试质量（失败注入、并发、幂等）显著高于行业均值。
- 主要风险集中在**「静默失败 → 资源双分配/丢账」**这一类：两处 P0 都是「清理失败被当成成功处理」的变体，提示项目需要把「provider 清理结果必须显式反馈」提升为与「ONE RESOURCE = ONE SOURCE OF TRUTH」同级的硬约束。
- 账本、warmpool claim、部署校验三大子系统实现质量可作同类项目的参考实现。

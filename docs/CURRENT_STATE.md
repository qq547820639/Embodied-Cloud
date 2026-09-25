# CURRENT_STATE — EmbodiedCloud

> 版本：**0.5.0 验证纵深 + 计费预授权**（2026-09-26）。
> 数据来源：`docs/VALIDATION.json`（`make validate` 自动生成，JUnit 稳定计数）。

## 1. 本次真实验证（实测，非复制旧文档）

| Gate | 结果 |
|---|---|
| Test | **PASS（410 passed + 1 skipped[k8s_integration]，collected 411）** |
| Lint / Type | PASS（ruff 0 / mypy 40 files） |
| Migration | PASS（clean DB empty→head **14 文件链** + schema 落地 + downgrade 循环 + **模型↔迁移对账**） |
| Integration PostgreSQL | **PASS 18/18**（自建一次性容器，真行锁语义） |
| Integration Docker | **PASS 17/17**（真容器，非 mock） |
| Integration Browser | **PASS 11/11**（Playwright 驱动系统 Chrome 真 DOM） |
| Integration K8s | PENDING（原因登记：无真实集群；请求体合规改由 SDK 模型档覆盖 6 例） |
| 供应链 | PASS（`uv.lock` 一致性 + SBOM + `uv audit --locked`，CI 门禁） |
| OpenAPI / VALIDATION freshness | PASS（`make api-docs` / `make validate` 无 diff） |

## 2. v0.5.0 本轮交付

| § | 内容 | 验证 |
|---|---|---|
| V-1 | **PostgreSQL 真并发档**（此前"SQLite 下 `FOR UPDATE SKIP LOCKED` 是 no-op，并发语义无法验证"被当作环境限制）：`tests/pg_server.py` 自建一次性 PG 容器（就绪判据走测试真正使用的 TCP+`SELECT 1` 路径，不用容器内 `pg_isready`——它在 initdb 期间会先起一个只监听 unix socket 的临时服务器） | `test_postgres_concurrency.py` 18 例：`FOR UPDATE` 必须阻塞 / `SKIP LOCKED` 必须放行的成对判据、allocate 锁范围确定性探针、并发不重复占用、worker lease CAS、账本 8 线程幂等、warm pool CAS 单赢家、BillingAccount 行锁两档并排、外键冲突不得被当成"卡不够" |
| V-2 | **Docker provider 真容器档**：`health/start/inspect/logs/wait_ready/reconcile` 及流式占用门禁首次在真实守护进程上执行（架构匹配镜像选择、不使用 `--rm` 以免"退出但存在"被掩盖、宿主 http_server 提供真实下载源、会话级容器泄漏守卫） | `test_docker_provider_integration.py` 17 例 |
| V-3 | **浏览器级前端档**：Playwright + 系统 Chrome 驱动真 DOM，替掉"grep 前端源码"式伪覆盖 | `test_browser_console.py` 11 例；含 `<img src=x onerror=…>` 载荷在 DOM 中不执行、终态/页面隐藏时轮询真的停下（`pollTimer === null`）、控制台零错误 |
| V-4 | **K8s 请求体合规档**：provider 生成的对象经真实 SDK 的 `sanitize_for_serialization` 过一遍线格式，不再只与自造 fake 对拍 | `test_k8s_model_conformance.py` 6 例 |
| S-1 | **供应链可复现**：universal `uv.lock`（多平台 marker + sha256）、SBOM（`uv export --format cyclonedx1.5`）、`uv audit --locked`；CI 增 `make verify-lock` / `make sbom && make audit`，并要求"需要 docker 的档位必须真的 PASS，否则红" | `docs/SUPPLY_CHAIN.md` §4/§5 |
| B-1 | **§17 BillingAccount + §18 CreditHold 落地**：可用额度 = 账本毛余额 − pending hold；provision 前圈额度、结算转正、失败退回、超时扫描 | `test_credit_holds.py` 14 例（含 3 条走 worker 真实执行链的端到端）+ PG 档锁语义 |
| F-1 | 修 **GPU allocate 无界 `FOR UPDATE`**：一次启动会锁住整片候选 GPU，并发启动互相饿死（SQLite 档结构性看不见） | PG 档 `test_allocate_locks_exactly_one_candidate_row`（锁范围）+ `test_unbounded_candidate_read_starves_concurrent_allocate`（饿死反证） |
| F-2 | 修 **生命周期入队冲突谎报成功**：start/stop/delete 在队列拒绝时返回 409；`start` 不再先提交 `QUEUED` | `test_lifecycle_conflict_is_reported_instead_of_faking_success`（变异对照验过牙） |
| F-3 | 修 **edge_agents 租户外键从未被创建**（模型声明有、迁移没有 → SQLite 与 PG 都没有），并补上"模型↔迁移"对账门 | `3f0c9a51b7e2` + `test_migrations.py` 用 `compare_metadata` 对账（改前红 2 条 `add_fk`，改后 0） |
| F-4 | 修 **SQLite 外键默认不校验**：`make_engine` 逐连接 `PRAGMA foreign_keys=ON`；并把"SQLite 不提供行锁"钉成常驻断言 | `test_sqlite_semantic_baseline.py` 4 例 |
| F-5 | 修 released-version 回退非全序（同秒并列导致 flake）；零秒运行段 leave pending hold → capture 改为无条件 | `test_template_versions.py`、`test_credit_holds.py` |
| T-1 | **测试库进程隔离**：22 处模块级 `test-*.db` 文件库改为 pid 独占名（`tests/dbfiles.py`）。此前同一仓库并发跑两个 pytest 会互清对方的库。
改前基线（HEAD 工作树、两进程并排）：一份 31 例假红 rc=1、另一份 26 例假红 rc=1；
改后同一并发对照：两份 rc=0、FAILED 计数 0/0 | 正反两档都实测（并发即判据） |
| F-6 | **引用完整性收口**：19 处未声明外键的 `*_id` 列按删除能力普查逐条裁决，11 处补约束（`b7e4c1a09f52`）、8 处留理由；互指对只保留一条方向（两边都加会让 `compare_metadata` 静默跳过整环比较，已改成为判红条件） | PG 档 18/18 + 对账门无警告绿 |
| F-7 | 修 **`allocate()` 把任何 IntegrityError 都当成并发争用**：外键落地后"workspace 行不存在"被误报成"没有空闲卡"；改按 SQLSTATE / 约束文案分类 | 变异对照：退回盲重试即红（实测读数见 ADR 0005） |
| G-1 | 把两条"写在文档里的约定"变成常驻对账：①模型声明 ↔ 迁移产物（`compare_metadata`，非空即红）；②`Settings` 字段 ↔ `.env.example`（双向：漏文档 / 留死键都红） | `test_migrations.py` 对账用例 + `test_config_docs.py` 2 例（改前红：`billing_hold_ttl_minutes` 未进 .env.example） |
| D-1 | 决策记录：ADR 0004（hold 为何是独立表而非账本条目）、0005（SQLite/PG 语义差与两层验证）、0006（冲突即 409）；API.md 补 409/402 口径 | `docs/adr/000{4,5,6}-*.md` |

## 3. 分项状态

### VERIFIED PASS
410 tests 全绿（含 PG 真并发 18、真容器 17、真浏览器 11、SDK 线格式 6、预授权 14）；
lint/type/migration/build/smoke/release/供应链全链路。

### PHYSICAL_VALIDATION_PENDING / NOT_RUN（不假装 PASS）
GPU 真机（G1–G4 脚本就绪，本机无 NVIDIA 设备）· 真实 K8s 集群 · Streaming 媒体面（Isaac Sim WebRTC）· Robot 真机 · Warm pool SLA。

### BLOCKED_EXTERNAL_DEPENDENCY
NGC 凭据（镜像 digest 回填）· S3 凭据 · 物理机器人 · 真实 K8s 集群凭据 · NVIDIA 容器运行时（Docker 档的 `--gpus` 分支）。
> 注：docker daemon 与 postgres 镜像本轮已可用，旧文档"无 docker daemon"的说法作废。

### TECH DEBT（已知、有意延后）
edge agent 独立包（§25）· `default_idle_timeout_minutes`（缺 runtime 活动信号，标注预留）·
**测试公共 fixture 抽取**（4 组 `_setup`/`_register` 拷贝粘贴）·
K8s provider 的 `wait_ready/rotate_credentials/supports_credential_rotation` 真实集群路径 ·
Docker `--gpus` 设备透传分支 · S3 `ArtifactStore` 真实后端。

**引用完整性已逐条裁决完（见 ADR 0005 追加节）**：`*_id` 列普查出的 19 处未声明外键，
以"全仓删除能力普查"（唯一硬删是 `GpuAllocation`，且无人按 id 引用它）为依据分派——
11 处补上约束（`b7e4c1a09f52`），8 处保留不声明并逐条写明理由（互指环 2、slug 形态 3、
账本历史 2、多态主体 1）。这里不再有待办，只有已记录的设计立场。

（已闭：`test-*.db` 模块级文件库改 pid 独占，见 §2 T-1。闭之前实测过一次代价——两个
pytest 进程并发跑同一仓库，互相清库，读出 31 / 26 例假红。）

## 4. 结论

v0.5.0 把上一轮"记为无法在本环境验证"的四类判据（PostgreSQL 行锁、Docker provider、
真实浏览器 DOM、K8s 请求体形状）全部变成常驻门禁，并在此过程中修出 5 个真实缺陷：
GPU 分配锁范围只有真行锁下才看得见、生命周期谎报是浏览器档真点出来的、
`edge_agents` 的租户外键则由模型↔迁移对账暴露（三条都需要真后端或真工具，SQLite
+ grep 的老办法一条也抓不到）。计费侧补上预授权，使"并发启动把余额花成负数"不再可能。
硬件与凭据类项目（GPU 真机、真实集群、机器人、S3/NGC）继续保持
PENDING/BLOCKED 显式登记，不用测试通过来冒充物理验证。

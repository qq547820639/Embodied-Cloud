# Changelog

## 0.6.0 — 2026-09-26（把"没执行过的后端"逐个跑起来）

`docs/VALIDATION.json`（`make validate` 生成）：collected 458 / **passed 457** /
skipped 1（仅 `k8s_integration`，需 NVIDIA Device Plugin）/ failed 0；
lint/typecheck/migration/build 全 PASS；六个集成档中五档 **PASS**：
postgres 18/18、docker 20/20、browser 11/11、**object_store 20/20**、
**k8s_control_plane 7/7**。overall = `PASS_WITH_PHYSICAL_PENDING`。

### 对象存储：S3 后端第一次真正执行（ADR 0008）
- 此前的"S3 覆盖"是零执行的：boto3 不在任何依赖组里（懒加载直接 ImportError），
  6 条用例全部 `monkeypatch` 掉 `_client()` 并注入自造的 `ClientError`。
  现在 `tests/s3_server.py` 自起一次性 VersityGW 容器（真实 HTTP + SigV4 + XML 错误体），
  `make test-s3` 20 例常驻。载体选型是查出来的：MinIO 仓库已归档（末次 release
  2025-10-15）、LocalStack 已归档且许可 NOASSERTION、moto 属对 AWS 语义的二次实现
  （拿它验证错误码判读=循环自证），故选 Apache-2.0 且当日仍在提交的 VersityGW；
  MinIO 只用做一次性的**交叉核对**（同一次读数两台逐点一致 ⇒ 是 S3 通用行为）。
- 修 **HEAD 无响应体导致的误判**：HeadObject 对"key 不存在"与"桶不存在"只给同一种
  `Error.Code == "404"`，旧 `exists()` 于是把桶被删/配错读成"产物不存在"——正是它
  docstring 声称要避免的。现在 404 分支再探一次 HeadBucket。旧的 fake 用例看不见这个，
  是因为 fake 给 HEAD 编造了 `NoSuchBucket` 响应体（协议上不存在这种响应）。
- 修 **异常类型跨后端不一致**：Local 抛 `ArtifactStoreError`、S3 抛裸 `ClientError`；
  且 `verify_checksum` 用 `except Exception` 把任何存储故障一律写成
  "artifact object missing" 并把部署推进 **FAILED 终态（终态不再重验）**。
  现在家族分 `ArtifactNotFoundError` / `ArtifactStoreUnavailableError`：
  只有"确实不存在"或"checksum 不符"才判 FAILED，故障返回 **503** 且记录停在
  `downloading` 可重试（API.md 已写口径）。
- 接通生产装配：新增 `EMBODIEDCLOUD_ARTIFACT_BACKEND=local|s3` 与四项 S3 参数，
  由组合根选后端；`s3` 而凭据不全 → 装配期即 `BLOCKED_EXTERNAL_DEPENDENCY`，
  不静默回退 local。新增 `[s3]` 可选依赖组（boto3），dev extra 同步带上。
- 变异对照（本机实测）：M1 删桶探测 → 真实档红 3 条而**旧 fake 档红 0 条**；
  M1b 同一变异跑改写后的离线档 → 红 2 条（离线档也带牙）；M2 清空"不存在"码集 →
  真实档 5 + 离线 2；M3 verify 退回 `except Exception → _fail` → 红 2 条，
  且失败信息原样复现了谎报（`artifact object missing: S3 head_bucket failed`）。

### Docker `--gpus`：把 argv 交给守护进程自己验收（G0.19 扩到 20 例）
- provider 的 `docker run` 命令行抽成 `run_argv()`，真实档拿**生产同款 argv**
  `docker create` 后回读守护进程记账的 `HostConfig.DeviceRequests`：
  `--gpus device=N` → `{"DeviceIDs":["N"],"Capabilities":[["gpu"]]}`（N=0 与 3 两档），
  标签/Binds/env/hostNetwork 同样回读。测试不再重抄命令行。
- 把生产 argv 原样交给 `docker run`：本机（无 NVIDIA 运行时）失败于守护进程的
  GPU 发现（`failed to discover GPU vendor from CDI`）而非命令行用法错误，
  且失败后不留同名容器（`--rm` 会清掉启动失败的容器）⇒ provision 重试不会被名字冲突卡住。
  容器内**真的看得见 GPU** 仍属物理档（`scripts/gpu_acceptance.sh`），不假装验证。
- M4 变异（把 `--gpus device={gpu_index}` 写死成 `device=0`）→ 新增的守护进程记账档
  与既有离线档各自开火。

### K8s 控制面：kind 真集群档（ADR 0009，7 例常驻）
- `wait_ready` / `rotate_credentials` / `supports_credential_rotation` 此前从未执行：
  唯一相关档位的前置把"要控制面"和"要 GPU"绑在一起。现在用 kind 起真集群
  （真 kubelet/调度器/endpoints 控制器），只允许**一处** fixture 差异（container
  `resources`），其余对象图与 GPU limit 声明全部由 `provision()` 产出并逐项回读。
- 真集群读数：生产资源声明被集群自己拒绝（`Insufficient cpu, 1 Insufficient memory,
  1 Insufficient nvidia.com/gpu`，节点 allocatable 实测 4 CPU / 5.8 GiB）；
  M6 变异（`wait_ready` 直接 return True）红 3 条——含"提前放行后独立复核读到
  `available_replicas=None`"，说明正向用例不信 provider 的判读。
- 三条工具层事实（都实测）：strategic merge patch 对 `resources.limits` 是**按键合并**
  （GPU limit 会在 patch 后存活）；`replace` 会因控制器先写 status 撞 409（改为读→改→写
  有界重试）；判断"spec 是否被动过"要看 `generation` 而非 `resourceVersion`
  （实测 740 → 744 而 spec 未变）。
- 新增 `EMBODIEDCLOUD_K8S_KUBECONFIG`：kubernetes Python SDK 在**模块 import 时**就把
  `KUBECONFIG` 固化成常量（读到源码 `KUBE_CONFIG_DEFAULT_LOCATION`，并本机复现：
  进程起来后再 export 该变量 → `Invalid kube-config file`）。

### 供应链：镜像配方钉死 + 机检（SUPPLY_CHAIN §2/§3/§6）
- code-server 4.130.0 两架构 tarball 加 `sha256sum -c`（**上游这一版不发布校验文件**：
  下载其 `SHA256SUMS.txt` 实测 `Not Found`，release notes 也无校验表 ⇒ 摘要来自本机对
  官方制品的实算，字节数与 GitHub API 报告值 201284549 / 197540112 逐一吻合，
  只防后续构建拿到被替换/截断的制品，不是第三方背书）；`sha256sum -c` 机制本身
  做了正确/错误两档对照。
- IsaacLab `v3.0.0-beta2.patch1` 钉到 commit `ffff603e…`（GitHub refs API 与
  `git ls-remote` 两个来源同一 sha），clone 后比对 HEAD。
- 新常驻门禁 `tests/test_supply_chain.py`：`runtime/Dockerfile*` 里每个下载步骤必须同块
  `sha256sum -c`、每个 `git clone --branch` 必须比对 HEAD commit，并断言判据作用域非空。
  改钉之前它对两处开火（读数的具体文件名见 ADR/登记）。
- CI：测试镜像拉取（新增 versitygw、kindest/node）与 kind 安装（按上游 `.sha256sum` 校验）
  移到 `make validate` **之前**——原顺序是"先 validate 后拉镜像"，档位读数永远来自
  镜像还没缓存的那一刻。
- 顺带更正两条文档事实错误：code-server 下载不在 `scripts/build_workspace_image.sh`
  而在 `runtime/Dockerfile.isaaclab-workspace`；§14 的 Pod 标记实测是
  `embodiedcloud.workspace="true"`（workspace id 在 Deployment 标签上）。

## 0.5.0 — 2026-09-26（验证纵深 + 计费预授权）

### 四档"本环境做不到"的判据变成常驻门禁
- **PostgreSQL 真并发档（17 例）**：`tests/pg_server.py` 自建一次性容器 + 每例独占库
  （跑完整 13 级迁移链，`DROP DATABASE … WITH (FORCE)` 收尾）。就绪判据走测试真正
  使用的那条路（对发布端口建 TCP 连接 `SELECT 1`），不用容器内 `pg_isready`——官方
  镜像在 initdb 期间会先起一个只监听 unix socket 的临时服务器，用它判就绪是假绿。
  覆盖：`FOR UPDATE` 必阻塞 / `SKIP LOCKED` 必放行的成对判据、并发 allocate 不重复
  占用、worker lease CAS、账本 8 线程幂等、部分唯一索引串行、warm pool CAS 单赢家、
  BillingAccount 行锁两档并排。
- **Docker provider 真容器档（17 例）**：`health/start/inspect/logs/wait_ready/
  reconcile` 与流式占用门禁首次在真实守护进程上执行；按 daemon 架构匹配镜像
  （否则容器秒退、`--rm` 把"退出但存在"掩盖成"不存在"）、宿主 http_server 提供真实
  下载源、会话级泄漏守卫。
- **浏览器档（11 例）**：Playwright 驱动系统 Chrome 真 DOM，替代"grep 前端源码"。
  含存储型 XSS 载荷在 DOM 中确实不执行、终态/页面隐藏时轮询真的停（`pollTimer === null`）、
  控制台零错误（由此抓出并修掉 favicon 404）。
- **K8s 线格式合规档（6 例）**：provider 生成的对象过真实 SDK 的
  `sanitize_for_serialization`，不再只与自造 fake 对拍。真实集群验收仍以
  `K8S_PHYSICAL_VALIDATION_PENDING` 显式登记，不用 skip 冒充。

### 计费：主体行 + 启动预授权（§17/§18）
- `BillingAccount(subject_type, subject_id)` 唯一：把"user/org 两个 FK 聚合视角"收敛
  成一行，作为预授权前的串行化根；迁移按现存 users/organizations 回填。
- `CreditHold` 独立可变表（不进 append-only 账本，理由见 ADR 0004）：provision 前
  `reserve_launch` 圈住最低额度、结算 `capture_hold` 转正、失败/销毁 `release_hold`
  退回、worker 周期扫 `expires_at` 兜崩溃残留（RUNNING 段不回收）。
- 可用额度改为「账本毛余额 − pending hold」；`enforce_preauthorization=False`
  （本地/演示）时零写行。
- 连带修两处真实竞态：`reserve_launch` 与 `account_for` 在并发首批请求下会漏出
  `UniqueViolation`/`IntegrityError`，改为回滚后收敛到已存在行。

### 缺陷修复
- **GPU 分配锁范围**：`allocate` 原先对全部候选 `FOR UPDATE`，一次启动锁住整片 GPU，
  并发启动互相饿死；改为每次 `limit(1) + SKIP LOCKED` + 有界重试，并区分"没剩下"
  与"被别人持着"。SQLite 档结构性看不见此缺陷。
- **生命周期谎报**：start/stop/delete 忽略入队返回值，队列拒绝时仍回 2xx；改判 409，
  且 `start` 不再先提交 `QUEUED`（ADR 0006，附变异对照）。
- **`edge_agents` 租户外键从未存在**：`c7c6f510d21f` 只加列未加约束，SQLite 与
  PostgreSQL 都没有它；`3f0c9a51b7e2` 补建，并把"模型↔迁移"对账（`compare_metadata`）
  接进默认档门禁，防同类漂移。
- **SQLite 外键默认不校验**：`make_engine` 逐连接 `PRAGMA foreign_keys=ON`（ADR 0005）。
- released-version 回退改为全序（同秒并列导致的 flake 根因）；零秒运行段的 hold
  由无条件 capture 收口。

### 引用完整性（v0.5.0 收口追加）
- 普查出 19 处「列里存的是别表主键、却从未声明外键」的 `*_id` 列，按**全仓删除能力**
  逐条裁决（唯一的硬删是 `GpuAllocation`，且没有表按 id 引用它）：11 处补上外键
  （`b7e4c1a09f52`），8 处保留不声明并写明理由（互指环 / slug 形态 / 账本历史 / 多态主体）。
- 互指对只保留一条方向：两边都加外键会让 Alembic 的 `compare_metadata` 发
  "unresolvable cycles" 并静默跳过该环内的全部外键比较——对账门从此把这类警告本身判红。
- 顺带修出 `allocate()` 的错误分类缺陷：它把任何 `IntegrityError` 都当成"卡被抢了"，
  于是外键挡下的脏 workspace_id 被报成"没有空闲卡"。现按 SQLSTATE 与约束文案区分，
  非唯一冲突直接抛真实原因（双向用例已验）。

### 测试基建
- 22 处模块级 `test-*.db` 文件库改为**按 pid 独占**（`tests/dbfiles.py`）。此前同一仓库
  并发跑两个 pytest 会互相清库：HEAD 工作树两进程并排实测 → 31 / 26 例假红（登录、
  隔离、账本类全断）且两份 rc=1；改后同一并发对照两份 rc=0、零 FAILED。`make clean`
  随之收 `test-*.db`。

### 测试基建（续）
- 抽取 4 份重复的注册/鉴权夹具到 `tests/http_auth.py`。先做逐字比对再动手：请求体、
  端点、`assert 201` 四处完全相同，`_auth` 一字不差，只有"返回 token / token+id /
  元组"这层投影各随用例需要 —— 所以共享的是会漂移的契约部分，投影留在原地。
  抽取后用例数与结果不变。

### 登记册事实校正（实测，不改代码）
- `default_idle_timeout_minutes` 代码侧**零消费者**（`grep` 读数：仅 `app/config.py` 一处
  声明；`.env.example` 有它是被配置文档门要求的）。它不是"已实现待调参"，而是尚未实现的
  预留开关——按容器 CPU 判空闲会误杀 GPU 长跑任务并照秒扣费，可信信号要真机 GPU 利用率
  （仍被硬件阻塞）。
- "edge agent 独立包（§25）"实为**组件缺失**：仓库里没有 agent 客户端，且服务端缺少
  agent 侧的工作发现与取件通路（agent token 只能 heartbeat/telemetry/report-checksum）。
  落地需要先定"分派发现方式"和"取件鉴权"两件事，已记为 ADR 0007（Proposed，含六维
  外部方案对比；本轮不新增鉴权面）。

### 供应链
- 提交 universal `uv.lock`（多平台 marker + sha256），CI 跑 `make verify-lock`。
- `uv.lock` 里本项目自身的版本也纳入版本一致性用例：只 bump `pyproject.toml` 而忘了
  `uv lock` 时，过去要等到 release 第 4.1 步才红，现在 `make test` 就红（本轮真实踩过）。
- SBOM（`uv export --format cyclonedx1.5`）+ `uv audit --locked` 进 release 步骤与
  `dist/checksums.txt`；CI 要求"需要 docker 的集成档必须真 PASS，否则红"。

### 文档
- 新增 ADR 0004（hold 为何独立成表）、0005（SQLite/PG 语义差与两层验证）、
  0006（冲突即 409）；API.md 补 409 语义与 402 额度口径；CURRENT_STATE /
  ACCEPTANCE_GATES / SUPPLY_CHAIN / ARCHITECTURE / OPERATIONS / MASTER_PLAN 按实测读数对齐。
- 两条"写在文档里的约定"接进常驻对账：模型声明 ↔ 迁移产物（`compare_metadata`）、
  `Settings` 字段 ↔ `.env.example`（双向）。后者落地即开火——它抓出了本轮自己漏文档的
  `billing_hold_ttl_minutes`。

## 0.4.0 — 2026-08-14（Product UX Iteration）

### 前端重构（多视图 SPA，仍为无构建工具链的静态资源）
- 概览 / 用量与账单 / 课程 / 部署·Sim2Real / 边缘设备 / GPU 管理 六视图 + hash 路由；
  此前前端仅 68 行 JS 只覆盖登录+模板+工作区，后端 9 组路由大部分能力（账本/课程/
  部署/Edge/流/GPU）前端零入口。
- 用量页：per-workspace 明细（已结算+live 秒、按模板费率估算 ¥）、不可变账本明细表、
  充值（演示语义）、口径说明（1 credit = 1 GPU 秒）。
- 课程页：slug 邀请码加入、老师建课/成员/实验/作业/全班完成矩阵，学生一键启动实验、
  查看我的进度、提交作业。
- 部署页：工作区 → artifact 路径 → 创建部署，download/verify/run/complete 状态机操作 +
  checksum 展示；演示模式可一键生成模拟 checkpoint 端到端走通。
- 边缘设备页：注册（token 一次性展示+复制，本地保存供心跳/遥测代发）、心跳、遥测。
- 流会话：工作区卡片内联会话面板（start/connect/disconnect/reconnect 状态机）。
- GPU 管理页（admin）：inventory/hosts、维护/异常流转。
- 状态反馈：工作区瞬态自动轮询（终态即停、页面隐藏暂停）、进行中 spinner、
  状态→中文映射、按状态渲染可用操作（修复「非 running 一律显示启动」）。
- 破坏性操作确认弹窗 + in-flight 防重（防双击重复创建/误删）。
- 安全：所有用户可控内容渲染前 HTML 转义（消除存储型 XSS：workspace 名/错误信息/
  模板字段/日志标题）；/demo-workspace 后端同步转义 name/launch_command 并按真实
  状态渲染徽标（不再无条件 RUNNING）；IDE 密码改为「复制密码」按钮而非明文 toast。
- 可用性：登录/注册 tab、表单回车提交、autocomplete 语义、刷新页面后 /api/auth/me
  校验真实用户（修复 token 前缀 + undefined 角色）、模板匿名可浏览、逐区块错误态+
  重试、toast 定时器清理、aria-live/焦点环/skip-link、对比度调优、移动端响应式。

### 后端（UX 支撑 + 清理）
- 新增端点：`POST /api/courses/join-by-slug`（邀请码加入）、
  `GET /api/courses/{id}/my-progress`（我的作业进度）、
  `POST /api/workspaces/{id}/demo-checkpoint`（mock 专用演示产出；非 mock 400）。
- 权限修正：labs/assignments 列表对 member 可读（学生此前无法看到要 launch 的实验）。
- /demo-workspace：鉴权（owner/admin）+ HTML 转义 + 真实状态徽标 + 非运行中提示。
- 清理死代码：WorkspaceStatusLegacy、require_admin、scheduler.release_all_for_workspaces、
  ledger.history、warmpool.drain/mark_failed、logging_setup.new_request_id/workspace_log_context。
- config/.env.example 对齐：password_pepper 注释修正（生产 fail-closed）、
  PROVISION_READY_TIMEOUT_SECONDS、K8S_GPU_MEMORY_MB；idle timeout 标注为预留。

### 测试
- 新增：test_auth.py（login/logout/me + verify_password 边界 + token 仅存哈希）、
  test_course_onboarding.py（slug 加入/member 可见/progress/launch 402）、
  test_demo_checkpoint.py、test_gpu_admin.py（admin inventory + GPU 分配→释放真实断言）、
  test_api_success_paths.py（templates/{id}/start/admin/all 成功路径）、
  demo 页 XSS 转义回归。
- 修正伪覆盖：deployment_verification「恒真 VERIFIED」改为「PENDING 直接 verify 409 防绕过」；
  迁移测试校验关键表落地/移除；admin_adjustment 补 HTTP 层 403/200；跨部署 checksum
  改为真实「两个不同 checksum 的 A→B 互报」；provision_rollback 四个失败注入点改为
  真实分阶段（GPU 分配后/容器创建后/端点配置后/readiness gate），K8s 补偿清理改为
  精确计数断言（provision 内补偿 + destroy 各删一次）+ Deployment 失败双清理用例 +
  404/非 404 分支；test_k8s_inventory 改为直接调用真实 `deps._sync_k8s_gpus`（删除
  复刻版 `_sync_inventory`）；test_k8s_node_truth 改为真实 orchestrator 链路捕获
  reservation（删除复制构造自证）；新增 make_tripwire_models 安全哨兵 fake，把
  「未设置 privileged/hostNetwork/hostPath/security_context」从恒真断言变为必然失败
  哨兵；streaming 补 FAILED 终态不可迁移测试（此前只有注释）；warmpool 观测切到
  pool_metrics 真实按 state 计数（删除 legacy metrics）；修测试全局状态泄漏
  （SEED_TEMPLATES try/finally、RETRY_BASE_DELAY 恢复、端口池释放）。

### 文档
- API.md 重写为全量端点参考；ARCHITECTURE.md 对齐代码（Provider 协议/routers/数据模型/
  reconcile 语义）；ACCEPTANCE_GATES 去重与数字刷新；CURRENT_STATE 重写；
  过时模板 slug（GPU_HOST/ACCEPTANCE）修正；四份 08-13 review 报告加「已修复」免责头；
  版本标号统一 0.4.0。

## 0.3.0 — 2026-08-12（Acceptance Hardening）

### Correctness（P0）
- GPU 单一权威保持：scheduler reservation ↔ provider `--gpus` 一致性测试链（test_gpu_single_authority）。
- **Operation lease/fencing**：lease_owner/fencing_token/heartbeat_at；claim 原子（rowcount）；
  执行期心跳续期；finish 必须 fencing 验证（LeaseLostError 禁止过期 worker 写终态）；
  SQL 层比较（SQLite/PostgreSQL 语义一致）。
- **K8s offline 隔离**：model_factory 注入（tests/k8s_fakes.py），offline 测试零 Kubernetes SDK 依赖。
- **K8s inventory 真实路径**：node nvidia.com/gpu capacity → GpuHost/Gpu（capacity reservation，
  device 分配归 NVIDIA Device Plugin）。
- K8s integration harness 真实全流程（无 NotImplementedError；无集群 SKIP 标 PENDING）。

### Product hardening
- Warm pool 真实 launch 路径：POST /api/workspaces → BillingPolicy → claim；credential rotation
  （Mock/Docker/K8s 三实现；rotation 失败不得交付 → DRAINING + fallback）。
- Warm pool 指标 COUNT(*) 真实计数。
- ArtifactStore 集成：DeploymentService 走 store 协议（object_key/content_type/store_name）。
- **Edge 上报 checksum 协议**：edge 本地 sha256 → server 比较 → VERIFIED/FAILED（防绕过/防 replay）。
- Template.current_version_id 确定性版本指针（不用 created_at 猜 latest），Artifact/Deployment
  版本来自 Workspace.template_version_id。
- Billing 预授权（minimum_launch_minutes）+ active-runtime quota monitor（透支优雅停止）。
- 凭据配置生产安全：EMBODIEDCLOUD_WORKSPACE_CREDENTIAL_KEY；生产 provider 未显式配置拒绝启动；
  enc: 密文解密失败 fail closed。

### Process
- scripts/validate_release.py 自动生成 docs/VALIDATION.json/.md（CI freshness 门禁）。
- 版本统一 0.3.0（pyproject/app/Makefile/CHANGELOG/OpenAPI 单一来源）。
- Release 清洁验证（archive 不含 __pycache__/pyc/test db/.env）。

## 0.2.0 — 2026-08-12

### Identity / Isolation
- User / Organization / Role（user、admin；预留 org_admin/instructor/student）+ 会话认证（PBKDF2-SHA256 600k 迭代、session token 仅存哈希）。
- 全部 Workspace/Ledger/Course/Deployment 资源 owner/org 隔离；越权访问返回 404（测试：tests/test_isolation.py）。

### Control plane core
- 数据模型全面扩展 + Alembic 迁移体系（up/down 循环测试）；SQLite 本地 / PostgreSQL 生产。
- GPU Scheduler：inventory（host/gpu）、原子分配（FOR UPDATE + 唯一约束兜底）、并发不重复、unhealthy/draining 不调度、stop/delete 释放、crash recovery。
- Provider 接口完备（inspect/logs/health）+ KubernetesWorkspaceProvider（可离线单测）。
- 不可变 CreditLedger：RECHARGE/USAGE/PROMOTION/REFUND/ADJUSTMENT，幂等键防重复扣款，同一运行段只结算一次。
- WorkspaceStatus 增加 CREATED/DELETED；异步 provisioning crash recovery。

### Product capabilities
- Template Registry：slug/version/image/gpu_requirement/entrypoints/outputs/streaming/healthcheck/metadata；5 个 Golden Template 全部 version locked，禁 latest。
- Streaming 状态机（starting/ready/connected/disconnected/failed）+ 端口清理 + 模拟链路测试。
- Warm Pool：pool manager + metrics + benchmark harness（P50/P95）。
- 高校 Course/Lab/Assignment/Submission 教师/学生流程。
- Edge Agent（register/heartbeat/device_info/download/verify/start/stop/telemetry）+ RobotDriver interface + MockRobotDriver + Deployment（checkpoint→artifact→checksum→deploy→verify→run）。

### Observability & Ops
- /metrics Prometheus（workspace_launch_*/gpu_*/template_*/stream_*）；JSON 结构化日志 + request_id 中间件 + 敏感字段脱敏。
- CLI：bootstrap-admin / list-gpus / show-usage / make-session。
- G1–G4 GPU 验收脚本（preflight / gpu_acceptance / isaac_sim_smoke / isaac_lab_cartpole_smoke / franka_smoke）；无 GPU 环境输出 BLOCKED_EXTERNAL_DEPENDENCY，不标 PASS。
- release.sh：semver 校验 → lint/type/test → build → checksums → 分级验证矩阵（Software/GPU/Streaming/Robot Verified 严格区分）。
- 84 tests 全绿；lint/typecheck/build/smoke 全绿。

## 0.1.0 — 2026-08-11

### Product
- 收敛为 Isaac Lab Cloud Workspace，不实现 BP 中的国产仿真引擎/通用硬件/资产商城。
- Golden Template 作为最小产品 SKU。

### Control plane
- FastAPI dashboard/API、Template Catalog、Workspace lifecycle、usage estimate。
- Mock Provider 可无 GPU 完整演示。
- Workspace access endpoint 返回 v0.1 code-server access secret。

### GPU runtime
- Single-host Docker Provider，一物理 GPU 一 Workspace。
- Isaac Sim 6.0.1 + Isaac Lab v3.0.0-beta2.patch1 workspace image recipe。
- code-server 4.130.0。
- Isaac Lab Streaming 由用户启动的进程自身承载，避免双 Isaac Sim 实例。
- v0.1 一宿主最多 1 个 public WebRTC stream（49100/TCP + 47998/UDP）。

### Operations
- GPU preflight / NVIDIA compatibility acceptance / image build / control-plane run scripts。
- Docker Compose、Nginx、Kubernetes control-plane skeleton。
- Pytest、compile/shell checks、GitHub Actions CI。

# CURRENT_STATE — EmbodiedCloud

> 版本：**0.7.0 边缘设备真进程通路（发现 / 开门 / 取件 / 遥测回读）**（2026-09-26）。
> 数据来源：`docs/VALIDATION.json`（`make validate` 自动生成，JUnit 稳定计数）。

## 1. 本次真实验证（实测，非复制旧文档）

| Gate | 结果 |
|---|---|
| Test | **PASS（collected 521 / passed 520 / skipped 1 / failed 0）**（本串与 `docs/VALIDATION.json` 由常驻判据对账） |
| Lint / Type | PASS（ruff 0 / mypy 45 files：`app` + 本轮入册的 `edge_agent`） |
| Migration | PASS（clean DB empty→head **14 文件链** + schema 落地 + downgrade 循环 + **模型↔迁移对账**） |
| Integration PostgreSQL | **PASS 18/18**（自建一次性容器，真行锁语义） |
| Integration Docker | **PASS 20/20**（真容器；`--gpus` 参数的守护进程侧记账 3 例自 v0.6.0 起在册） |
| Integration Browser | **PASS 11/11**（Playwright 驱动系统 Chrome 真 DOM） |
| Integration Object store | **PASS 20/20**（一次性 VersityGW 容器 + 真实 boto3；MinIO 交叉核对读数一致） |
| Integration K8s control plane | **PASS 7/7**（kind 真集群：真 kubelet/调度器/endpoints，无需 GPU） |
| Edge agent 真进程通路 | **PASS**（真 uvicorn 子进程 + 真 `python -m edge_agent` 子进程 + mock 驱动：一轮到 VERIFIED、落盘摘要核对、第二轮不重复上机；服务端另有 10 例鉴权/防线 + 设备侧 12 例坏响应形状） |
| Integration K8s（GPU 全流程） | PENDING（原因登记：需要节点带 `nvidia.com/gpu` 容量 = Device Plugin；控制面路径已由上一行覆盖） |
| 供应链 | PASS（`uv.lock` 一致性 + SBOM + `uv audit --locked` + 镜像配方下载/克隆钉死机检 + **外部基础镜像钉 digest 且消费侧逐字同源** + **workspace 镜像 digest 有写入入口与读者**） |
| OpenAPI / VALIDATION freshness | PASS（`make api-docs` / `make validate` 无 diff） |
| overall | `PASS_WITH_PHYSICAL_PENDING`（物理待验：GPU 真机、Isaac 流媒体面、真机器人） |

## 2. v0.7.0 本轮交付

| § | 内容 | 验证 |
|---|---|---|
| N-1 | **边缘设备真进程通路落地（§25）**：设备侧新增 发现 / 开门 / 取件 三条 `X-Agent-Token` 端点，`POST /deployments` 支持部署期指派 `edge_agent_id`；ADR 0007 由 Proposed 转 Accepted，两个前置问题都按一手依据裁决（AWS IoT Jobs 生命周期页：`QUEUED` 由服务端 rollout、`IN_PROGRESS` 由设备发起、取任务走独立 API 而不是通知通道） | `tests/test_edge_agent_api.py` 10 例 + ADR 0007 的 M1/M2/M3 开火读数 |
| N-2 | **取件端点的三道防线**：租户与绑定校验（越权 404）、`downloading` 状态前提（未 begin → 409）、`object_key` 的 workspace 前缀复核（一行被改写的库记录也读不到别人的件）；存储故障按 v0.6.0 口径返回 503 且记录留在 `downloading` | 逐道"拆掉即红"：M1 下攻击者真拿到 victim 字节（200 而非 404） |
| N-3 | 修 **遥测只写不读**：`telemetry_events` 自 v0.4 起一直在写、全仓零读路径；新增 `GET /api/edge/agents/{id}/telemetry`（租户 scope、越权 404） | e2e 用它核对 `edge-run` 恰好一条、payload 摘要与登记一致 |
| N-4 | 新增设备侧包 **`edge_agent/`**（只依赖标准库、不 import `app`）：流式取件边写边算 sha256、体积熔断、`.part` 原子改名、核对不过不留半成品；CLI 凭据走 env 而非 argv；base_url 限 http(s)；错误文本不含 token | `tests/test_edge_agent_client.py` 12 例（坏响应形状）+ e2e 的磁盘现场 |
| N-5 | **真进程 Sim2Real e2e**：真 uvicorn 子进程 + 真 `python -m edge_agent` 子进程 + mock 驱动，断言一轮到 VERIFIED、落盘字节与登记摘要一致、无 `.part` 残留、第二轮不重复上机 | `tests/test_edge_agent_e2e.py`；G5.1 判据据此升级 |
| N-6 | 修 **断言把进度外包给调度器**：旧 `_wait_status` 是 sleep+读 HTTP，两个 pytest 进程并排跑时红过（"20s 内没到 running"）。新 `tests/workspace_progress.py` 每轮先 `worker.tick_once()` 自己推进（claim 是 CAS+fencing，胜者唯一），超时信息带当前状态与操作队列 | `tests/test_workspace_progress.py` 2 例：掐掉后台线程后主动档到得了、被动档到不了（后者是前提档，它若读到 running 就说明对照失效） |
| N-7 | 修 **共享测试库的 GPU 池饿死**：全套共用一个 SQLite、mock 只 seed 8 张卡且用例不还卡。单变量配对定位污染源（本轮新文件 13 个 workspace × `test_gpu_admin` 即红；其余 55 个文件逐个配上去都不红）。新文件模块级归还自己占的卡，需要空闲卡的用例显式 `ensure_free_gpus`（走 `GpuScheduler.release`，不手写 UPDATE） | 红→绿配对复跑；坑与读法写进 OPERATIONS §7 |
| N-8 | 真起 uvicorn 的夹具从浏览器档抽成 `tests/live_server.py`，浏览器档与 agent e2e 共用同一份就绪判据与回收顺序 | 迁移后浏览器档 11/11 重跑为绿（24.0s ≈ 原 22–24s） |
| N-9 | **发布报告必须能归因**：`test_run.failed_names` 由 JUnit 结构属性得出（`failure` 与 `error` 两类都算），`VALIDATION.md` 行内展示；只带名字不带 message 是故意的——报告必须确定性（CI freshness 比 `git diff`），而失败消息里带时间/端口 | `test_report_carries_the_names_of_failing_cases`：4 条里 2 条红必须恰好点出那两条；全绿必须给出空列表 |
| N-10 | 新常驻判据：`make lint`/`make typecheck` 与 release 门禁的 ruff/mypy 目标集合必须同源相等（并钉 `edge_agent` 在册）；`edge_agent` 已进 packaging/lint/mypy | 开火读数：从门禁侧删掉 `edge_agent` 即红（`make=[app,edge_agent,tests]` vs `gate=[app,tests]`） |
| N-11 | **Isaac Sim 基础镜像钉 digest，并揭掉一条写错的阻塞理由**：`runtime/Dockerfile.isaaclab-workspace` 的 `FROM` 由裸 tag 换成 `nvcr.io/nvidia/isaac-sim:6.0.1@sha256:783444c7…30aa9`（多架构索引，子清单 amd64 `b1c542b2…`／arm64 `20269735…`）。此前 SUPPLY_CHAIN §8 登记为"有 NGC 凭据后改 @sha256:"——**实测该前提不成立**：manifest 与 digest 用匿名 pull 令牌即可解析（`docker-content-digest` 与 body 重算 sha256 两条独立读数吻合），凭据只在拉层字节时才要。新增三条常驻判据：①非自有命名空间的 `FROM` 必须带 `@sha256:`，未钉者必须与例外登记表**双向**对账（多登记＝死免检、漏登记＝新裸 tag，都红），例外须带固定词表的证据等级与 ≥40 字理由，且与 `docs/SUPPLY_CHAIN.md` 逐字互核；②消费侧（`gpu_acceptance.sh`／`isaac_sim_smoke.sh`／`release.sh`／`docs/GPU_HOST.md`）引用同一基础镜像时必须与 Dockerfile 钉死的那份**逐字相等**（归属键刻意剥掉 tag，否则"把 2.0.0 写成 1.9.9"这种最常见漂移根本进不了比较）；③三条判据的作用域均须非空，②另按"必须覆盖到哪几个文件"做子集断言 | `tests/test_supply_chain.py` 10 例（含 `_image_path` 纯文本函数的端口/无 tag 分支单独验）；真实内容变异电池 SC1–SC6 六支全开火、干净副本 control 不开火（读数见 CHANGELOG 与 SUPPLY_CHAIN §2/§6）。`python:3.12-slim` 按例外登记而未钉：Docker Hub 三端点本机均不可达，唯一拿到的第三方镜像站读数与本机缓存互不印证（详见 §2 同一行），钉一个未证实的 digest 会让构建直接失败 |
| N-12 | 把 TECH DEBT 里那条"设了也不生效"的预留开关从**文档陈述**升级为**机器不变量**：AST 逐字段数出 `app/`（除声明文件）里的读取位置，"零读取字段集合"必须恰好等于惰性登记表 `INERT_SETTINGS`（漏登记＝有人会被误导，死登记＝文档在撒谎），且登记项在 `.env.example` 紧邻上方注释块必须带"未启用"标记 | 实测普查：`Settings` 34 个字段中恰好 1 个零读取（`default_idle_timeout_minutes`）。开火读数 CFG1/CFG2 + 非恒真对照（`ide_port_start` 探针必须读到非空）+ 纯函数四档边界（无注释／断一行／写了"预留"但没写"未启用"／合规）全在 /tmp 副本上量，主树不动 |
| N-13 | **`TemplateVersion.image_digest` 从装饰性字段变成有写入入口、也有读者的一列**：普查读数是"自 v0.4 建模以来 0 处写入、0 处读取"（列存在不等于镜像被钉住，provider 一直启动可变 tag）。新增 `app/services/image_ref.py:pinned_ref`（workspace 快照那一刻拼成 `image@sha256:…`，形制不对／与 image 内已有摘要冲突／拼完超过列宽则**拒绝**而不是静默退回 tag）+ CLI `record-image-digest`（幂等；已钉在另一摘要的 released 版本拒改，退码 3 表示"该发布新版本而不是就地改写"）+ `build_workspace_image.sh` 构建后打印摘要、拿不到就退 2 | 整条链在同一份真库上连跑（回填→新建工作区快照→docker argv 里的 token），另有"回填不改历史工作区快照"的断言；变异对照 PIN1（快照处退回原写法）／PIN2（放过坏形制）／PIN3（允许就地挪针）／PIN4（吞掉退码）各自翻红，`image_digest` 有生产读者的普查判据带 `current_version_id` 作非恒真对照 |
| N-14 | **GPU 分配策略从"写在 SQL 里的习惯"变成量过并被钉住的判据**：`allocate()` 的 ORDER BY 抽成 `candidate_order()`，新增 `tests/scheduler_policy_lab.py`（同一个分配器、同一份工作负载，只换排序）与 `make policy-bench`。实测（合成舰队 192 GiB、负载合计恰等于容量）：best_fit 8/8 全接、48 GiB 两张都留得住、浪费率 1.00；arrival 与 pack_host 各 6/8、worst_fit 4/8，三者一个大任务都接不下 | `tests/test_scheduler_policy.py` 4 例：现产排序**按表达式直比**（不经过认档函数）、其余三档必须都接不满、实测表逐格钉值、跑完必须复原生产排序。变异 POL1 改向→2 红、POL2 换 pack_host→红、POL3 漏复原→2 红、POL4 让排序不生效（实测台失去区分力）→2 红、POL6「认档函数说谎 + 生产改向」组合→直比那条红（POL5 只说谎不改今天的判决，如实记为未变） |
| N-15 | 真集群档的一条**前提竞态**被一次真实红抓出并修掉：负向对照在 `wait_ready(...) is False` 之后直接 `_pods(...)[0]`，而 Pod 及其 conditions 是集群控制器写的——本轮实测 517 passed / 1 failed，红的正是取 Pod 那一行的 `IndexError`（同一次运行里其余 6 条真集群用例全绿，所以不是引导失败也不是产品缺陷） | 判据行留在最前，佐证改用 `tests/k8s_server.poll_until`/`await_pod`/`await_condition_reason`：拿不到前提时抛**"前提未达成"并带最后一次读数**，而不是把夹具竞态报成产品结论；离线控制 2 例（晚出现的 Pod 要轮询到第 3 次、永不出现要红、判词侧同理）。修后单档复跑 7/7（254s，kind 建集群本身就要 4 分钟，6s 判定窗口输掉竞争毫不奇怪） |


## 3. 上一轮交付（v0.6.0 / v0.5.0）

### v0.6.0（2026-09-26）

| § | 内容 | 验证 |
|---|---|---|
| O-1 | **对象存储真实后端档**：`tests/s3_server.py` 一次性 VersityGW v1.8.0 容器 + 真实 boto3 线协议（此前 S3 分支从未执行：boto3 不在依赖里、6 条用例全 monkeypatch）。选型读数：MinIO 仓库已归档、LocalStack 已归档且许可 NOASSERTION、moto 是二次实现（不能拿它验证错误码判读） | `make test-s3` 20 例；ADR 0008 |
| O-2 | 修 **HEAD 无 body ⇒「桶不存在」被读成「对象不存在」**：`exists()` 在 404 分支再探 HeadBucket；两台独立服务端读数逐点一致（证明是 S3 通用行为，非厂商方言） | 真实档 + 改写后的离线档（按实测形状造载荷）同判据并排；变异 M1/M1b/M2 读数见 ADR 0008 |
| O-3 | 修 **异常跨后端不一致 + 故障被写成终态**：拆 `ArtifactNotFoundError` / `ArtifactStoreUnavailableError`，`verify_checksum` 只对"确实不存在"判 FAILED，故障 → **503** 且记录停在 `downloading` 可重验 | `test_deployment_bypass.py` 新增 3 条 HTTP 级用例；M3 变异复现旧谎报 |
| O-4 | 接通 **`EMBODIEDCLOUD_ARTIFACT_BACKEND=local\|s3`** 与 `[s3]` 依赖组：装配期 fail-closed，不静默回退 local | `test_artifact_store.py` 装配用例（含"失败路径不留半个目录"） |
| O-5 | **`--gpus` 交给守护进程验收**：provider argv 抽成 `run_argv()`，真实档 `docker create` 后回读 `HostConfig.DeviceRequests` 证明按 reservation 的 index 绑定；并证明启动失败不留同名孤儿容器（重试不被名字冲突卡住） | `make test-docker` 20/20；M4 变异（写死 `device=0`）开火 |
| O-6 | **K8s 控制面真集群档（kind）**：`wait_ready` 正/负两档、provision 对象图逐项回读、凭据轮换真的滚出新 Pod 且无 Pod 持旧口令、stop/start/destroy 无残留 | `make test-k8s-control-plane` 7/7；M6 变异红 3 条（含独立复核不被骗过）；ADR 0009 |
| O-7 | 修 **SDK 在 import 期固化 `KUBECONFIG`** 导致无法在进程生命周期内指路：新增 `EMBODIEDCLOUD_K8S_KUBECONFIG` | 本机复现 `Invalid kube-config file`，改后 7/7 绿 |
| O-8 | **镜像配方钉死 + 机检**：code-server 两架构 tarball `sha256sum -c`（上游该版不发校验文件，摘要来源与限制如实登记）、IsaacLab tag→commit `ffff603e…`、新常驻判据要求每个下载/克隆步骤都校验且作用域非空 | `tests/test_supply_chain.py` 3 例；改钉前对两处开火（读数见 CHANGELOG） |
| O-9 | **CI 顺序修正**：测试镜像拉取与 kind 安装移到 `make validate` 之前，否则 VALIDATION 读数来自镜像尚未缓存的那一刻 | `.github/workflows/ci.yml` |
| O-10 | **文档 ↔ 实测计数对账**：CHANGELOG 当前版本节与本文的计数串必须等于本次报告。值对账放在 `validate_release.py` 汇总之前；pytest 只判"恰好一处"的形状，因为 pytest 阶段读到的必然是上一次的报告（把值比较放那儿会造出不收敛的自引用，本轮真实踩过） | `tests/test_version_consistency.py` 3 例 + `docs_test_counts` 门禁（开火对照：喂错数字必须两处点名） |


### v0.5.0（2026-09-25）

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
| G-2 | **注册/鉴权夹具抽取**：4 份 `_register` 经逐字对比确认请求体、端点与 201 断言完全相同（只差返回值投影），`_auth` 四处一字不差 → 共享核心落到 `tests/http_auth.py`，各模块只留自己那一行投影；用例数与结果不变（410 passed） |
| T-1 | **测试库进程隔离**：22 处模块级 `test-*.db` 文件库改为 pid 独占名（`tests/dbfiles.py`）。此前同一仓库并发跑两个 pytest 会互清对方的库。
改前基线（HEAD 工作树、两进程并排）：一份 31 例假红 rc=1、另一份 26 例假红 rc=1；
改后同一并发对照：两份 rc=0、FAILED 计数 0/0 | 正反两档都实测（并发即判据） |
| F-6 | **引用完整性收口**：19 处未声明外键的 `*_id` 列按删除能力普查逐条裁决，11 处补约束（`b7e4c1a09f52`）、8 处留理由；互指对只保留一条方向（两边都加会让 `compare_metadata` 静默跳过整环比较，已改成为判红条件） | PG 档 18/18 + 对账门无警告绿 |
| F-7 | 修 **`allocate()` 把任何 IntegrityError 都当成并发争用**：外键落地后"workspace 行不存在"被误报成"没有空闲卡"；改按 SQLSTATE / 约束文案分类 | 变异对照：退回盲重试即红（实测读数见 ADR 0005） |
| G-1 | 把两条"写在文档里的约定"变成常驻对账：①模型声明 ↔ 迁移产物（`compare_metadata`，非空即红）；②`Settings` 字段 ↔ `.env.example`（双向：漏文档 / 留死键都红） | `test_migrations.py` 对账用例 + `test_config_docs.py` 2 例（改前红：`billing_hold_ttl_minutes` 未进 .env.example） |
| D-1 | 决策记录：ADR 0004（hold 为何是独立表而非账本条目）、0005（SQLite/PG 语义差与两层验证）、0006（冲突即 409）；API.md 补 409/402 口径 | `docs/adr/000{4,5,6}-*.md` |

## 4. 分项状态

### VERIFIED PASS
全套用例全绿（唯一 skip 是 `k8s_integration` GPU 档）。四元组计数只在 §1 出现一处，
由 `make validate` 的值对账钉住——**本节刻意不再复读绝对数字**，多抄一份就多一处会静过期、
且门禁看不见的位置。
其中真后端档：PG 真并发 18/18、真容器 20/20、真浏览器 11/11、
对象存储真后端 20/20、K8s 控制面真集群 7/7、SDK 线格式 6、预授权 14、
**边缘设备真进程 e2e（真 uvicorn 子进程 + 真 agent 子进程 + mock 驱动）**。
lint/type/migration/build/smoke/release/供应链全链路。

### PHYSICAL_VALIDATION_PENDING / NOT_RUN（不假装 PASS）
GPU 真机（G1–G4 脚本就绪，本机无 NVIDIA 设备）· K8s 上 `nvidia.com/gpu` 的真实分配
（需节点带 Device Plugin；控制面路径本身已由 G0.26 覆盖）·
Streaming 媒体面（Isaac Sim WebRTC）· Robot 真机 · Warm pool SLA。

### BLOCKED_EXTERNAL_DEPENDENCY
NGC 凭据（**构建并推送 workspace 镜像**后回填 `TemplateVersion.image_digest`）·
云 S3 真实账号凭据（协议语义已由本地真服务端覆盖，缺的只是"云厂商那份实现"）·
物理机器人 · 带 GPU 的 K8s 集群凭据 · 能出到 Docker Hub 权威 registry 的网络位置
（`python:3.12-slim` 的 digest 因此仍未钉，按例外登记，见 SUPPLY_CHAIN §2）。
> 已解除：docker daemon、postgres 镜像（v0.5.0）、S3 兼容服务端与真 K8s 集群（v0.6.0，
> 本机自起自删）、**`nvcr.io` 基础镜像钉 digest**（v0.7.0：这条曾被登记为"阻塞于 NGC 凭据"，
> 实测不成立——manifest 与 digest 用匿名 pull 令牌即可解析，凭据只在拉层字节时才要）。

### TECH DEBT（已知、有意延后 —— 本轮逐条量过，不是照抄旧措辞）

v0.6.0 记在此处的"edge agent 独立包与其分派/取件鉴权面"**本轮结案**：组件存在
（`edge_agent/` 包 + CLI + 三条设备侧端点），鉴权面被常驻用例覆盖（发现/开门/取件、
越权 404、前缀复核、真进程 e2e），ADR 0007 转 Accepted。剩下的"真机驱动"不是软件任务：
`edge_agent/drivers.py:build_driver` 是唯一替换点，等的是真实机器人（G5.2）。

v0.5.0 记在此处的三条（K8s provider 控制面路径、Docker `--gpus` 分支、
S3 `ArtifactStore` 真实后端）**本轮全部结案**：前一条由 kind 真集群档 7/7 覆盖
（ADR 0009），后两条由真守护进程记账（docker 档 20 例，其中 3 例是 GPU argv）与
真 S3 服务端（20 例）覆盖（ADR 0008、G0.19/G0.25）。
剩下的只有"物理设备才答得了"的部分（GPU 设备在容器内可见、device plugin 真实分配），
已移到上面的 PHYSICAL/BLOCKED 两节，不再冒充"待办"。

当前真正的延后项只剩下面一条，且它被本轮实测改过性质：

- **`default_idle_timeout_minutes` 是一个"设了也不生效"的预留开关**（本轮按 AST 逐字段数过：
  `Settings` 共 34 个字段，恰好 1 个在 `app/`（除去声明所在的 `app/config.py`）零读取，就是它。
  所以它不是"已实现待调参"，而是**尚未实现的预留开关**。
  本轮把这句话从文档挪进了门禁（`tests/test_config_docs.py`）：
  ①"无人读取的字段集合"必须**恰好等于**惰性登记表（漏登记＝有人会被"设了就生效"误导，
  死登记＝文档在撒谎，两个方向都红）；②登记表里的每个字段，其在 `.env.example` 中紧邻上方
  的注释块必须带"未启用"标记（"预留"两个字不算，因为误导运维的是"设了会生效"这句隐含话）；
  ③探针本身带非恒真对照（已知有读取者的 `ide_port_start` 必须读到非空）。
  开火读数（全在 /tmp 副本上做，主树不动）：**CFG1** 往 `app/deps.py` 加一行真读取 →
  未读集合变空、该登记项被判"死登记"；**CFG2** 把 `.env.example` 的"未启用"改成"预留" →
  标记判据点名该字段；control（干净副本）两把都不开火。
  **为什么仍然不实现**：要先回答"用什么算活动"——容器 CPU 在 GPU 训练下会长时间接近 0
  （CPU 空闲 ≠ 任务空闲），据此自动停机等于误杀长跑任务并照秒扣费；可信信号来自
  真机 GPU 利用率（被 NVIDIA 设备阻塞，见上方 BLOCKED）。→ 保持延后，
  但延后现在是**被机器看着的延后**：谁接了消费者而不改文档，常驻档立刻红。

**引用完整性已逐条裁决完（见 ADR 0005 追加节）**：`*_id` 列普查出的 19 处未声明外键，
以"全仓删除能力普查"（唯一硬删是 `GpuAllocation`，且无人按 id 引用它）为依据分派——
11 处补上约束（`b7e4c1a09f52`），8 处保留不声明并逐条写明理由（互指环 2、slug 形态 3、
账本历史 2、多态主体 1）。这里不再有待办，只有已记录的设计立场。

（已闭：`test-*.db` 模块级文件库改 pid 独占，见 §3 的 T-1。闭之前实测过一次代价——两个
pytest 进程并发跑同一仓库，互相清库，读出 31 / 26 例假红。）

## 5. 结论

v0.5.0 把"记为无法在本环境验证"的四类判据变成常驻门禁；v0.6.0 把剩下的
**"从未真正执行过的后端"**逐个跑起来：S3 分支（boto3 都不在依赖里）、
K8s 控制面三方法（被"要 GPU"这个不相干前置拖着）、Docker 的 GPU 参数
（只在测试里重抄过命令行）。三处都抓出了假测试看不见的缺陷：HEAD 无 body 导致
"桶没了"被读成"产物没了"、故障被写进不可重验的 FAILED 终态、
strategic merge patch 并不能把 `nvidia.com/gpu` 从模板里去掉。

方法论上的收获已经固化成门禁而不是叙述：**能自起后端就别用 fake**（fake 只能证明
自己和自己一致）、**能把判据交给被测系统自己回答就别信测试的转述**
（守护进程的 HostConfig、集群的 Unschedulable 判词、第二台服务端的错误码）、
**每条新保证都要有一支"退回旧写法必然开火"的变异对照**（M1b/M2/M3/M4/M6 全部实测）。

仍然待办且性质明确：`default_idle_timeout_minutes` 缺可信活动信号（`app/` 内 0 读取，本轮起
这句话由常驻判据把守——接上消费者却不改文档会立刻红，见 §4 TECH DEBT 与 N-12）。
上一轮挂在延后清单里的 edge agent 组件与分派/取件鉴权面，本轮已落地并进门禁（ADR 0007 Accepted）；
引用完整性的 19 处 `*_id` 已全部逐条裁决（11 处补约束、8 处写明理由）。
硬件与凭据类项目继续显式登记 PENDING/BLOCKED，不用测试通过冒充物理验证。

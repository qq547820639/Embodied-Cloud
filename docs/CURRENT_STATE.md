# CURRENT_STATE — EmbodiedCloud

> 版本：**0.7.0 边缘设备真进程通路（发现 / 开门 / 取件 / 遥测回读）**（2026-09-26）。
> 数据来源：`docs/VALIDATION.json`（`make validate` 自动生成，JUnit 稳定计数）。

## 1. 本次真实验证（实测，非复制旧文档）

| Gate | 结果 |
|---|---|
| Test | **PASS（collected 538 / passed 537 / skipped 1 / failed 0）**（本串与 `docs/VALIDATION.json` 由常驻判据对账） |
| Lint / Type | PASS（ruff 0 / mypy 45 files：`app` + 本轮入册的 `edge_agent`） |
| Migration | PASS（clean DB empty→head **14 文件链** + schema 落地 + downgrade 循环 + **模型↔迁移对账**） |
| Integration PostgreSQL | **PASS 21/21**（自建一次性容器，真行锁语义；含本轮的锁等待窗口与持锁时长实测） |
| Integration Docker | **PASS 25/25**（真容器；`--gpus` 参数的守护进程侧记账 3 例自 v0.6.0 起在册；本轮 +3 例＝镜像层清单的接线与分流判据） |
| Integration Browser | **PASS 11/11**（Playwright 驱动系统 Chrome 真 DOM） |
| Integration Object store | **PASS 20/20**（一次性 VersityGW 容器 + 真实 boto3；MinIO 交叉核对读数一致） |
| Integration K8s control plane | **PASS 7/7**（kind 真集群：真 kubelet/调度器/endpoints，无需 GPU） |
| Edge agent 真进程通路 | **PASS**（真 uvicorn 子进程 + 真 `python -m edge_agent` 子进程 + mock 驱动：一轮到 VERIFIED、落盘摘要核对、第二轮不重复上机；服务端另有 10 例鉴权/防线 + 设备侧 12 例坏响应形状） |
| Integration K8s（GPU 全流程） | PENDING（原因登记：需要节点带 `nvidia.com/gpu` 容量 = Device Plugin；控制面路径已由上一行覆盖。**本轮把这条原因查到根**：假 device plugin 既没检索到成熟实现、也不是绑定约束——Pod 镜像取自 `app/services/providers/k8s.py:190` 的 `settings.workspace_image`，那是 amd64 + NGC 基座、本机没构建也没推送的镜像，容量造假只会停在 ImagePullBackOff。源头逐条见 ACCEPTANCE_GATES 附注） |
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
| N-15 | **两条"前提竞态"各由一次真实红抓出并修掉，判据的超时一分没加**：①真集群档负向对照在 `wait_ready(...) is False` 之后直接 `_pods(...)[0]` —— Pod 及其 conditions 由集群控制器写，provider 只给 6s 窗口（主树全量复算 517 passed / 1 failed，红的就是取 Pod 那行的 `IndexError`，其余 6 条真集群用例同轮全绿 ⇒ 既非 kind 引导失败也非产品缺陷）；②换树换环境复算又红一条 docker 档：`exit 0` 的"已启动→已退出"只差几毫秒，工作区又无 ide_port/healthcheck，`wait_ready` 首次 inspect 抓到 running 便返回 True（519 passed / 1 failed，`assert True is False`，同一内容主树刚绿过；这个 True 本身是产品正确行为，另有用例钉着） | 两处同法：判据行留在最前，前提改走 `tests/k8s_server.poll_until/await_pod/await_condition_reason` 与新加的 `wait_exited`（超时抛"前提未达成 + 最后一次读数"，不返回 False 冒充结论）；工具自身各有开火对照（假 list 函数三种边界 / 不起 exit 的容器 2s 内必须红）。复跑：真集群档 7/7（254s）、docker 档 21/21（57.6s），全套 519 → 522 |
| N-16 | **`allocate()` 的重试预算被量过，"等不到"与"没卡"分成两句话**：参数（5 次 × 0.05s 退避）此前无从解释；实测预算 0.500s、真实放弃发生在 0.816s / 0.821s，16 线程抢 4 卡 ×3 轮里单个分配事务 median 30.5→43.1ms、max 35.5→70.8ms 且每轮赢家 4/4 ⇒ 参数保持不动，"改成 deadline 式长等待"被同一批读数否掉（只是把假空换成更慢的首包）。真正的缺陷是可确定性复现的谎话：唯一候选被别的会话 `FOR UPDATE` 持住时，`still_waiting` 每轮都数得到 1 张，却仍抛 `No GPU available` | 新增 `GpuPoolContendedError`（含等待卡数与预算）+ 窗口/持锁两支实测常驻用例 + `truly_empty` 反面档；`test_unbounded_candidate_read_starves_concurrent_allocate` 的断言就地反转（从钉住谎话改为钉住"必须报 contention 且不得出现容量那句"）；变异 CONT1 短路 contention 分支 → 两支红、反面那支照旧绿。pg 档 18 → 21，全套 522 → 525 |
| N-17 | **workspace 在「还要重试」的那一刻被宣布死亡**：`make validate` 连两轮同一条红（`tests.test_workspace_credential::test_access_endpoint_returns_plaintext_password`，`assert 'failed' == 'running'`）。归因靠仓库外临时诊断插件打出的两份现场读数：失败瞬间 8 张卡里 6 张 ALLOCATED、victim 自己没卡；而**收尾**读数里同一个 workspace 已经 `running`、它的 `provision` op 是 `succeeded(2)` —— 那句「这个任务失败了」是第 1 次尝试替第 2 次尝试下的结论。放大器另有一处：`tests/test_gpu_pool_guard.py` 的 `rig` 留 8 行 CREATED workspace，app 每次启动都跑 `reconcile_all()`（「QUEUED/CREATED 且无 active op ⇒ 重新入队 PROVISION」），于是下一个模块的 worker 先替残骸抢卡（本文件 + `test_api` 配对即红，日志 4 条 `provision(ws-guard) failed, retrying`） | ①重试判据收成一个函数 `OperationWorker.will_retry(op)`，`finish_failure` 与 `orchestrator._fail(terminal=...)` 同读它（两侧各写一遍 `attempts >= MAX_ATTEMPTS` 时，任何一侧改动都会让「op 在重试」和「workspace 已 FAILED」同时成立）；非终态失败写 QUEUED + 保留 `error_message` + 归还卡。`workspace_operations` 在 API 层零读者（`grep -rn WorkspaceOperation app/routers/` = 0），所以 status 是「还在重试」的唯一出口；②`tests/settle.py::await_workspace_settled`：终态集合 + 前提预算 30s（旧轮询 2s < backoff 1+2s）+ 失败回显池子现场，4 处 `{running, failed}` 轮询全部接上；③守卫收尾归还自己借的卡并删自己的行；④`test_api` 补上它一直缺的前置声明。反证两支：CONT2 把判据短路成「永远终态」→ 新增两支红、其余 10 支照旧绿；settle 助手带正反两支（永远 queued 必须红且报「前提未达成」+ 池子读数；running/failed 都不红，且首读数未收敛 ⇒ 它真在轮询）。复跑：全套两连绿 528 passed / 1 skipped（宿主 load 15.7 与 28.4），全套 525 → 529 |
| N-18 | **上一轮那条"取不到权威 digest"的登记，错在通道清单没列全**：`python:3.12-slim` 的例外理由写的是"本机三条路径均不可达"，而那三条全是 **CLI/curl 那条传输**（`auth.docker.io` / `hub.docker.com` / `registry-1.docker.io`）；**守护进程自己的出网路径从没被试过**。这与 N-11 拆掉的"Isaac Sim 钉 digest 阻塞于 NGC 凭据"是同一类错：把"我试过的那条通道不通"记成"这件事做不了" | 权威读数取自 docker.io 本身：`docker pull --platform linux/amd64 python:3.12-slim` 打印的 `Digest` ＝ `sha256:f77ac9e4…`，与上一轮独立从 `public.ecr.aws` 读到的索引 digest 逐字同值；再按该 digest 直拉一次成功。钉进 `runtime/Dockerfile.control-plane`（多架构索引，tag 仅留可读性）并从例外登记表删除。**登记表清空会让 `unpinned == registered` 与恒真同形**，所以双向对账抽成纯函数 `_exception_table_offenders`，配常驻注入夹具（漏登记、死登记各开一次火；两侧皆空与两侧相等都不开火），作用域判据同时改为"真实树必须零个未钉外部镜像"。构建侧实跑：`make control-image` 的 `Step 1/10` 用的正是这条引用，`Successfully built`，产物容器内 `python -V` ＝ `Python 3.12.14`（本次构建产物随后 `docker rmi` 删除）。supply-chain 档 10 → 12，全套 529 → 531 |
| N-19 | **两条挂了很多轮的"外部阻塞"里，只有一条是真的**：`integration_k8s` 那句"需要 Device Plugin"被查到根——按技术选型规矩先做候选调研（`NVIDIA/k8s-device-plugin` README：无 fake 模式，`FAIL_ON_INIT_ERROR` 只是"没 GPU 的节点上不崩"；HAMi README：前置条件仍写 `NVIDIA driver >= 440`；kubernetes.io device-plugins 页正文被截断，`#examples` 没读到，故只说"可见部分没提"；GitHub 仓库检索 0 命中按工具盲区记账，不当结论），再读被测代码定位绑定约束：Pod 镜像取自 `app/services/providers/k8s.py:190` 的 `settings.workspace_image`（amd64 + NGC 基座，本机没构建也没推送），**假容量只会让 Pod 停在 ImagePullBackOff**，这一格真正的门与 G1–G4 是同一道 | ①把这条调研连同"为什么不做假插件"写进 `docs/ACCEPTANCE_GATES.md` 末尾附注与 §1 的 PENDING 行，让下一读者不必重跑；②顺手把上一轮的一次性构建实测提成常驻门禁：`tests/test_docker_provider_integration.py::test_pinned_base_of_the_control_plane_recipe_is_fetchable` 用守护进程那条传输真的 pull 配方里钉死的引用（pull 成功 + `inspect` 出 arm64/linux 与非空 Id 才算过），因为在此之前**没有任何常驻门禁构建过控制面镜像**（`make validate` 的 build 检查量的是 wheel）。负向对照（把 digest 首位翻转后必须取不到）本轮实测开火：`failed to resolve reference "docker.io/library/python@sha256:077ac9e4…"`，但它的代价是实测 91.8s（正向 pull 37.7s，本机 daemon 到 registry 一趟就是几十秒），所以默认档只出读数、用 `EMBODIEDCLOUD_RECIPE_BASE_CONTROL=1` 打开——要证的事不随每轮代码变化。同轮附带读数：`python:3.12-slim` tag 今天的 `RepoDigests[0]` 与钉住的那份**相等**（尚无漂移）。docker 档 21 → 22（默认档 21.99s），全套 531 → 532 |
| N-20 | **镜像层清单这一格今天到底缺什么**：SUPPLY_CHAIN §8 第 3 项挂了几轮，措辞把两件事捆在一起（"需真实构建后由 trivy/syft 生成"）。本轮分开量：**工具侧今天能落地**，而且选型是被通道读数改掉的——功能更对口的 syft 三条通道全取不到字节（daemon 经镜像站取 docker.io 时 TLS 握手超时、`ghcr.io` 拨号超时、GitHub release 下载在宿主与容器内两处都不通），trivy 在自己的 `docs/getting-started/installation.md:12-16` 列了三个官方注册表，其中 `public.ecr.aws` 当场可拉，同文件第 22 行明文支持"挂容器引擎 socket 扫镜像"。**构建侧今晚极不稳**：同一配方连跑 5 次全败在 `Step 7/10 : RUN pip install`（153s／102s／224s／156s／50s），第 6 次 165s 才成，并排探针把成因定位到容器侧→`files.pythonhosted.org` 的 TLS 超时（同一时刻容器内取 `pypi.org/simple/` 是 200／535 KB／1.3s，宿主 `curl` 同一条 CDN URL 拿得到 302，上一轮同一配方 160s 成功过）——卡这一格的是**通道**，不是**机制**（产物最终仍落盘，见下面的真读数） | 新增 `scripts/image_sbom.sh` + `make image-sbom`（工具镜像钉 `public.ecr.aws/aquasecurity/trivy:0.74.0@sha256:62b1e65e…`；**钉的方向单独核过**：ECR Public 匿名令牌取 index、逐字节重算 sha256 得同一个值（3772 B，`application/vnd.oci.image.index.v1+json`，子清单 amd64 `ee940acb…`／arm64 `55ad20f8…`），确认钉的是多架构索引而不是本机 arm64 那一份；被审镜像不在场退 2 而不是交空产物；结果走 stdout 再 `.tmp`→`mv`）＋ `scripts/check_image_sbom.py`（产物层判据：`type=container` 且 purl 带 `@sha256:`、≥1 `pkg:deb/`、≥1 `pkg:pypi/`、`bomFormat=CycloneDX`、`components` 非空、不混漏洞结论、"文件不存在必须红"；`--self-test` 14 档全开火（**逐档核对精确条数**，防某条判据被邻居顺手救活而从未单独运行过），`mypy` 零错）。三层常驻把关共用同一份实现：配方层（`test_image_sbom_step_exists_and_is_pinned` ＋ 双向注入 `test_tool_image_criterion_fires_in_both_directions`）、产物层（`test_image_sbom_validator_fires_per_clause`）、接线层（docker 档 `test_pinned_sbom_tool_actually_produces_a_checkable_image_sbom`）。真读数两笔：docker 档接线用例对配方里那份钉死的基础镜像产出 `components=89 / deb=87 / pypi=1 / spec=1.7`，其自报摘要与 §2 钉进 `Dockerfile.control-plane` 的 `f77ac9e4…` 同值；`make image-sbom` 对本轮真构建的控制面镜像产出 `components=137 / deb=87 / pypi=49`，并绑 `docker image inspect` 的 `.Id`＝`sha256:cd371b31…`（三处同值：Id＝trivy 自报 ImageID＝purl 摘要）。**加固一轮**（独立评审交回 10 条，逐条重开原行后落地 7 条）：配方层原判据只认 `*_IMAGE="${VAR:-…}"` 一种赋值形状——把变量改名或直接在 `docker run` 行写 `aquasec/trivy:latest` 都能绕过而判据照绿，现改为看**会被拉起来的那些引用**（折续行、去整行注释、认 Docker Hub 两段名，路径与挂载点不算），四种绕过形状各有注入对照；产物层加 `--image-id` 一致性条款（在此之前"扫错对象也能过"是真的：接线用例自己拿基础镜像跑，两层条款它同样满足）；第二通道与 daemon 读数的 2×3 组合全档常驻，坏摘要在任何传输形状下都必须红（上一版按关键字先跳过＝让"钉错 digest 恰好被传输问题掩盖"免检）；另补空 stderr 的 `splitlines()[-1]` 越界与失败时 `.tmp` 残留的 trap。**撤回自己本轮写下的一条错误归因**：第一次运行用 `-v /tmp/trivyout:/out --output …` 拿到 rc=0 而宿主目录为空，我记成"trivy 会 rc=0 却不落盘"——实为这台机器（colima）的 `/tmp` 不是共享进虚拟机的挂载点：同分钟内容器写 `/tmp` 挂载点宿主不可见、改写工程目录下的 `dist/mnttest` 立刻可见（露馅的是同一只空挂载的另一症状：`-v /tmp/probe.py:/probe.py` 报 "can't find `__main__` module"）。判据仍按产物内容写，但成因改记在夹具上。连带更正 `docs/ACCEPTANCE.md:19` 与 `docs/IMPLEMENTATION_PLAN.md:40` 的"本机没有 Docker daemon"（被 `scripts/release.sh:215` 与本轮 `docker version` → Server 29.5.2 双重反驳）；§3 那条"code-server 上游不发校验文件"补了第二条独立通道（release API 逐枚枚举 v4.130.0 的 9 个资产）。**复核后不改的一条**：子代理报 `tests/test_k8s_integration.py:46` 裸调 `load_kube_config()` 会忽略 `KUBECONFIG`——SDK 默认位置本来就吃该环境变量，本仓 `tests/test_k8s_control_plane.py:82` 上一轮已记过。supply-chain 档 12 → 15，docker 档 22 → 25，全套 532 → 538 |
| N-21 | **加固一轮后剩下的开放项只有一条**：Debian 层那 87 个 `pkg:deb` 组件今天没有任何东西在比 CVE（`make audit` 走 uv，只覆盖 Python 侧）。原先并列的那条——控制面镜像那份 SBOM 产物未落盘——本轮已闭：`make image-sbom` 真跑出 `components=137 / deb=87 / pypi=49` 并绑到 `.Id` | OS 层漏洞扫描见 SUPPLY_CHAIN §8 第 5 项：同一份钉死的 trivy 加 `--scanners vuln` 就能做，代价是它运行时要从自己的分发点下载漏洞库＝又一条出网依赖＋一份非确定性读数，所以单独立项而不顺手并进本轮；做之前先量库里有没有当天数据，别把「拉不到库」洗成「没有漏洞」。 |


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
| S-1 | **供应链可复现**：universal `uv.lock`（多平台 marker + sha256）、SBOM（`uv export --format cyclonedx1.5`）、`uv audit --locked`；CI 增 `make verify-lock` / `make sbom && make audit`，并要求"需要 docker 的档位必须真的 PASS，否则红"。本轮补上**镜像层**那一半：`make image-sbom` 用钉死 digest 的 trivy 对已构建镜像出 CycloneDX，产出必须过形状判据（被审对象带 `@sha256:` 且摘要等于 inspect 的 `.Id`、`pkg:deb` 与 `pkg:pypi` 两层都在、不混漏洞结论） | `docs/SUPPLY_CHAIN.md` §4/§5、`docs/ACCEPTANCE_GATES.md` G0.35、`scripts/check_image_sbom.py --self-test`（14 档注入，逐档核对精确条数） |
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
其中真后端档：PG 真并发 21/21、真容器 25/25、真浏览器 11/11、
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
物理机器人 · 带 GPU 的 K8s 集群凭据。
> 已解除：docker daemon、postgres 镜像（v0.5.0）、S3 兼容服务端与真 K8s 集群（v0.6.0，
> 本机自起自删）、**`nvcr.io` 基础镜像钉 digest**（v0.7.0：这条曾被登记为"阻塞于 NGC 凭据"，
> 实测不成立——manifest 与 digest 用匿名 pull 令牌即可解析，凭据只在拉层字节时才要）、
> **Docker Hub 权威 digest**（v0.7.0：登记理由写的是"本机三条路径均不可达"，而那三条全是
> CLI/curl 那条传输；守护进程自己的出网路径从没试过，一试就通 ⇒ `python:3.12-slim` 已钉索引
> digest、例外登记表清空）。两条同为"把我试过的某条通道不通记成这件事做不了"——
> 写"取不到"之前必须先把通道列全（CLI 直连／守护进程／构建器／另一台机器）并逐条记怎么试的。

### TECH DEBT（已知、有意延后 —— 本轮逐条量过，不是照抄旧措辞）

v0.6.0 记在此处的"edge agent 独立包与其分派/取件鉴权面"**本轮结案**：组件存在
（`edge_agent/` 包 + CLI + 三条设备侧端点），鉴权面被常驻用例覆盖（发现/开门/取件、
越权 404、前缀复核、真进程 e2e），ADR 0007 转 Accepted。剩下的"真机驱动"不是软件任务：
`edge_agent/drivers.py:build_driver` 是唯一替换点，等的是真实机器人（G5.2）。

v0.5.0 记在此处的三条（K8s provider 控制面路径、Docker `--gpus` 分支、
S3 `ArtifactStore` 真实后端）**本轮全部结案**：前一条由 kind 真集群档 7/7 覆盖
（ADR 0009），后两条由真守护进程记账（docker 档 21 例，其中 3 例是 GPU argv）与
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

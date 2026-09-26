# Changelog

## 0.7.0 — 2026-09-26（Sim2Real 从"控制面替设备走状态机"变成真设备通路）

`docs/VALIDATION.json`（`make validate` 生成）：collected 545 / passed 544 / skipped 1 / failed 0（唯一 skip 是 k8s_integration，需 NVIDIA Device Plugin）。
overall = `PASS_WITH_PHYSICAL_PENDING`（物理待验仍是 GPU 真机 / Isaac 流媒体面 / 真机器人）。

### 边缘设备通路（§25，ADR 0007 从 Proposed 转 Accepted 并实施）
- **裁决依据是查来的，不是拍的**：读 AWS IoT Jobs 的任务生命周期页
  （`iot-jobs-lifecycle.html`）拿到两事实——`QUEUED` 由服务端 rollout、
  `IN_PROGRESS/SUCCEEDED/FAILED` 一律"Initiated by device"，且取任务用的是
  `StartNextPendingJobExecution` 这个独立 API 而不是把任务塞进通知通道。
  据此定：分派走独立 `GET .../deployments/assigned`（heartbeat 保持"只登记存活"），
  设备自己发 `POST .../begin` 开门，控制面继续持有 `run/complete`
  （那两步要绑 GPU 工作区、要结算账本，属主不是机器人）。
- 服务端新增：`GET /api/edge/agents/{id}/deployments/assigned`、
  `POST /api/edge/agents/{id}/deployments/{dep}/begin`（条件 UPDATE，重复调用幂等）、
  `GET /api/deployments/{dep}/artifact`（`X-Agent-Token`，带 `X-Artifact-Sha256` /
  `X-Artifact-Size` / ETag），`POST /api/deployments` 可选 `edge_agent_id` 当场指派。
  `begin` 不是装饰：`report_checksum` 只接受 `downloading`（§23 防绕过），
  没有设备侧开门就还得让控制面替它写状态。
- 修 **遥测只写不读**：`report_telemetry` 从 v0.4 起一直在写 `telemetry_events`，
  全仓没有任何读路径。新增 `GET /api/edge/agents/{id}/telemetry`（租户 scope、越权 404），
  设备的 `edge-run` 结果才谈得上被运维看见。
- 新增设备侧包 `edge_agent/`（只依赖标准库、不 import `app`：设备上装的是本包）：
  `client.py` 流式取件 + 边写边算 sha256 + 体积熔断 + `.part` 原子改名，
  `drivers.py` mock 驱动（真驱动接入点 `build_driver`），`agent.py` 一轮编排，
  `__main__.py` CLI。安全细节：base_url 限定 http(s)（否则取件客户端就是任意文件读取器）、
  凭据只走环境变量（argv 上的 token 同机任何用户能从 `ps` 读到）、
  不下发 `Content-Disposition`（响应头里不带用户可控文本）、
  `AgentClientError` 只带状态码/URL/detail，token 不入异常文本（T2/T5）。
- 防线读数（每条都是"拆掉它，看哪支用例翻红"）：
  **M1** 去掉 `read_artifact` 的 workspace 前缀复核 → 攻击者拿到 200 + victim 的字节；
  **M2** 去掉取件的 `downloading` 前提 → 未 begin 也能取件（200 而非 409）；
  **M3** 去掉部署期的 `edge_agent` 绑定 → 发现面变空，e2e 与 API 档同时红。
  如实登记一处**没有**独立开火对照的冗余：`begin` 的条件 UPDATE 里
  `edge_agent_id` 那一支与路由层校验语义重叠，单线程观测不到差别（要它可观测需 PG 档并发用例）。
- 常驻验证：`tests/test_edge_agent_api.py`（10 例，含跨租户 404、同租户未绑定 404、
  越权取件、503 可重试、begin 幂等/拒终态）、`tests/test_edge_agent_client.py`
  （12 例，坏响应形状：摘要不符 / 声明体积超限 / 中途超限 / 非 http base_url /
  token 不外泄）、`tests/test_edge_agent_e2e.py`（真 uvicorn 子进程 +
  真 `python -m edge_agent` 子进程 + mock 驱动，断言落盘 sha256 与登记一致、
  无 `.part` 残留、遥测读得到、**第二轮不重复上机**）。
  已知限制如实写进 ADR：没有运行游标，崩在 verified 之后、驱动之前不会自动补跑。

### 测试夹具：把"靠调度器运气"和"靠排队位置"两类隐性前提拿掉
- `wait_status` 的常驻动机：`test_gpu_admin` 在全量跑里红过一次，报错是
  "20.0s 内未到 running"。旧形状是 sleep + 读 HTTP，等于把后台 worker 线程
  拿不拿得到 CPU 当前提。现在每轮先 `worker.tick_once()` 自己推进
  （claim 是 CAS + fencing，胜者唯一），并配**确定性的两档对照**
  （`tests/test_workspace_progress.py`）：把后台线程循环体掐掉之后，
  主动 tick 的到得了 running 且真绑上 GPU，被动等的到不了——后者是前提档，
  它若读到 running 就说明对照失效，正例读数一律不作数。
- 修 **共享测试库的 GPU 池饿死**：全套共用一个 SQLite、mock 只 seed 8 张卡，
  `POST /api/workspaces` 占卡而用例不还。实测把本轮新加的 `test_edge_agent_api.py`
  （13 个 workspace）与 `test_gpu_admin.py` 配对即红，其余 55 个文件逐个配对都不红
  ——单变量定位到污染源。修法是两头：新文件模块级归还自己占的卡；
  需要空闲卡的用例显式达成前置条件（`tests/gpu_pool.py:ensure_free_gpus`，
  回收走 `GpuScheduler.release` 这条唯一分配权威，不手写 UPDATE）。
- 真起 uvicorn 的夹具从浏览器档抽成 `tests/live_server.py`，浏览器档与 agent e2e
  共用同一份就绪判据与回收顺序（迁移后浏览器档 11/11 重跑为绿，用时 24.0s ≈ 原 22-24s）。

### 发布链：让"报告只说 failed: 1"这种形状不可能再出现
- `docs/VALIDATION.json` 的 `test_run` 现在带 `failed_names`（名字取自 JUnit 的
  `classname::name`，含 `failure` 与 `error` 两类），`docs/VALIDATION.md` 同步行内展示。
  起因是本轮真实撞到的排查死角：`validate` 把 pytest 输出丢弃（`code, _ = run(...)`），
  一次偶发失败之后**连用例名都拿不到**，重跑两次都不再红，只能挂一条"未归因"。
  只带名字不带 message 是有意的：报告必须确定性（CI freshness 门禁比较 `git diff`），
  而失败消息里带时间/端口就每次不同。常驻对照判据：造一份"4 条里 2 条红"的 JUnit，
  必须恰好点出那两条；全绿报告必须给出空列表（否则这条判据只是"字段存在"）。
- 新增常驻判据：`make lint`/`make typecheck` 的 ruff/mypy 目标集合必须与
  `scripts/validate_release.py` 里的一致，并钉住 `edge_agent` 在册。
  开火读数：把 `edge_agent` 从门禁那侧删掉即红
  （`ruff: make=['app','edge_agent','tests'] gate=['app','tests']`）。
  本轮新增包时要同时改两处，漏一处的后果是"新代码恰好是没人量的那份"。
- 文档同步：`docs/API.md` 设备侧三条 + 遥测回读 + 取件 409/404/503 口径；
  `docs/OPERATIONS.md` 新增 §8 边缘设备（入网/常驻/凭据放 env 而非 argv/排查表）
  与两条读数坑（"PENDING ≠ 跑过"、共享库的固定卡池）；
  `docs/ACCEPTANCE_GATES.md` 新增 G0.28/G0.29，G5.1 的判据从"控制台页走通"
  升级为真进程回环；`docs/openapi.json` 重新生成（+206 行）。

### 供应链：外部基础镜像钉 digest（顺带揭掉一条写错的阻塞理由）
- **这条待办的前提是错的**。`docs/SUPPLY_CHAIN.md` §8 原文写"Isaac Sim 基础镜像钉 digest：
  有 NGC 凭据后改 `@sha256:`"——实测**不需要凭据**：向 `nvcr.io/proxy_auth` 换一枚匿名 pull
  令牌（`scope=repository:nvidia/isaac-sim:pull`）就能读 manifest。两条独立读数吻合：
  `HEAD /v2/nvidia/isaac-sim/manifests/6.0.1` 的 `docker-content-digest` =
  `sha256:783444c7…30aa9`，而 `GET` 回来的 743 B manifest list 重算 sha256 得同一个值。
  凭据只在**拉层字节**时才要——所以"钉 digest"这件事从来不在阻塞清单里，被阻塞的是构建与推送。
- 三个选型判断（都是量出来或读出来的，不是按习惯挑的）：
  **钉多架构索引而不是单个平台清单**（子清单 amd64 `b1c542b2…`／arm64 `20269735…`）——
  钉平台清单等于把配方锁死在构建机的架构上；**保留 tag 与 digest 并写**
  （`name:tag@digest`）而不是只留 digest——可读性不付代价，因为本机 `docker build` 实测
  接受该形式并进入解析、按 digest 开始拉层（权威侧只有这一条一手证据：
  docs.docker.com 的 Dockerfile 参考页本机抓取失败，故不引其措辞）；
  **`python:3.12-slim` 不钉**（见下条）。
- `python:3.12-slim` 按**例外登记**而非钉死，理由是可获得的读数都不权威：Docker Hub 的
  `auth.docker.io` 与 `hub.docker.com` 本机实测均 `curl 28` 超时；唯一能读到的
  `public.ecr.aws/docker/library/python`（Docker 官方镜像的第三方镜像站）给
  `sha256:f77ac9e4…`（body 重算 sha256 一致，OCI index，16 个子清单），但它 amd64/arm64
  子清单的 config digest（`9e87977b…`／`8630ab77…`）**都不等于**本机缓存那份
  `python:3.12-slim` 的 config（`2f17fc04…`）。两来源互不印证 ⇒ 今天的权威 digest 未证实；
  钉一个未证实的 digest 只会让构建直接失败。顺带这条不吻合本身就是"tag 会移动"的实证。
- 新增三条常驻判据（`tests/test_supply_chain.py` 从 4 例扩到 10 例）：
  ①非 `embodiedcloud/` 命名空间的 `FROM` 必须带 `@sha256:`；未钉者必须与例外登记表
  **双向**对账——多登记（其实已经钉上）与漏登记（新引入裸 tag）都判红，例外条目必须带
  固定词表里的证据等级 + ≥40 字理由，并与 `docs/SUPPLY_CHAIN.md` 逐字互核（钉上的 digest
  也要在文档里逐字出现，文档只写 tag 就等于把移动的东西宣称成钉死的）；
  ②**消费侧**（`gpu_acceptance.sh`／`isaac_sim_smoke.sh`／`release.sh`／`docs/GPU_HOST.md`）引用
  同一基础镜像时必须与 Dockerfile 钉死的那份**逐字相等**；
  ③三条判据的解析作用域均须非空，②另按"必须覆盖到哪些文件"断言（子集检查，
  不按命中数——数量会随新增消费侧自己涨，而有人改名/删引用时子集会立刻缺）。
- 真实内容上的变异电池（把 `runtime/`+`scripts/`+`docs/GPU_HOST.md` 复制到 /tmp 逐条拆，主树不动）。
  这组编号用 **SC**（supply chain）而不是接着往下排 `M5/M6…`——`M5`/`M6` 在本仓已被
  `tests/test_gpu_pool_guard.py` 的 reclaim 对照和 §5 里 v0.6.0 那批读数各自用过一遍，
  同号不同事会让读数无法回溯：
  **SC1** 摘掉权威侧 digest → 未钉集合多出 `nvcr.io/nvidia/isaac-sim:6.0.1`、与例外表差集
  非空即红（此时②**不**开火：权威侧已无可抄的钉，两把判据互补而非冗余，这一条如实记下）；
  **SC2** 只把 `gpu_acceptance.sh` 的 tag 写成 `6.0.0`（digest 照抄）→ ②恰好 1 条 offender；
  **SC3** 只把 `release.sh` 的 digest 末 4 位改掉 → ②恰好 1 条 offender；
  **SC4** 给 `python` 钉上 digest 但忘删例外 → 报"死登记"；
  **SC5** 新加一个没登记的 `FROM node:20.19.0` → 报"漏登记"；
  **SC6** 只在 `docs/GPU_HOST.md` 里退回裸 tag（脚本全对）→ ②开火 2 条（手册那一行是一次真实
  拉取，把运维侧写回可变 tag 就等于绕过配方）；干净副本 control 两把都不开火。
  ②的归属键**刻意剥掉 tag**：第一版把 `repo:tag` 当键，常驻开火对照
  （`test_sameness_criterion_fires_when_a_script_drifts` 的 tag 漂移那档）当场就不开火——
  键不相等，最常见的那类漂移反而完全看不见；改成剥 tag 的归属键后又单独给这个纯文本函数
  钉了一例（`test_image_path_key_strips_tag_but_not_registry_port`），因为 registry 带端口时
  那个冒号不是 tag 分隔符，只在最后一个 `/` 之后才找冒号。

### 配置面：把"设了也不生效"从一句提醒升级为机器不变量
- `default_idle_timeout_minutes` 在 `.env.example` 里有键、在 `Settings` 里有字段，唯一没有的是
  读取者。这类"预留开关"的真实危险不是功能缺失，而是**运维以为设了值就会超时停机**。
  本轮把这句话从 TECH DEBT 的段落挪进门禁（`tests/test_config_docs.py`）：
  按 AST 逐字段数 `app/`（排除声明所在的 `app/config.py`）里的读取位置——属性访问
  （`settings.x` 与 `self.settings.x` 同一种节点，不看接收者）与字符串形式
  （`getattr(settings, "x")`、`dict["x"]`）两种形态都算；
  **"零读取字段集合"必须恰好等于惰性登记表**，两个方向都会红：漏登记＝有人会按谎言设值，
  死登记＝文档宣称"不生效"而代码其实已经在读；登记项还必须在其 `.env.example` 条目的
  **紧邻上方**注释块里带"未启用"标记（写"预留"不算——误导来自"设了会生效"那句隐含话，
  只有明确否认它才叫澄清）。
- 普查读数：`Settings` 共 34 个字段，**恰好 1 个**零读取，就是它（不是"大概几个"）。
- 牙口读数（全在 /tmp 副本上做，主树不动）：**CFG1** 往 `app/deps.py` 追加一行
  `return settings.default_idle_timeout_minutes` → 零读取集合变空、该登记项被点名"死登记"；
  **CFG2** 把 `.env.example` 的"未启用"改成"预留" → 标记判据点名该字段；
  **CFG3（非恒真对照）** 探针在已知有读取者的 `ide_port_start` 上必须读到非空，
  在假字段名上必须读到空——否则"零读取"这句话只是探针坏了。
  纯函数侧另配四档边界：无注释／注释与键之间断一行／写了"预留"没写"未启用"／合规。
- 为什么仍然不实现自动停机：可信活动信号只有真机 GPU 利用率（容器 CPU 在 GPU 训练下会长时间
  接近 0，据此停机会误杀长跑任务并照秒扣费），被 NVIDIA 设备阻塞。区别在于——**延后现在是
  被机器看着的延后**。

### 调度并发：量出重试预算，然后把"等不到"和"没卡"分开报
- 优化对象是 `allocate()` 的重试预算（`ALLOCATE_MAX_ATTEMPTS=5`、`BACKOFF=0.05s`，
  注释里从没写过它们怎么来的）。真 PG 行锁上量：预算 0.500s、实际放弃发生在
  **0.816s / 0.821s**；16 线程抢 4 张卡 ×3 轮，成功分配的单个事务 median 30.5→43.1ms、
  max 35.5→70.8ms（两次运行），每轮赢家都是 **4/4**。⇒ 参数**不动**（最坏持锁 ≈70ms
  对 0.5s 窗口有 ≥7× 余量）；"改成 deadline 式等待"也被同一批读数否掉——拉长上界只是
  把假空概率换成更慢的首包，而真实缺陷是**两种原因共用一句话**。
- 那个缺陷可确定性复现（另一会话 `SELECT … FOR UPDATE` 持住唯一候选不放）：
  `allocate()` 每轮都数得到 `still_waiting = 1`，却仍抛
  `No GPU available with >= X GB VRAM`。现在分两句话：
  `GpuPoolContendedError`（"N 张卡在等锁，0.50s 预算内没等到，**不是容量不足**"）
  与原来的容量结论。SQLite 下 `FOR UPDATE` 是 no-op ⇒ 这类谎话只有真 PG 档看得见。
- 一条常驻用例的断言随之反转：`test_unbounded_candidate_read_starves_concurrent_allocate`
  过去只能钉住那句谎话（`match="No GPU available"`），现在它钉"整批候选被锁走时必须报
  contention、且不得出现容量那句"；反面同批补一档（需求 999 GB 压根不进等锁分支，
  仍报容量结论），保证新分支不是"什么都算 contention"。
- 变异对照 CONT1（把 contention 分支短路成 `if False`）：窗口那支与 starvation 那支
  同时红、`truly_empty` 那支照旧绿 —— 极性正确。
- 读法进 OPERATIONS：看到 `GpuPoolContendedError` 意味着"有人在同一批卡上抢"，
  不是池子配小了。全套 522 → 525（pg 档 18 → 21，§1/G0.18/§4 三处同步）。

### 两条前提竞态：各由一次真实红换来的修法（判据超时不动）
- 上一支（5eca7bf）之后的全量复算红了一条：`test_wait_ready_is_false_while_the_gpu_request_cannot_be_scheduled`
  在 `IndexError: list index out of range` 上崩——**崩在断言之后的取 Pod 那一行**
  （`_pods(api, name)[0]`）。同一次运行里其余 6 条真集群用例全绿、集群正常建起来了，
  所以这既不是 kind 引导失败（那是另一类环境红，见 OPERATIONS §7）也不是产品缺陷：
  `wait_ready` 的负向对照只给 6s 判定窗口，而 Pod 和它的 `conditions` 是集群控制器写的，
  单档复跑要 254s（kind 建集群就占掉 4 分钟）——宿主忙时"Pod 还没被建出来"完全正常。
- 修法不是把超时调大：判据行（`wait_ready is False`）留在最前，**佐证**两行改走
  `tests/k8s_server.py` 新增的 `poll_until` / `await_pod` / `await_condition_reason`，
  到点拿不到前提就抛 `AssertionError("前提未达成：…最后一次读数 …")`。
  区别在于报告说的必须是发生过的事：崩溃是"我不知道它在说什么"，
  "前提未达成"是"这条用例没资格给产品下结论"。
- 两个小工具本身离线可测（喂一个假的 list 函数即可），常驻 2 例：晚出现的 Pod 要真的轮询到
  第 3 次才返回、永不出现必须以"前提未达成"红、判词侧同理。修后单档复跑 7/7（254s）。
- **同形状的第二例是"换树换环境复算"红出来的**（在干净 worktree 里跑 HEAD：
  519 passed / 1 failed，`assert True is False`，而主树同一内容刚跑过绿）。docker 档
  `test_wait_ready_fails_when_container_not_running` 起一个 `sh -c "exit 0"` 就断言
  `wait_ready is False`——可"容器已启动"与"进程已退出"只差几毫秒，而这个工作区既没有
  ide_port 也没有 healthcheck，`wait_ready` 首次 inspect 抓到 running 就照实返回 True
  （**这是产品的正确行为**，另一条用例正是钉它的）。所以红的不是产品，是一条把前提当运气的用例。
  补了 `wait_exited`（`wait_running` 的负向对称体：超时抛"前提未达成"而不是返回 False），
  并给它配一支自己会开火的对照（起一个 `sleep 300`，2s 内必须红）。
- 两次的共同教训：**判据的超时预算是被测主张的一部分，红了的判据不能靠调大超时来治；
  需要等的是"别的东西异步做完"那个前提**，而且等不到时要报"前提未达成 + 最后一次读数"，
  既不能让一次竞速冒充产品结论，也不能让 IndexError 冒充"这条用例在说什么"。
  全套 519 → 522；修后 docker 档 21/21（57.6s）。

### 调度：把 `allocate()` 的排序策略量成表，再钉成判据
- `order_by(Gpu.memory_total.asc())` 就是分配策略本身（best-fit），但写在 SQL 里的排序
  没人知道它值多少：把它改成 `desc()` 全套用例照绿。本轮把排序抽成
  `GpuScheduler.candidate_order()`（默认逐字不变），新增 `tests/scheduler_policy_lab.py`
  与 `make policy-bench`：**同一个 `allocate()`、同一份工作负载，只换排序**。
- 实测台的第一版是废的，如实记下：舰队 192 GiB、负载合计 336 GiB，四种策略一律
  "接 8 拒 8、剩余 0"——那份读数只量出"池子不够"，分不出任何策略差别。
  改成**负载合计恰好等于池子容量**（每档各两张 + 两个 48 GiB 排在最后）后差异才显形；
  同时把卡片的入库顺序打乱，否则"按入库顺序"会因为 id 恰好与容量同序而与 best-fit 打平，
  那是建表顺序造出来的假平局。
- 本轮读数（`make policy-bench`）：best_fit 8/8、48 GiB 接 2 张、浪费率 1.00；
  arrival 6/8、pack_host 6/8（各拒两个 48）；worst_fit 4/8、浪费率 3.00。
  同一份硬件上排序改坏就少接 4 个工作区（吞吐 -50%）。
  `pack_host` 这一维今天没有独立后果：一个 workspace 至多绑一张卡（`uq_gpus_workspace`），
  它落后只是顺手浪费了小卡——多卡协同放置还不在这条路径上，这点如实写明而不是拿来邀功。
- 判据（`tests/test_scheduler_policy.py` 4 例）不比对字面量而比对表达式，
  并且**不经过实测台的认档函数**：变异读数 POL1（现产改 `desc()`）红 2 条、
  POL2（换成 pack_host）红、POL3（实验室漏复原生产排序）红 2 条、
  POL4（让排序根本不生效＝实测台失去区分力）红 2 条。
  另外两条如实记：POL5（只让认档函数谎报 best_fit）今天不改判决、不红；
  但配上 POL1 就会溜过去，所以直比那条断言是为此而留的——POL6（谎报 + 改向）红。
- 登记本身的两个坑也被钉住了：本轮两次把新行"锚在上一格那一行后面"，结果一条判据行
  落到 G0.30 之前、一条交付行落到 N-12 之前——**每行内容都对，只有顺序看得见**。
  新增 release 门禁 `docs_row_order`（`scripts/validate_release.py::row_order_offenders`，
  纯函数）：CURRENT_STATE 的 `N-x` 与 ACCEPTANCE_GATES 的 `G0.x` 必须按号递增出现且无重号，
  解析不到两行以上即报"判据会恒真"。常驻对照两例（合规表不开火；倒序／重号／空表三种
  都必须点名）。**它上线后几分钟就抓到我自己犯的同一种错**：下一节要加的 N-15 行锚在
  `| N-14 |` 前面插了进去，一次 `doc_row_order_discrepancies()` 直接报
  `编号非单调递增，相邻逆序对 [(15, 14)]`（读数原样留在本轮终端记录里）。

### 镜像 digest：从"建模了但从没人用"到写入入口 + 消费点
- 上一支把**外部基础镜像**钉住了，这一支补的是**我们自己产物**那一半。普查读数：
  `TemplateVersion.image_digest` 自 v0.4 起就在模型与 §15 文档里，但 `app/` 里
  **0 处写入、0 处读取**（本轮按 AST 数过）——也就是"字段就绪"一直被当成"镜像已钉住"，
  而 docker/k8s 两条 provider 实际启动的一直是 `image` 那个可移动 tag。
- 三处补齐，各管一段：
  `app/services/image_ref.py:pinned_ref(image, digest)` 是唯一消费点，落在 **workspace 快照**
  那一刻（`orchestrator.create`），所以两条 provider 路径不必各自再判一次"要不要钉"；
  形制不对、与 `image` 里已有的摘要冲突、或拼完超过 `String(255)` 列宽 ⇒ **拒绝**，
  绝不静默退回可变 tag（退回就是假装钉过）。
  CLI `python -m app.cli record-image-digest --template-id … --version … --digest …` 是写入入口：
  幂等（同值再记返回 0 且**不写第二遍**）、released 版本已钉在另一摘要时返回 3
  （同 tag 换内容应当发布新版本，而不是就地改写这条不可变记录）、版本不存在或没有 image 返回 2。
  `build_workspace_image.sh` 构建后打印摘要并给出上面那条命令；**拿不到摘要就退 2**，
  宁让这一列留 NULL（如实的"没钉"），也不写一个看起来像 digest 的字符串。
- 摘要来源是量出来的，不是听说的：本机 `docker image inspect --format '{{.Id}}'` 与
  `.RepoDigests[0]` 同值，两例各自独立——本地构建、从未推送的 scratch 镜像，以及拉取来的
  `postgres:16-alpine`。所以它确是这份 manifest 的内容摘要，与推没推送无关。
  反过来，`python:3.12-slim` 那条例外也由此更硬：本机缓存那份的摘要不是镜像站今天给的
  任何一个子清单摘要，即该 tag 已经移动过。
- 常驻验证 +10 例（全套 503 → 513）：整条链在**同一份真库**上连跑（CLI 回填 → 新建工作区快照
  带 digest → `docker run` argv 里就是那个 token），并断言"回填不改历史工作区的快照"
  （快照语义，否则不可变性会被追溯改写）；`test_image_digest_column_has_a_production_reader`
  盯着这一列别退回去，探针本身用已知有读者的 `current_version_id` 做非恒真对照。
- 变异读数（真树副本上逐条拆，主树不动）：**PIN1** 快照处退回 `template_version.image`
  → 链上两支红（普查那支仍绿，因 CLI 侧还在读这列——两条判据各看各的，别把绿读成"没问题"）；
  **PIN2** `pinned_ref` 放过坏形制 → `test_pinned_ref_arms` DID NOT RAISE；
  **PIN3** 允许就地挪针 → `test_record_image_digest_refuses_to_move_a_released_pin` 红；
  **PIN4** 分发处吞掉退码 → `test_main_record_image_digest_dispatch` 红。

### 连红两轮的谎话：workspace 在「还要重试」的那一刻被宣布死亡

`make validate` 连两轮给出同一条红，且这轮的报告第一次带得出用例名（G0.29 的 `failed_names`
在这里兑现）：`tests.test_workspace_credential::test_access_endpoint_returns_plaintext_password`，
`assert 'failed' == 'running'`。归因没有靠"再跑一遍看看"，而是临时挂了一个**仓库外**的 pytest
插件（`/tmp/gpu_trace.py`，跑完即删），把"失败瞬间"和"整轮收尾"两份域内现场一起打出来：

- 失败瞬间：8 张卡里 6 张 ALLOCATED、victim 自己一张都没拿到（容量那句 `No GPU available`
  在当时**是真的**，`still_waiting == 0`，不是上一轮那条 contention 谎话）；
- 整轮收尾：**同一个 workspace** 已经 `running`，它的 `provision` op 是 `succeeded(2)`。

两句合起来才是根因：那句"这个任务失败了"是第 1 次尝试替第 2 次尝试下的结论。
`_fail()` 每一轮失败都写 FAILED，而 worker 手里还有两次尝试；`workspace_operations` 在 API 层
零读者（`grep -rn WorkspaceOperation app/routers/` = 0），所以 status 是"还在重试"的唯一出口，
下一轮尝试开头还会把 `error_message` 清成 None——假死连痕迹都不留。

放大器另有一处，是本轮自己踩出来的：`tests/test_gpu_pool_guard.py` 的 `rig` 留下 8 行 CREATED
workspace，而 app 每次启动都跑 `reconcile_all()`，其规则包含"QUEUED/CREATED 且无 active op
⇒ 重新入队 PROVISION"——于是**下一个**起 TestClient 的模块的 worker 先替这份残骸去抢卡。
配对复算（本文件 + `test_api`）当场让 `test_api` 的 provision 报 `No GPU available`，
日志里 4 条 `provision(ws-guard) failed, retrying` 是它自己的 worker 在替别人重试。

修四处：

- **重试判据只留一份**：`OperationWorker.will_retry(op)`；`finish_failure`（写 RETRYING/FAILED）
  与 `orchestrator._fail(terminal=...)`（写 workspace 状态）同读它。两侧各写一遍
  `attempts >= MAX_ATTEMPTS` 时，任何一侧改动（调上限、新增不可重试错误）都会让
  "op 在重试"与"workspace 已 FAILED"同时成立。非终态失败写 QUEUED + 保留 `error_message`
  + 归还卡；终态才 FAILED。ADR 0002 两句原文都保留（异常不得泄漏、失败必带可诊断原因），
  只把"任何异常 ⇒ FAILED"这一句按尝试轮次分流，并加了修订小节。
- **前提预算与判据预算分开**：新增 `tests/settle.py:await_workspace_settled`，4 处
  `for _ in range(40): sleep(0.05)` 全部接上。旧的 2s 不是判据预算，是**误把 worker 的
  重试节奏（backoff 1s + 2s ⇒ 第 3 次尝试最早 3s 之后）当成被测主张的时限**；
  助手在终态才返回，等不到就报"前提未达成 + 最后一次读数 + 池内空闲卡数/ALLOCATED 数"。
  **没有任何一条判据的超时被调大**（`wait_ready` 6s、`wait_running` 30s 原样）。
- **谁留的行谁收尾**：`rig` 的 teardown 改成先 `scheduler.release` 还自己借的卡，再删自己的
  operation 与 workspace 行；`test_api::test_end_to_end_workspace_lifecycle` 补上它一直缺的
  `ensure_free_gpus` 前置声明（全套 8 张 mock 卡的共用池里，需要卡的用例必须自己达成前提）。
- **反证两支**：CONT2 把 `_failure_is_terminal` 短路成"永远终态"（＝修法之前）→ 本轮新增两支
  红、`tests/test_worker.py` 其余 10 支照旧绿，说明判据真接在它们身上；`await_workspace_settled`
  带正反两支（永远 queued 必须红且报出池子读数；`running`/`failed` 都不红，且第一次读数
  未收敛 ⇒ 它真在轮询而不是读一次就下结论）。

复算：全套两连绿 528 passed / 1 skipped（宿主 `vm.loadavg` 1 分钟值 15.7 与 28.4 各一轮，
这一族的复现本来就依赖负载窗口），全套 525 → 529。

**本轮明确不做**：不给共用池加 per-test 配额或改造成每用例独立库——那只是把"谁借谁还"的责任
挪进框架，而 N-17 的读数指向的正是"留下行的模块没收尾"这一条已经写进文档、这次被机器追上的规矩。

### 补一条自己写下的假阻塞：`python:3.12-slim` 的权威 digest 其实拿得到

上一轮把控制面基础镜像按**例外**登记，理由写着"Docker Hub 的三个端点本机实测均不可达"。
这句话今天被自己推翻：那三条路径全是 **CLI/curl 那条传输**（`auth.docker.io`、`hub.docker.com`、
`registry-1.docker.io` 确实都超时，`docker manifest inspect` 走 CLI 直连也超时），
但**守护进程自己那条出网路径一次都没试过**。一试就通：

- 权威读数：`docker pull --platform linux/amd64 python:3.12-slim` 打印
  `Digest: sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f`，
  与上一轮**独立**从 `public.ecr.aws/docker/library/python` 读到的索引 digest 逐字同值
  （镜像站复制的是同一份 manifest，同值本就是预期——差别在于这次一手来自权威侧）；
  再 `docker pull docker.io/library/python@sha256:f77ac9e4…` 按 digest 直拉一次，成功。
- 钉进 `runtime/Dockerfile.control-plane`：`FROM python:3.12-slim@sha256:f77ac9e4…`
  钉的是**多架构索引**（本机 arm64 与 GPU 主机 amd64 各自按平台解析），tag 只留可读性。
- 构建侧实跑：`make control-image` 的 `Step 1/10` 用的正是这条引用，`Successfully built
  53af23a7ecd0`，产物容器内 `python -V` ＝ `Python 3.12.14`；构建产物随后 `docker rmi` 删除。
  顺带记一笔：**在此之前没有任何常驻门禁构建过控制面镜像**（`make validate` 的 `build`
  检查量的是 `python -m build` 出的 wheel），所以这条配方能不能构建此前只有文档说法。

**登记表清空带来的真问题**：`assert unpinned == set(UNPINNED_EXCEPTIONS)` 在两个集合都为空时
与恒真同形——"以后有人加裸 tag 忘了登记"和"钉上了忘了删登记"都会静默通过。所以双向对账
抽成纯函数 `_exception_table_offenders(unpinned, registered)`，并补常驻注入夹具
`test_exception_reconciliation_fires_in_both_directions`：漏登记开火、死登记开火、
"两侧相等"与"两侧皆空"都不开火；作用域判据同时改成"真实树必须**零个**未钉的外部基础镜像"。
supply-chain 档 10 → 12（两支注入夹具：表级双向 + 条目级三把判据）。

方法论记账（写进 SUPPLY_CHAIN §8 与 CURRENT_STATE 的已解除清单）：本轮之前已经有**两条**
同类假阻塞——"Isaac Sim 钉 digest 阻塞于 NGC 凭据"（匿名 pull 令牌就能解析 manifest/digest）
与这条"三条路径不可达"（漏了守护进程）。规则改写成：**写"取不到"之前必须先把通道列全**
（CLI 直连／守护进程／构建器／另一台机器），并逐条记下哪几条试过、怎么试的、失败形状是什么。

### 把"外部阻塞"查到根：一条是假的，一条是真的；顺手给配方补上第一把运行时门禁

`integration_k8s` 从 v0.3.0 起一直挂着同一个原因："需要真实集群 + NVIDIA Device Plugin"。
本轮按最高指令的技术选型规矩先做候选调研，再回到被测代码定位绑定约束，结论是
**这句话把两道门混写成了一道**：

- 调研侧（逐个一手源，看到什么写什么）：`NVIDIA/k8s-device-plugin` README 里**没有** fake/mock
  模式，只有 `FAIL_ON_INIT_ERROR`——原文是"allow the plugin to deploy successfully on nodes that
  don't have GPUs"，即"没 GPU 的节点上不崩"，不是伪造可分配设备；HAMi README 的前置条件仍写着
  `NVIDIA driver >= 440`（检索命中的"Fake GPU + HAMi 教程"来自内容聚合站，未采信为证据）；
  kubernetes.io 的 device-plugins 概念页正文被截断，`#examples` 一节没读到，所以只能说"可见部分
  没提到假设备插件"；GitHub 仓库检索两次返回 0 命中，按既有教训记为**工具盲区**而不是"生态没有"。
- 代码侧才是决定性的：那个用例的 Pod 镜像取自 `app/services/providers/k8s.py:190` 的
  `workspace.image or template.image or settings.workspace_image`，夹具建的 Template 不带 image
  ⇒ 落到 `settings.workspace_image`，也就是 **amd64 + NGC 基座、本机既没构建也没推送**的 workspace
  镜像。就算假造出容量，Pod 只会停在 ImagePullBackOff，300s 就绪窗口照样红。
  **绑定约束是镜像与 x86 主机，与 device plugin 无关。**

决定：不做假 device plugin 档，把这段调研写进 `docs/ACCEPTANCE_GATES.md` 末尾附注与 §1 的 PENDING 行
（用途：下一个读这格的人不必再花一轮去试"能不能假造"）。

同轮把上一轮那次一次性构建实测提成常驻门禁。此前**没有任何常驻门禁构建过控制面镜像**
（`make validate` 的 build 检查量的是 `python -m build` 出的 wheel），"钉进去的 digest 其实取不到"
这类错误只会在别人 `make control-image` 的那一刻暴露。新增
`tests/test_docker_provider_integration.py::test_pinned_base_of_the_control_plane_recipe_is_fetchable`：
用守护进程那条传输真的 pull 配方里的钉死引用，并要求 `inspect` 出非空架构与 `sha256:` 开头的 Id。

- 负向对照（digest 首位翻转后必须取不到）本轮实测开火：
  `Error response from daemon: failed to resolve reference "docker.io/library/python@sha256:077ac9e4…"`。
- 它的代价也实测了：同一台 daemon 上正向 pull 37.7s、翻转后 91.8s（registry 一趟就是几十秒），
  所以控制档默认只出读数、置 `EMBODIEDCLOUD_RECIPE_BASE_CONTROL=1` 才开火——要证的那件事
  （pull 按 digest 而非按 tag 解析）不随每轮代码变化。默认档整支 21.99s。
- 附带读数：`python:3.12-slim` 今天的 `RepoDigests[0]` 与钉住的那份**相等**，尚无漂移；
  判据刻意不断言这个等式（钉住的内容本来就该在 tag 移动后保持不变，断言相等等于制造
  一个"每漂移必红"的项，把配方钉反）。

docker 档 21 → 22，全套 531 → 532。

### 镜像层清单：`make image-sbom` 与它的三把判据（G0.35，SUPPLY_CHAIN §8 第 3 项闭合）

- **选型是被通道读数改掉的，不是被偏好改掉的**。功能上更对口的候选（syft，唯一职责就是出清单、
  SBOM 模式不需要再下载任何东西）今天拿不到字节：`docker pull` 走守护进程配置里的镜像站
  `docker.1panel.live` 时 TLS 握手超时（上一轮它是这台机器唯一通的那条）、`ghcr.io` 拨号超时、
  GitHub release 下载在宿主 `curl` 与容器内 `urllib` 两处都不通。另一候选（trivy）在自己的
  `docs/getting-started/installation.md:12-16` 里**列了三个官方注册表**，其中
  `public.ecr.aws/aquasecurity/trivy`（同一文件第 16 行）当场可拉，且第 22 行明文支持
  "挂容器引擎 socket 扫镜像"这种接法。六维逐项出处：License 两份都从
  `raw.githubusercontent.com/.../LICENSE` 读到 Apache-2.0 正文；活跃度取
  `api.github.com/repos/{anchore/syft|aquasecurity/trivy}` 与 `releases/latest`
  （syft v1.52.0 发布 2026-09-17／9,613★，trivy v0.74.0 发布 2026-08-14／38,083★，两者仓库
  pushed 都是 2026-09-25）；签名形态是 31 vs 47 个 release 资产（cosign 签名 checksums vs
  逐件 sigstore）。第三个候选 `docker build --sbom/--attest` 本机直接不可用
  （`docker buildx version` → `docker: unknown command: docker buildx`）。
  **一处自我更正**：先前把"trivy 运行时要下载漏洞库"记成它的安全风险——真跑之后 trivy 自己打印
  「`--format cyclonedx` disables security scanning」（与它 `sbom.md:203` 一致），那条主张撤回。
- **钉的 direction 单独核过**：用 ECR Public 的匿名令牌 + `Accept: …image.index.v1+json` 取回
  manifest body（3772 B，`application/vnd.oci.image.index.v1+json`），逐字节重算 sha256 得
  `62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969`，与 `docker pull` 打印的
  `Digest` 同值；子清单 amd64 `ee940acb…`／arm64 `55ad20f8…` 各在——钉的是**多架构索引**而不是
  本机 arm64 那一份。上一轮刚写下"钉错方向会让所有人的构建当场失败"，这一轮不靠守护进程的打印说话。
- **落成的东西**：`scripts/image_sbom.sh`（工具镜像按 digest 钉死、被审镜像不在场退 2 而不是交
  空产物、结果走 stdout 再 `.tmp`→`mv`）＋ `Makefile` 新 `image-sbom` 档（与 `control-image` 同档，
  **不进** release 链，理由写在 RELEASE_PROCESS §6）＋ `scripts/check_image_sbom.py`（产物层判据：
  被审对象 `type=container` 且 purl 带 `@sha256:`、≥1 `pkg:deb/`、≥1 `pkg:pypi/`、
  `bomFormat=CycloneDX`、`components` 非空、不得混漏洞结论，外加"文件不存在必须红"）。
  `--self-test` 14 档全 OK（每档核对**精确条数**，不只判"有没有开火"——一条注入顺手打中三条判据时，
  只判真假的那版会让从未单独运行过的条款藏在邻居后面），`mypy` 零错。
  **加固来自一轮独立评审**（子代理交回 10 条，逐条重开原行后落地 7 条）：配方层原判据只认
  `*_IMAGE="${VAR:-…}"` 这一种赋值形状，把变量改名成 `TRIVY_IMG=`、或直接在
  `docker run` 那一行写 `aquasec/trivy:latest`，都能绕过而判据照绿——现在判据看的是
  **会被拉起来的那些引用**（折续行、去整行注释、认 Docker Hub 两段名，路径与挂载点不算镜像），
  四种绕过形状各有注入对照；产物层加 `--image-id`（清单自报摘要必须等于 `docker image inspect`
  的 `.Id`）——在此之前"扫错对象也能过"是真实存在的：接线用例自己就拿基础镜像跑，
  两层条款它同样满足。另补 2×3 组合的常驻分流（坏摘要在任何 daemon 读数形状下都必须红；
  上一版按关键字先跳过，等于让"钉错 digest 恰好被传输问题掩盖"免检）、空 stderr 的
  `splitlines()[-1]` 越界、以及失败时 `.tmp` 残留的 trap。
- **真读数**：trivy 0.74.0 对配方里那份钉死的基础镜像 `image --format cyclonedx` 用时 12.5s，
  `components=89 / deb=87 / pypi=1 / spec=1.7`，其自报 purl 摘要 `f77ac9e4…` 与 §2 钉进
  `Dockerfile.control-plane` 的 digest 同值——两把独立的尺子（构建配方／清单工具自报）对上同一个事实。
- **一条我自己写错又改回的归因（记账，免得下一个人照抄）**：第一次运行用
  `-v /tmp/trivyout:/out --output /out/x.json`，trivy 退 0 而宿主目录是空的，我据此写下
  "trivy 会 rc=0 却不落盘"。这个成因是**错的**：这台机器（colima）的 `/tmp` 根本不是共享进虚拟机的
  挂载点。同一分钟内两半对照——容器内 `echo hi > /out/via-tmp.txt` 在容器里看得见、宿主
  `/tmp/mnttest` 为空；把挂载点换成工程目录下的 `dist/mnttest` 再写同一个文件，宿主立刻可见。
  触发重测的是同一只空挂载的另一个症状（`-v /tmp/probe.py:/probe.py` 报
  "can't find `__main__` module"）。**改的是夹具，不是结论的形状**：判据照旧按"产物说不说得出事实"写
  （rc=0 配一份空产物是真实失败模式），只是不再把它安在 trivy 头上。
- **常驻把关分三层，判据只有一份实现**：配方层（工具引用必须钉 digest 且逐字进文档，裸 tag／
  钉了没进文档两个方向各注入一次）、产物层（同上条款的注入夹具，跑的是量具本体）、
  接线层（真 pull 钉死的工具、真挂 docker.sock、真过同一份判据，整支 18.8s 且是与一次构建并发跑）。
- **两条常驻机制的补强**：`test_pinned_base_of_the_control_plane_recipe_is_fetchable` 今晚被镜像站的
  一次 `not found` 打过（对**有效**摘要回 not found，同一条通道上一轮还能 pull 成功），于是
  "取不到就红"换成两条传输定案的三档分流：present→skip 并把两条读数都打出来、absent→红＝钉错、
  unknown→红＝无法定案不洗；分流逻辑抽成纯函数 `_pull_failure_action` 并配 2×3 全组合常驻对照
  （坏摘要在任何 daemon 读数形状下都必须红——上一版按关键字先跳过，等于让"钉错 digest
  恰好被传输问题掩盖"免检），
  第二通道自己也有正反对照（真摘要 present／翻一位 absent——否则它就是免检通道）。
  `tests/k8s_server.py:kind_binary()` 补第三档发现位：本机 kind 在仓库同级的 `.toolcache/`
  （实测 `kind version 0.33.0`＝ADR 0009 钉的那版），上一轮能跑靠的是某个 shell 导出过
  `EMBODIEDCLOUD_KIND_BIN`，换个 shell 就静默跳 7 例；补上发现档后真集群 7/7 恢复，用时 2:08。
- **顺手补掉它照出来的盲区**：`make image-cve`（同一份钉死的 trivy 加 `--scanners vuln`）对控制面镜像真跑出 `os-pkgs 156 / lang-pkgs 6`（HIGH 44／MEDIUM 58／LOW 58／UNKNOWN 2），而 `make audit`（uv）对这 156 条一无所知。库通道是量的不是猜的：trivy 自己 `--help` 给的默认两条（`mirror.gcr.io/aquasec/trivy-db:2`、`ghcr.io/aquasecurity/trivy-db:2`）本机分别 `connect: connection refused` 与拨号 i/o timeout，只有 ECR Public 同命名空间那条通（匿名 manifest GET 200）。漏洞库**按设计不钉 digest**（钉住＝把扫描冻在过期库上，把「库里还没有」读成「没有漏洞」），因此走登记式免检 + 双向对账，而不是偷偷放行。**这一步今天只出报告**：44 条 HIGH 一条都没分诊，先接阈值的结果会是每次都红→整步被跳过；报告与 `docker image inspect .Id` 逐字绑定（实测同值 `sha256:cd371b31…`）。
- 工具引用收成一个文件：`scripts/trivy_tool_ref.sh`（`TRIVY_IMAGE` + `TRIVY_DB_REPOSITORY` 各一份默认值），`image_sbom.sh` 与 `image_cve.sh` 都 source 它——判据的覆盖面随之从单文件改成三份并扫，并加两档"谁在被扫"的断言，防止扫描面哪天缩水成只看一边。
- **真产物最后落地，过程值得记**：`make image-sbom` 对 `embodiedcloud/control-plane:0.7.0` 交出
  `dist/sbom.image.cdx.json`，读数 `components=137 / deb=87 / pypi=49`，并绑到
  `docker image inspect` 的 `.Id`＝`sha256:cd371b31…`（三处同值：Id＝trivy 自报 ImageID＝purl 摘要）。
  同一配方今晚**前 5 次全败**在 `Step 7/10 : RUN pip install`（153s／102s／224s／156s／50s），
  第 6 次 165s 成——把成因定成"容器侧→`files.pythonhosted.org` 的 TLS 超时"而不是"配方坏了"，
  靠的是同一时刻两条并排探针（容器内取 `pypi.org/simple/setuptools/` 是 200／535 KB／1.3s；
  宿主 `curl` 同一条 CDN URL 拿得到 302）与上一轮同一配方 160s 成功的那份读数。**没有**因为
  "连红五次"就去改配方、加大 pip 超时或换基础镜像——那等于把外网波动记成代码变更。
  OS 层的 CVE 比对新立 SUPPLY_CHAIN §8 第 5 项，不顺手并进本轮。
- **连带更正三处旧措辞**（起因：把"哪些登记把单条通道当成了整件事"派给子代理普查，交回 16 条
  候选，逐条重开原行后落定）：`docs/ACCEPTANCE.md:19` 与 `docs/IMPLEMENTATION_PLAN.md:40` 都写着
  "当前执行环境没有 Docker daemon"，被 `scripts/release.sh:215`（"本环境可用（colima）"）与本轮
  `docker version` → Server 29.5.2 双重反驳，按读数改成"缺的是 NVIDIA GPU 与 NGC 登录态"；
  SUPPLY_CHAIN §3 那条"code-server 上游不发校验文件"补了第二条独立通道（GitHub release API 逐枚
  枚举 v4.130.0 的 9 个资产，确无校验／签名文件）。**复核后不改的一条**：子代理报
  `tests/test_k8s_integration.py:46` 裸调 `load_kube_config()` 会忽略 `KUBECONFIG`——SDK 默认位置
  本来就吃该环境变量，本仓 `tests/test_k8s_control_plane.py:82` 上一轮已记过，按不成立处理。
- **子代理分工（本轮新做法）**：「SBOM 工具调研」与「过期单通道措辞普查」两件派给独立子代理
  （分别 47／35 次工具调用），主理人只做实现、判据与定档。进判据的部分（三个注册表、socket 挂载
  接法、`--format cyclonedx` 关扫描）全部由主理人重开原文或真跑核实；普查那条 k8s 主张在进门禁前
  被复核推翻。

### 镜像层漏洞扫描：162 条命中读成一张分诊表，顺手量出一个锁文件管不到的面

- **`make image-cve` 真跑通了**，读数：`embodiedcloud/control-plane:0.7.0`
  （`ImageID=sha256:cd371b31…`）→ `os-pkgs 156 / lang-pkgs 6`，按等级
  `HIGH 44 / MEDIUM 58 / LOW 58 / UNKNOWN 2`，合计 **162 条**，冷跑 2.5s（库缓存 1.4 GB 已就位）。
  这些是 `make audit`（uv 只导 wheel 清单）**结构上看不见**的那一层。
- **库通道是量出来的，不是抄默认值**：trivy 默认那两条（`mirror.gcr.io/aquasec/trivy-db:2`
  `connect: connection refused`、`ghcr.io/aquasecurity/trivy-db:2` 拨号 i/o timeout）本机都不通，
  只有 ECR Public 同命名空间那条通（匿名 manifest 200）。工具引用收进唯一一份
  `scripts/trivy_tool_ref.sh`，两个步骤都 source 它；配方层判据因此从"单文件扫赋值"改成
  **跨文件扫会被拉起来的逻辑行**，并把漏洞库通道放进登记式免检（`TOOL_UNPINNED_EXCEPTIONS`，
  grade `accepted-risk`，理由 ≥40 字）——不钉 digest 是设计：钉了就等于把扫描冻在过期库上。
  免检表与文档双向对账：登记了但文档没写、或登记了却已经不违规，两侧都会红。
- **分诊做下来推翻了"缺的是阈值"这个前提**。逐条读 44 条 HIGH：塌成 **17 个二进制包／8 个 CVE**，
  9 个包（util-linux 源包的三种 epoch 写法）共享同一组 4 个 CVE，一条就占 36/44；
  `FixedVersion` 这个键在 156 条 OS 命中里**一条都没有**（162 条里只有 6 条带它，全在 `lang-pkgs`），
  `Status` 分布 `affected 154 / fix_deferred 2 / fixed 6`，HIGH 那一档是 `affected 43 + fix_deferred 1`。
  **裁决：这一步保持只出报告，不接阈值**——今天接"HIGH==0"就是一条我们无能为力（43 条上游没发版、
  1 条 Debian 自己标 `fix_deferred`）的红，正是"每次都红→整步被跳过"的死法。真正可行动的尺子是
  `Status == fixed`：6 条全落在基础镜像自带的 `pip 25.0.1` 上（`PkgPath` 只有一条
  `usr/local/lib/python3.12/site-packages/pip-25.0.1.dist-info/METADATA`），而 `pip` 不在 `uv.lock` 里
  （`grep -c '^name = "pip"$' uv.lock` → `0`），所以 wheel 层清单与 `make audit` 对它全盲。
  UNKNOWN 那 2 条 `SeveritySource` 均为 `null`、其中一条编号还是 Debian 占位 `TEMP-1147318-639065`，
  按"看不见"处理，不折算成"没有漏洞"。
- **给镜像打清单这件事，代价是当晚就撞出一个真缺陷**（登记表 N-22）：第一次把镜像里的
  `pkg:pypi` 与 `uv.lock` 逐名比对，`sqlalchemy` 镜像 `2.1.1` vs 锁 `2.1.0`、`pip` 整个不在锁里。
  根因是配方 `runtime/Dockerfile.control-plane:14` 用 `pip install ".[postgres]"` 在构建时**现解析**——
  `make verify-lock` 全绿也管不到镜像里装的是什么。修法与选型另起一段（下一节），不改判据先不闭。
- **自己这次重构留下的回归，是常驻用例抓住的**：把工具引用从 `image_sbom.sh` 挪进
  `trivy_tool_ref.sh` 之后，docker 档那条接线用例还在按"赋值就住在消费脚本里"的旧前提解析单文件，
  直接读到空集而红（`image_sbom.sh 里解析不到工具镜像赋值，这支判据会无事可做`）。修法不是把引用
  抄回两处，而是让解析面跟着"source 关系"走——`_tool_image_refs_in([消费方, 被 source 的那份])`，
  并把原判据的"非空"升级成"**恰好一份**"，这样以后多出一份也不会被 `sorted()[0]` 悄悄挑掉。
- **lint 覆盖面补一格**：`make lint` 原来只扫 `app tests edge_agent`，而常驻用例 import 的
  `scripts/check_image_sbom.py` 不在里面——本轮它确实有一行 123>120 而门禁全绿。把 `scripts` 纳入
  lint 范围（`ruff check app tests edge_agent scripts` → `All checks passed!`），以后量具自身也在闸内。

### 镜像内容与锁文件对上：`pip install ".[postgres]"` 换成 `uv sync --frozen`（N-22 闭合）

- **这条缺陷是清单一手量出来的，不是审查出来的**：给镜像打漏洞库时顺手把镜像里的
  `pkg:pypi` 与 `uv.lock` 逐名比对，抓到 `SQLAlchemy 2.1.1`（锁钉 `2.1.0`）。根因是配方
  `runtime/Dockerfile.control-plane:14` 那条 `RUN pip install --no-cache-dir ".[postgres]"`
  在构建时对 PyPI 现解析——`make verify-lock` 全绿也管不到它，因为它验的是仓库里的解析，
  不是镜像里装上的东西。
- **选型四档，全部开过官方文档**（docs.astral.sh/uv 的 docker／sync／export／uv-pip-sync 四页）：
  多阶段 `uv sync --frozen`＋COPY `.venv`／`uv export` 出 requirements 再 `pip --require-hashes`／
  `uv pip sync` 直读锁／pip-tools 自解析。第三条被文档自己否掉——`uv pip sync` 枚举的支持格式里
  没有 `uv.lock`；第四条等于再造一个解析器（两份真源）；第二条的哈希校验确实强（实测 pip 25.0.1
  在全部哈希置零时回 "THESE PACKAGES DO NOT MATCH THE HASHES"），但要留一份会漂移的
  `requirements.txt`，而 uv 文档自己不建议同时保留两份真源。**引依赖＝第一条**。
- **uv 到底校不校验锁里的哈希？实测两条极性**（本地 uv 0.11.28，冷缓存、真下载）：
  把 `uv.lock` 里 **wheel** 的 sha256 整条置零 → `uv sync --frozen` 退 **1** 并打印
  "Computed: 946d195a…"；还原后同命令退 **0**。记账一次自己的无效测试：**头两次我把 sdist
  那一行改了、而安装走的是 wheel，于是得到两次"uv 不校验哈希"的错读数**——错的不是 uv，
  是夹具没打在该打的那一行上。`$?` 取在 `| tail` 之后也骗了我一次，改成先重定向再取退码。
- **改完的配方与产物**：builder 用 `uv sync --frozen --no-dev --extra postgres
  --no-install-project --no-editable` 装出 `/app/.venv`，运行层只 COPY 那份 venv 加源码并把
  venv 放进 PATH（迁移 job 的 `command: ["alembic", "upgrade", "head"]` 因此仍解析得到）。
  真跑 `make control-image`＝**55.6s**（旧配方 160～165s，大头是现解析＋构建隔离装 setuptools），
  产物 `import app.main` OK、`python -c "sqlalchemy.__version__"`＝`2.1.0`、`psycopg 3.3.6`。
  重出清单：`components=135 / deb=87 / pypi=47`（少掉的两条是原先作为发行包装进去的本项目
  dist，旧清单里它以同一个 purl 出现了两次），**46/47 与 `uv.lock` 逐名逐版本相等**，
  唯一剩下的 `pip 25.0.1` 是基础镜像自带的。
- **两条常驻判据，反证用真产物**：配方层 `test_control_plane_recipe_installs_from_the_lock`
  三档注入（退回旧配方／摘 `--frozen`／换成 `pip install --require-hashes -r` 的合规变体），
  产物层 `check_image_sbom.py --lock uv.lock` 的白名单只认 `pip`（改个名即红）。反证是从旧配方
  那台真镜像（`.Id`＝`sha256:cd371b31…`，dangling 还没被清掉）跑真 trivy 得到的清单里逐字节裁出的
  `tests/fixtures/sbom.image.prefix-drift.json`，判据对它开 1 条点名 `sqlalchemy 2.1.1 vs 2.1.0`。
  判据自己的 `--self-test` 从 14 档加到 **23 档**（逐档核对精确条数）。
- **两处判据缺陷是被自己写的反例抓出来的，不是评审抓的**：① 配方判据原来直接读原文，
  注释里那句讲 `uv sync --frozen` 的话替被摘掉旗标的 RUN 行背书 → 改成只看去掉注释、
  折好续行之后的指令行；② 判据最初钉成"必须有 `uv sync --frozen`"，于是合法的
  `uv export --frozen` ＋哈希安装被误红 → 改成判"uv 读锁"这一族（`sync|export|pip sync` 带
  `--frozen`），因为要钉的性质是"这一组依赖由哪把锁决定"，不是工具名字。
- **`COPY --from=` 进钉死判据的扫面**：多阶段之后 builder 还拉一份第三方工具镜像
  （`ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b1…`，digest 由两条独立通道同值取证：宿主匿名
  令牌回的 `Docker-Content-Digest` 与对 2196 B 索引字节自算的 sha256 逐字相等；`docker pull` 退 0）。
  如果判据只看 `FROM`，"把 uv 换成裸 latest"能一路绿过所有钉死判据。阶段名（`COPY --from=builder`）
  按同文件声明的名字扣除，两个方向各有注入档。docker 档那条"取不到怎么定案"的覆盖面同时从
  "第一个 FROM"扩成"配方里所有钉死的引用逐个 pull"，并为 ghcr 补了第二条通道（同注册表、
  换传输——它能定案"摘要在不在"，不能定案"这家注册表有没有被篡改"，读数里写清楚）。
- **顺手量出第二个盲区并修掉**：`make sbom` 用的是 `uv export --frozen --format cyclonedx1.5`，
  默认只导主依赖集——导出的 **44** 个组件里没有 `psycopg`／`psycopg-binary`（生产镜像装的驱动）、
  没有 `boto3`，而 `dist/sbom.cdx.json` 是进 `dist/checksums.txt` 的发布产物。加
  `--extra postgres --extra s3` 后实测 **51** 个组件、三条到齐，dev 组仍不进（点名而不是
  `--all-extras`）。留一格没做：wheel 清单（51）与镜像 pypi 组件（47）的基数本来就不该相等，
  "部署要用的 extra 与配方里 `--extra` 那几个名字是否同集合"目前没有判据在核。
- **通道读数又翻了一次**：上一轮记的是 `ghcr.io` 拨号 i/o timeout，本轮 `docker pull ghcr.io/…`
  退 0 且 manifest 取到——同一台机器、隔几小时两种相反读数。所以 §8 那条方法论再加一句：
  写"这条通道不行"只在它被记的那一刻成立，下一次要重跑而不是引用。
- **两条钉死方向的复核与一次真服务**（都是给这轮的改动补的证据，不是新功能）：
  ① 那份 uv 引用的 digest **钉的是索引而不是本机 arm64 平台清单**——现取现数：`mediaType` ＝
  `application/vnd.oci.image.index.v1+json`，4 条子清单里 `linux/amd64`＝`sha256:d46db4c7b7f2…`、
  `linux/arm64`＝`sha256:de342e010065…` 各在（另两条是 attestation 的 `unknown/unknown`）。
  生产那批主机是 amd64，钉错方向等于让所有构建当场失败，§2 基础镜像那行记过同一课。
  ② **控制面镜像第一次被真的服务过一次**：`docker run -d -p 127.0.0.1:18112:8000` 起容器后
  `GET /api/health` 第 3 次探测回 `200`＝`{"status":"ok","provider":"mock","provider_ready":true,
  "version":"0.7.0"}`，`GET /metrics` 回 `200`／3770 B Prometheus 文本，容器随后 `docker rm -f`、残留 0。
  在这之前，仓库里关于这份产物的取证最远只到 `python -V` 和 `import app.main`。
- **生产架构那一侧复算过，方法是被迫换的、结论是可复用的量具**：先试 `docker build --platform
  linux/amd64`，**失败且失败方式有教育意义**——本机 docker 29 没有 buildx 插件（`docker buildx` 报
  unknown command），退回 legacy builder，它不把 `--platform` 传进中间容器（日志三次打印
  "…and no specific platform was requested"），于是 builder 阶段其实按 arm64 跑完，最后被判
  "does not provide the specified platform (linux/amd64)"；把那个中间镜像 inspect 出来是 `arm64`。
  换路径：`docker run --platform linux/amd64` 起同一份基础镜像的 amd64 子清单（容器内 `uname -m`＝
  `x86_64`），送进 `pyproject.toml`/`uv.lock` 与从钉死工具镜像里取出的 uv，跑**与 Dockerfile 同一行**
  的 `uv sync --frozen --no-dev --extra postgres --no-install-project --no-editable` → 退 0；
  装完在 x86_64 解释器下 `sqlalchemy 2.1.0`＋`psycopg 3.3.6` 都能 import，`alembic --version`＝1.20.0，
  site-packages 里有 1 个目录名带 `x86_64` 标签的发行。**顺带钉住"钉的是多架构索引"这句话**：那份
  `ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b1…` 在 amd64 侧解析出来的 uv 是
  `ELF 64-bit … x86-64, statically linked`——生产那批 x86 主机拿得到工具，不是我们的希望而是量到的读数。
  这一轮把它固化成 `make amd64-probe`（`scripts/probe_control_plane_amd64.sh`，人工/CI 档，
  容器用完即删），并把"本机别拿 `docker build --platform` 做跨架构复算"写进 OPERATIONS。
  未覆盖：`docker build` 的跨架构 plumbing 本身（要 BuildKit，且 `docs.docker.com` 今晚两个页面都
  fetch failed，取不到原文，所以只记本机观测、不记版本结论），以及在真 amd64 主机上跑完整构建。
- **N-23 留下的那一格也补成了判据**（派给子代理实现、主理人逐行重读后收下）：镜像真装的每一组 extra
  必须被 `make sbom` 的导出命令声明过，方向是**单边子集**、权威侧是配方——清单比配方宽（今天多声明
  一组 `[s3]`）合法，反过来就是刚修掉的那个缺陷的形状。两侧非空各有一道独立守卫，并且有一档
  "两边同时为空"的控制专门证明它不是子集判据的副产品。两处细节是子代理自己抓到的、比我下的任务书更对：
  配方侧必须走"去掉注释、折好续行"那份解析（`Dockerfile.control-plane:25` 的注释里就写着字面的
  `--extra postgres`，不剥注释会凭空多算一组），以及 `--extra-index-url` 要用后置 lookaround 挡住、
  不能读成一个 extra 名字。`tests/test_supply_chain.py` 19 → 20 例，全套 542 → 543。
  ③ 漏洞基线在**改造后的那份镜像**上重跑一遍（`.Id`＝`sha256:aa6500ec…`）：162 条、
  `os-pkgs 156 / lang-pkgs 6`、`HIGH 44`、17 包 8 CVE、带 `FixedVersion` 的 6 条全部逐格不变——
  换 wheel 没有动那 156 条 OS 命中，基线不绑定某一次构建。`scripts/image_cve.sh` 末尾那句
  "HIGH 未分诊前不接成门禁"是改前写的，已按分诊结论改成陈述事实而不是陈述待办。

### 闭合之后再过一遍刀：评审量出「门禁全绿但覆盖是假的」两处（N-24）

- **做法**：本轮四笔提交交给一个只读评审子代理（45 次工具调用），任务书里点名要找的六类病
  （恒真控制、极性缺一侧、断言强于证据、参数没转发、跨文件重复决定、死代码）。交回 6 项，
  我逐项重开原行与真镜像后落 5 项。**评审的读数不直接采用**：它说"两条 lookaround 都不起作用"，
  我自己在内存里把两条分别删掉跑六份输入，实测**后置那条确实删了没差别、前置那条删了会把
  `x--extra grpc`／`---extra grpc` 读成一组 extra**——于是只删不起作用的那条，并把注释改成
  实测因果（它原话"两道缺一不可"是假的）。
- **两处「闭合之后仍然是错」都是真的**：① `docs/OPERATIONS.md` 还在教运维"在容器里
  `pip install ".[s3]"`"，而新配方下容器里 `pip` 属基础镜像（实测 `readlink -f $(command -v pip)`
  → `/usr/local/bin/pip…`，`python` 是 `/app/.venv/bin/python`）——照做就是把 SDK 装到应用
  import 不到的地方；同一晚 `pyproject.toml` 的注释已经改成"构建时 `--extra s3`"，
  **同一件事两个面互斥**。② 生产清单两处 `image: embodiedcloud/control-plane:0.4.0` 停在三个版本
  之前，而版本一致性判据只读另一份清单——绿灯的覆盖面是假的，走那份部署连新镜像都拿不到。
  都已改，并把 `test_k8s_manifest_version_matches` 从"读一个文件"扩成"扫 `deploy/kubernetes/*.yaml`
  全部＋钉分母下界（≥3 处）＋就地注入旧号必须被点名"。
- **镜像运行时形状第一次被钉成判据**（docker 档）：venv 的 `python`、`app` 从源码 import、
  `alembic` 解析到 venv 且 `alembic heads` 真列修订号（迁移 job 依赖这条，此前无人验证过
  `WORKDIR /app` + `prepend_sys_path = .` 在 venv 布局下是否还成立）、`pip` 属基础镜像
  （把运维陷阱的成因从巧合变成被钉住的事实，它翻转就红）、本项目两个 `[project.scripts]`
  名字**有意**不在镜像里。
- **量具的自测接进每轮**：`--self-test` 那 23 档此前只有人手跑，`lock_offenders` 一族等于没有
  常驻反证。现在由 `test_image_sbom_validator_self_test_runs_in_this_gate` 起子进程跑它，
  除退码外还钉"无 BAD 且档位数 ≥23"（只判全 OK 不够——档数掉到 1 也全 OK）。牙是量过的：
  把 `lock_offenders` 换成永不开火的桩 → 6 档转 BAD、退码 1。
- **第二通道的分支覆盖**：present/absent 极性对照原来只核 `pinned[0]`，那么多阶段之后新加的
  ghcr 分支（同注册表、换传输）从来没被执行过。改成配方里每份钉死的引用各跑一对。
- **判据自我修正的第二处**：折续行那条控制原来只证明"不许漏红"，我要的其实是它另一个方向——
  `RUN pip install \` 换行才接 `-r reqs.txt` 是**合规**形状，不折叠就会误红。现在两对方向都在。
- **不补的那一项与理由**：`BASE_IMAGE_WHEELS` 是一张只认 `pip` 的白名单，理论上可以被人无声加宽。
  不为此开判据：加宽它必然伴随一个"镜像里真有那个锁外 wheel"的产物变化，那条变化已经被
  `--lock` 对账抓住；而为"集合恰好等于 {pip}"写断言只会得到一条天天要维护的空规则。
  这条判断写在这里，是因为它是一次"评审建议 ≠ 该做的动作"的裁决记录。
- 计数：supply-chain 档 20 → 21、docker 档 25 → 26，全套 543 → 545；OPERATIONS 补"本机别用
  `docker build --platform` 做跨架构复算"与两个判环境红的 tell；`scripts/image_cve.sh` 头部注释
  与 Makefile 目标注释都从"未分诊"改成分诊后的裁决。**另一手记账**：插入 N-24 之后我在
  表格里留下一个空行（会把一张表劈成两张、只有读回磁盘才看得见），是 `docs_row_order` 之外
  自己复算行号区间抓到的——写进记忆，别再靠"跑一遍没事"。

### 又推翻一条自己写的"外部阻塞"：NGC 凭据并不挡 workspace 镜像的字节

- 复算间隙顺手去量 `nvcr.io/nvidia/isaac-sim:6.0.1` 到底挡在哪一步，结果推翻了两句话：
  §8 第 1 项写的**"阻塞于 NGC 条款"**，和本轮早些时候补进去的**"凭据只在拉层字节时才要"**。
  一手读数（2026-09-27 本机 `curl`，全部不带任何 API key）：匿名 pull 令牌
  （`https://nvcr.io/token?service=nvcr.io&scope=repository:nvidia/isaac-sim:pull` 回 200，token 1198 字节）
  不只读得到 index（amd64 `b1c542b2…`／arm64 `2026973596…` 两个子清单）与 amd64 清单本身，
  **层字节也读得到**：config blob 整份无 Range 下载走 `307 → layers.nvcr.io` 签名地址 → `200`、
  实拿 **9910 字节**、`shasum -a 256` 与清单里的 `config.digest`（`2d4ebfef…`）**逐位相等**；
  第一层（压缩 29,724,688 字节）`Range: bytes=0-1048575` 退 `206`、实拿 **1,048,576 字节**。
- **同一句话我改宽了两次，第二次被自己的复测抓住**：先写"取字节不需要凭据"，再量才发现它把两件事混成一件——
  反向对照（同一 blob、**完全不带 `Authorization`**、即使跟随重定向）退的是 **`401`**。
  所以成立的说法是"**不需要 NGC 凭据（API key／登录）**，但**需要一枚匿名 pull 令牌**"；
  不成立的是"不需要任何认证"。规则进了记忆：用 range／部分读数去支撑"整步可行"这种主张之前，
  先把无 Range 的完整版本跑一遍，并且每一条正面前提都要把它的**无反面凭据极性**也量一次，
  否则"不需要凭据"与"不需要登录但需要令牌"在终端上完全同形。
- 真正挡住"把 workspace 镜像建出来、再回填 digest"的是**容量与验收口径**，两处都量了才敢写：
  colima 虚拟机根分区 `df` 只剩 **7.6 GiB**（86% 已用），而这个基础镜像光压缩层就是
  amd64 **9.96 GiB**／arm64 **8.78 GiB**（各 19 层，最大一层 9.85／8.67 GiB），解压还要再翻几倍；
  G2–G4 的运行验收另外要求 NVIDIA x86 主机。所以 §8 第 1 项与 BLOCKED 那一格都改成了
  "需要一台磁盘有数十 GiB 余量的构建机 ＋ 一台带 GPU 的 x86 主机做运行验收"，不再写"等 NGC 授权"。
- **一次更正只推平了两面，剩下的面按数字找是找不到的**：改完 §8 与 BLOCKED 之后按事实关键词
  （`NGC|nvcr.io`）跨全文重 grep，查出**七处**仍在复读被推翻的那句话，逐处改：
  `docs/SUPPLY_CHAIN.md:12`（"真实构建需 NGC 凭据"）、同文件 §8 末尾那行**原样留着被推翻句子**的
  闭合注记（"凭据只在拉层字节时才需要"）、`docs/GPU_HOST.md:10`、`scripts/release.sh:217`（发布报告
  的 BLOCKED 明细表）、`docs/ACCEPTANCE.md:19`（"环境没有 NVIDIA GPU 与 NGC 登录态"）、
  `docs/MASTER_PLAN.md:33`（遗留 BLOCKED 清单里列着"NGC 凭据"）、`DELIVERY.md:7`。
  七处都不是数字、没有任何门禁读它们，所以"跑一遍没事"完全不构成证据——只有按关键词 grep 才算普查。
  另修一处记账机械伤：`docs/CURRENT_STATE.md` 的 BLOCKED 段落里留着脚本拼字符串时的游离引号
  （`令牌"` / `"就能读`），是上一轮 Write 之后才看见的，本轮就地清掉。
- **更正之后又往前走了一步，而且这一步按老规矩自己重开了一手源**：派出去查"NGC 到底要不要登录"的
  检索交回一份带原话的报告，我没有直接引用，而是自己 `curl` 了那个页面（HTTP 200／**145,133 字节**）
  去标签后逐行读。Isaac Sim 6.0.1 容器安装页的拉取步骤就是裸的 `$ docker pull nvcr.io/nvidia/isaac-sim:6.0.1`，
  **拉取之前没有任何登录步骤**；整页唯一那句 "run docker login first" 讲的是 **Docker Hub** 的匿名拉取
  限速（429），与 nvcr.io 无关；许可是在**运行**时以 `-e "ACCEPT_EULA=Y"` 接受的
  （同页原文："By using the -e \"ACCEPT_EULA=Y\" flag, you accept the license agreement of the image…"）。
  **结论落到了文档面**：`docs/GPU_HOST.md` §2、`docs/RUNBOOK.md` §5、`DELIVERY.md` 三处的
  `docker login nvcr.io` 一步删掉，GPU_HOST 那一格换成带出处的说明；删之前先 `grep login scripts/`
  → **零命中**，所以那三行是纯 prose 步骤，删它不会机械断掉任何脚本行为。
  两条**未找到**也如实记在这里：nvcr.io 的匿名拉取限速／大小上限没有一手出处（NGC 私有仓库指南的
  "Single image layer size 10 GB / Total image size 1 TB" 是**发布与存储**侧配额，不是拉取侧），
  `Other` 这个占位用户名的官方说法同样没找到（`$oauthtoken` 有，见同指南）。
- **这次更正自己被自家门禁拦下一回**：往 `scripts/release.sh` 的 BLOCKED 明细表里写新句子时，
  我用了 `` `sha256` ``／`` `Range` `` 这样的代码块，而那张表整个在**未加引号的 heredoc** 里 ⇒
  反引号会被 bash 当命令替换执行。常驻用例 `test_release_script_heredocs_carry_no_backticks`
  当场把这轮 validate 判红：`overall=FAIL`、`543 passed / 1 skipped / 1 failed`、
  失败名 `tests.test_supply_chain::test_release_script_heredocs_carry_no_backticks`（`assert not [0]`）；
  改成裸词之后 supply-chain 档 21 支全过。这条门是上一轮为一次真事故建的（当时模板里的
  `make test-k8s-control-plane` 被真的执行了一遍、输出被抄进发布物），**它本轮第一次被我自己的改动
  触发，就是它该存在的证据**——也顺手记一条：文档面 ≠ 自由文本，落进 shell 模板的那些面要先过形状判据。
- **同一条改动又被第二条既有门拦下一次**：给 `docs/GPU_HOST.md` 写"为什么删掉 `docker login`"那段依据时，
  我把 NVIDIA 原文那句**只写到 tag、没有 digest** 的 `docker pull` 当引文抄进了运维手册。
  常驻判据 `test_pinned_base_ref_is_used_verbatim_by_every_consumer` 的消费侧清单里含 `docs/GPU_HOST.md`，
  于是它把这轮 validate 判红：`543 passed / 1 skipped / 1 failed`，offender 逐字打出
  `GPU_HOST.md: nvcr.io/nvidia/isaac-sim:6.0.1 != ['…@sha256:783444c7…']`。
  **修的是措辞不是判据**：运维面改成"描述而不复现裸 tag"（原文只写到 tag ⇒ 本仓一律用钉死的那一份），
  `docs/SUPPLY_CHAIN.md` 同一处一起改；只有本 CHANGELOG 保留那份原文当证据链，因为记录面不是操作入口。
  本轮两条既有门各开火一次（heredoc 反引号、逐字相等），这是它们不是装饰的直接读数。
- 这是同一个晚上**第二次**把"某条通道/某个前提不通"当成做不了（前一次是 ghcr 可达性，
  这一次是 NGC 凭据）。共同点很一致：**当时没有真去请求那一样东西**。因此把这条方法论再收紧一步：
  任何写成"阻塞于 X 凭据/条款"的句子，必须带上"我请求过 X 保护的那一步、并贴出返回码"的读数；
  没有返回码，就把它当成未验证前提，不许当阻塞理由。

### 撤掉一处自己写的过度声明，并给那条"接线"判据补上两支开火对照

- `tests/test_supply_chain.py::test_image_sbom_step_forwards_the_lock_to_the_criterion` 的
  文档字符串写着"形状按 AST 判（数关键字节点的实参位）"，而实现是一条正则扫 `image_sbom.sh`。
  这句在三点上都不成立：被读的是 bash（仓里没有它的语法树）、同一份文档里前一句还写着
  "AST 都比不出参数没转发"（自相矛盾）。按"只增不删会失效"的反面处理——**改措辞而不是改实现**：
  文本判在这条上是挣得来的（判据被绑在同一逻辑行内，只让续行反斜杠跨过换行），换成 AST 反而
  要先引入一个 bash 解析器。
- 原实现只有正向断言（"文件里有这个形状"），没有反证——按本仓标准那就是一条可以恒真的规则。
  补两支就地注入对照：摘掉 `--lock uv.lock`（保留判据名与其余参数）必须读成"没转发"；
  把旗标从续行挪成独立一行（真实 shell 语义里那是另一条命令）也必须读成"没转发"。
  两支都带 `mutated != text` 的落地断言，且变异靶点先数过 `count == 1`。
- 判据判别力实测（同一份 `image_sbom.sh`，两条正则并排）：
  真文本 命中／命中；摘旗标 无命中／无命中；挪出续行 **无命中／命中**——第三行正是第二支对照存在的理由：
  若哪天有人把判据放宽成"整份文件里两个 token 都出现过"，只有它拦得住。supply-chain 档 21 支全过。

## 0.6.0 — 2026-09-26（把"没执行过的后端"逐个跑起来）

`docs/VALIDATION.json`（`make validate` 生成）：collected 462 / passed 461 /
skipped 1 / failed 0（唯一 skip 是 `k8s_integration`，需 NVIDIA Device Plugin）；
lint/typecheck/migration/build 全 PASS；六个集成档中五档 **PASS**：
postgres 18/18、docker 20/20、browser 11/11、**object_store 20/20**、
**k8s_control_plane 7/7**。overall = `PASS_WITH_PHYSICAL_PENDING`。
文档里这串计数由常驻判据与产物对账：值比较在 `make validate` 汇总之前做
（pytest 阶段读到的必然是上一次报告），pytest 侧只钉"恰好一处 + 判据没被搬走"，
另配一支"喂错数字必须两处点名"的开火对照。

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
- **发布链自身的两处修正**（都是本轮踩出来的）：
  - 生成 `dist/VALIDATION_STATUS.md` 的未加引号 heredoc 里，我在本轮新写的表格行用了反引号
    ⇒ bash 把它当命令替换**执行**掉：`make test-k8s-control-plane` 在发布过程中又跑了一遍，
    其收尾行（`7 passed, 451 deselected ... in 64.66s`）被抄进发布工件，另一处
    （`nvidia.com/gpu`）替换为空串留下残句，而脚本全程退出码 0、`bash -n` 全绿。
    现在：模板文本不再用反引号；写完强制检查产物（含反引号或测试输出即 `release FAILED`）；
    并新增常驻静态判据（抽出 release.sh 所有未加引号 heredoc 正文，断言无反引号，
    且"解析到 0 块"也算红）。对照臂：把改前那一版 `release.sh` 喂进同一条判据 ⇒ 命中 1 块。
  - 原来的"版本一致就复用旧 `docs/VALIDATION.json`"在几分钟内就暴露了：发布链跑完后
    新加了一条判据用例，版本号不变 ⇒ 报告会比工作树少一条（正是 CI freshness 门禁要抓的
    形状）。现在 `make release` 无条件重跑 `make validate`。

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

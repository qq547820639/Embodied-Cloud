# Changelog

## 0.7.0 — 2026-09-26（Sim2Real 从"控制面替设备走状态机"变成真设备通路）

`docs/VALIDATION.json`（`make validate` 生成）：collected 1017 / failed 0。这份提交面现在**只放换机器重跑逐字节相同**的门禁；"本次跑跳过哪几支、各集成档是 PASS 还是 PENDING"属环境读数，改落 `dist/VALIDATION_RUN.{json,md}`（gitignored）——理由与判据见下方"计数面按可复现性分档"一节。
overall = `PASS_WITH_PHYSICAL_PENDING`（物理待验仍是 GPU 真机 / Isaac 流媒体面 / 真机器人）。

### 计数面按"可复现 / 环境读数"分档，skip 从数字改成闭集（N-31 闭合）
- **缺陷不是"文档要常改"，是提交物在替环境说话**：`docs/VALIDATION.json` 进版本库，
  CI 用 `make validate && git diff --exit-code docs/VALIDATION.*` 判新鲜 —— 这要求报告内容
  是仓库代码的函数。实测不成立：同一棵树跑两次，一次 `skipped=1`、一次 `skipped=2`
  （docker 档那条钉引用可取性的判据按三态分流，通道抖动时自判 PENDING），
  `test_run.passed/skipped`、`integration_docker.status` 连 note 里的"执行 25/26"一起漂。
- **拆法**：可复现门禁（collected／failed／lint／typecheck／migration／build／两份文档面判据）
  留在 `docs/VALIDATION.json`；环境读数（六个 `integration_*` 及其 note、passed/skipped、
  按用例名列出的 skip、闭集判决）改落 `dist/VALIDATION_RUN.{json,md}`（gitignored）。
  CI 的"Docker-backed tiers really ran"与 `scripts/release.sh` 的档位表随之改读后者。
- **skip 不再是数字**：合法 skip = 所属模块在闭集内 + 文案含该模块哨兵；闭集由
  `conditional_skip_universe()` 从用例源码 AST 现取（六个集成档模块；第二来源 `EXTRA_SKIP_UNIVERSE` 当时还含 `test_gpu_pool_guard`，现已由 N-41 清空），
  名单外一律 `unexpected_skips=FAIL`。文档面随之只写 `collected N / failed M`，
  并把 `passed\s+\d` / `skipped\s+\d` 判为越界写法（`face_offenders`）——
  环境翻一次不影响面，代码真变才红。遮罩自身也带判据：声明的环境字段必须真在报告里、
  遮完至少剩 4 项门禁，否则 `report_split=FAIL`（防"遮罩把报告遮没"这种假合规）。
- **顺手修掉两条让门禁不可信的旧账**：① CI 原先 `pip install -e '.[dev]'`，装的 ruff/mypy/pytest
  版本由 PyPI 当日解析决定 —— 那"lint=PASS"写进提交面就不是代码的函数；改成
  `uv sync --frozen --all-extras`。② 顺序：`make sbom` 第三行用 `.venv/bin/python`，
  而 `.venv` 原先要到那之后才存在，干净 runner 上必然先失败（读码证实，未在 runner 上验过：
  本仓未推送）。装环境一律排第一。
- **门禁抓到我自己两次**：新增测试文件留下一句没用了的 `import pytest` 让 `lint=FAIL`；
  而 `test_release_script_owns_the_value_reconciliation` 那条旧判据锚在 `software_failed = any(` 上，
  汇总变量一改名它直接 `ValueError` —— 已把锚点换成报告字面量（判据该跟着语义走，不该跟着变量名走）。
- 新常驻判据 9 支（`tests/test_validation_matrix.py` 8 + 计数面反证 1），门禁目录新开 G0.42／G0.43。

### 构建后端上锁、产物时间上 HEAD 提交（同一条"可复现"往下推一层）
- **量出来的缺口**：`[build-system].requires = ["setuptools>=75"]`，而 `uv.lock` 里**根本没有 setuptools**
  （锁造的 venv 里 `importlib.util.find_spec("setuptools")` 读回 `ABSENT`）⇒ `python -m build` 的默认
  隔离环境只能每次向 PyPI 现解析 `>=75`。于是提交面里 `build=PASS` 与 lint/typecheck 同性质：
  它记录的是"当天 PyPI 有什么"，不是仓库代码。**注意别把它读成"今天已经不一致"**：
  当天两条路径的 `Generator: setuptools (84.0.0)` 是同一个数，缺陷是"无法证明可复现"，不是"已经漂"。
- **改法**：`setuptools>=75`（与 `build-system.requires` 同名同区间，由常驻判据对账）进 `dev` extra →
  `uv lock` 锁到 84.0.0；`make build` 与 validate 的构建步统一改 `python -m build --no-isolation`。
  实证换装法不改工件：同一份树各跑一次，两个 wheel 解包后 `diff -r` **零差异**（文件清单与大小也逐项相等），
  差异只在容器的 zip 头。判据要判"锁住的版本满不满足那条区间"，这件事交给 `packaging`（PEP 440 的规范实现），
  不自研比较器，故 `packaging` 也一并进 `dev` extra（直接 import 就得直接声明）。
- **产物时间**：`make build` 连跑两次，改前 wheel sha 不同、改后同为 `2992a47ac5…` ——
  `SOURCE_DATE_EPOCH` 由 Makefile 缺省成 HEAD 提交时间并 `export`，validate 直接跑时用同一条 git 口径兜底。
  **sdist 仍不可复算**：`f0dad9e2…` 对 `90e84be2…`。逐字节定位到 tar 头：顶层目录、`PKG-INFO` 与各子目录的
  `mtime` 记的是打包那一秒（pax 记录里还带小数），setuptools 84 没有把它们夹到 `SOURCE_DATE_EPOCH`；
  解包后内容零差异，所以 `dist/checksums.txt` 里 sdist 那行只能当"某一次构建的记录"。登记为 N-34 未闭的一半。

### 预热池的容量闸门补上「跨遍」这一半（N-35 闭合）
- **闸门只算了一遍的账**：`maintain()` 用一份共享的 AVAILABLE 显存多重集逐格预扣（N-29），
  但补位是异步入队的 —— 格开了，`gpu_id` 要等 worker 跑完 PROVISION 才写上、卡才离开 AVAILABLE。
  两遍 maintain 落在「入队之后、执行之前」这段窗口里时，第二遍看到的空闲集合与第一遍逐位相同，
  于是 1 张卡 / 2 个模板 / size=1 会开出 **2 格**（常驻用例的原文读数）。`missing` 那侧一直是对的
  （PREWARMING 计入 `ready+prewarming+legacy`），漏的是容量侧 —— 同一条不变量的两根轴只钉了一根。
- **改法**：进入补位循环前，先给每个「还会去占卡、但卡还没占上」的池位（与 `missing` 同一批池位谓词
  ∧ `gpu_id IS NULL`）按 best_fit 扣一张卡；扣完仍有余量才开新格。
- **两头都钉**：`test_in_flight_slot_holds_its_card_across_passes`（改前红：2 格 / 1 张卡）与
  `test_a_card_released_between_passes_still_gets_filled`（两遍之间真多出一张空闲卡 ⇒ 第二遍必须照开，
  防「只要池里有在飞的格就永远不开格」那种假合规）。
- **一条如实的边界**：`make warm-capacity` 的两遍之间有 drain（worker 先跑完才开第二遍），
  所以取证台读不出这个形状；这轮的证据只来自常驻用例。门禁目录新开 G0.45。

### 产物 sha 从装饰变成有牙的主张（N-36）
- **缺口**：`dist/checksums.txt` 记 wheel / sdist 的 SHA-256，但没有任何东西核过"第三方能不能重建出同一份字节"。
  N-34 把时间钉住之后，wheel 已可复算、sdist 仍漂 —— 如果只把这句话写在文档里，它就还是主张。
- **做法**：`scripts/artifact_reproducibility.py`（`make verify-artifacts`）每类产物各建两次（每次一份新目录，
  避免读到上一次的残留），sha 全等才写 `recomputable=yes`；`check()` 把"清单声明 ↔ 本轮实测"双向对账：
  把不可复算的说成可复算＝假承诺，上游修好后清单还写着 no＝过期悲观，两种偏离都红。
  排在 `dist/checksums.txt` **之前**执行，主张先被核过再被发布。
- **一手读数**（同一棵树，`SOURCE_DATE_EPOCH=1790482819`）：`wheel 4259babd17 == 4259babd17`、
  `sdist 41dda386f2 != 174db2b484` ⇒ 清单 `wheel=yes / sdist=no`，探针退出码 0。
- **换不换后端？量过再答**：同一口径下 `uv build --no-build-isolation`（setuptools 同后端）sdist 仍漂
  （`70f43f10…` vs `8fd483bb…`）；hatchling 也漂（`cdd04043…` vs `d136bc11…`）；
  **flit_core 两者都定**（tar `0dd4912e…` 两次相同）——但它只认"与 `project.name` 同名的单个模块/包"，
  本仓发行的是 `app*` + `edge_agent*` 两个顶层包，不是一处改名能迁的模型 ⇒ 不换后端，改成让主张可被推翻。
- **时间口径收成一份**：`git log -1 --format=%ct` 原先在 Makefile 与 validate 各写一遍，靠文本判据兜着
  （把重复当事实用）；现在抽到 `scripts/build_env.py::epoch_env`，常驻判据钉"`%ct` 在 `scripts/` 下只出现一次"
  ＋"Makefile 那条 shell 与它逐字相同"。语义照写清：**时间基准取 HEAD 提交时间 ⇒ 同一个 commit 可复算，
  换 commit（哪怕只改文档）sha 会变**。
- **探针第一次真跑就抓到我自己的 bug**：清单按文件名索引、探针按类别聚合，两个键空间直接对账，
  每轮报"主张缺席 + 清单里有、本轮没测"；补了显式的 `by_filename()` 翻译并给它配反证
  （交叉对账必然报 4 条）。新常驻判据 8 支，门禁目录 G0.46。

### 两张取证台补上常驻读者，顺带把 N-35 的形状搬到台面上（N-37）
- **动机**：`make warm-sla`（N-28 的读数出处）与 `make warm-capacity`（N-29 的读数出处）被文档与登记表反复引用，
  但**没有任何常驻用例跑过它们**。文档引用一个坏掉的台子，比没有台子更糟。
- **补读者当场撞出两个真缺陷**：
  ① 直接 `python scripts/warm_pool_capacity_lab.py` 就 `ModuleNotFoundError: No module named 'tests'`
  ——只有 `make warm-capacity` 的 `-m scripts.…` 形状活着；台子自己把仓库根挂上 `sys.path`，
  判据两种形状都跑；
  ② SLA 台把"这次交付走的是 claim 而不是新建""服务端直方图有没有留下样本""READY 少了一格没有"
  三条读数**只 print 不记 rc**，量的根本不是 claim 也照样退 0 —— 现在折进 rc，
  并且配一支必开对照 `WARM_SLA_POOL_SIZE=0 ⇒ overall=FAIL 且退出码非 0`。
- **N-35 的第二处读数**：容量台新增"两遍之间不排空 worker"的形状（`drain_between=False`），
  跨遍窗口第一次在台子上可见。摘掉跨遍预留实测 **建过 14 行 / 舰队 8 张卡**，装上 8 行 / 8 卡；
  两种形状都要求 `provision_failed == 0`、`warm_tombstones == 0`，生产默认 size=1 在 drained 形状
  必须 `ready == requested_slots`（闸门不许误伤）。这条把 N-35 行里"取证台读不出这个形状"那句更正掉了。
- 常驻判据 4 支（`tests/test_warm_pool_labs.py`），门禁目录 G0.47；
  `pyproject.toml` 给容量台加了一条写明理由的 `E402` 豁免（它的 sys.path 引导必须早于 import）。

### 状态页只留能对上账的数（N-38）
- **N-31 少做了一半**：它把环境读数从**报告**逐出去，但 `docs/CURRENT_STATE.md` §1 那张表
  还在手抄各档"执行了几支"。本轮量到两处已经抄过期：`Integration Docker **PASS 25/25**`
  对报告 `26/26`（docker 档中途加过用例，没人回头改那行），`mypy 45 files` 对实测 46。
  这张表抬头写着"本次真实验证（实测，非复制旧文档）"，没有读者的时候这句话会悄悄变假。
- **两类数两条路**：
  - *代码决定的*纳入对账 —— 提交面新增 `checks.typecheck.files`（mypy 收口行解析，
    `Success: no issues found in N source files` 与 `Found X errors ... (checked N source files)`
    两种形状都读，读不到返回 `None` 并按"事实源缺位"判红 —— 不许拿 0 当兜底，0 会让比较退化成永远对不上）
    与 `checks.migration.chain`（`alembic/versions` 的迁移文件数，本轮实测 14）；
    新门禁 `docs_state_rows` 用 `state_row_offenders()` 把 `Lint / Type`、`Migration` 两行逐位对上。
  - *环境决定的*逐出 —— §1 五行 `Integration …` 的 `n/m` 全删，明细指向 `dist/VALIDATION_RUN.md`；
    判据同时禁止这些行再出现 `n/m` 形状，抄回来就红。
- **五种偏离各有一支点名**：数过期、形状读不到、被盯的行整行消失（覆盖面缩小）、一行都没有（恒真）、
  手抄环境数。常驻判据 4 支，门禁目录 G0.48。
- 顺带修掉一处自己造成的语法断裂：给 `validate_release.py` 插函数时把
  `def doc_row_order_discrepancies()` 的换行吃掉，`py_compile` 当场拒绝 —— 记账脚本改仓库代码时
  "改完立刻编译/加载一次"这一步不能省（本轮第三次被自家工具抓到，前两数是 lint 与探针）。

### 启动指标分族、容量闸门改读分配器那一列（N-39／N-40）
- **两件事同源：池的闸门和运维的告警都在读一个"给人看的数"**。`Template` 上有两列显存 ——
  `gpu_requirement_gb`（挑卡用）与 `recommended_vram_gb`（UI 卡片显示），池的补位闸门与
  在飞预留都读了后者；而面向用户的启动指标也不区分"用户按下的启动"与"池自己开的格"。
- **一手读数**：1 张卡、size=1，交互请求先抢走卡、再让 worker 跑池内那一格 ——
  `workspace_launch_total` 增量 **+2**，池内失败还落在 `workspace_launch_failed_total`；
  而 `docs/OPERATIONS.md` 对这条指标的告警口径是"增量 >0 持续 10min"。
  闸门侧：真需求 24 / 展示 8 / 一张 8 GiB 卡时改前 `created: 1`，即开出一格必然 `No GPU available` 的空转。
- **改法**：指标按 `warm_pool_state` 分族（`warm_pool_prewarm_total/_failed_total/_seconds`，
  判据两头都断言）；闸门两处改读 `gpu_requirement_gb`（正反两支 + seed 落库行两列相等 + 消费方 AST 判据）。
- **`/metrics` 清单机器可查了**：`docs/ARCHITECTURE.md` §8 从缩写式（`workspace_launch_total/failed/seconds`）
  改成逐个全名，新判据拿 `app/metrics.py` 的 AST 声明逐项对账 —— 一上线就抓到 11 个族没写进文档。
- 自纠两处：AST 判据第一版把函数名写成 `node.func.name`（`ast.Name` 只有 `.id`）当场 `AttributeError`；
  "文件里不许出现这个列名"的文本判据被我自己解释它的注释挡了，改成走 AST 属性集合。

### 闭集的第二个来源清零：条件跳过改成断言（N-41，闭合 N-33）
- 上一轮我给 `test_gpu_pool_guard` 的两支补哨兵、挂进 skip 闭集，并在注释里写下"代价是这两支可能静默不跑"。
  这句话本身就是缺陷描述：它们量的是"共享池被占干后回收守卫能不能救回来"，
  跑不跑取决于跑序 —— 等于给一条夹具卫生的 P0 判据留了免检口。
- 现在 `rig` 显式达成前置（余量 <2 就先 `reclaim_gpus`，再断言 `>= 2`；真达不成就是有卡放不掉 ⇒ 红），
  两支用例的 `pytest.skip` 换成断言；哨兵常量删除，`EXTRA_SKIP_UNIVERSE` 归空，机制与登记要求留着。
- 判据两支（闭集不许再含该模块 + 该文件 AST 里不许再有 `.skip(`／`GATE_SENTINEL`），门禁目录 G0.51；
  `G0.42` 那行对闭集组成的描述同步更正。
- 顺手：`rig` 的 `cards` 断言与新增前置一起跑，`tests/test_gpu_pool_guard.py` 现在 4 支全跑、0 跳过。

### 指标文档从"族名"核到"标签维度 + 告警引用"（N-42）
- N-39 那一步把 §8 的缩写式清单改成逐个全名并加了双向对账，但核的只是名字集合：标签维度
  （`workspace_launch_total{template_id,provider}` 的 `provider`）改了文档不会红，
  `docs/OPERATIONS.md` 告警表里写的指标名同样没人核 —— 告警句子可以指着一条不存在的序列。
- §8 改成「族名 + 标签 + 用途」三列表格（17 族全列），判据从 `app/metrics.py` 的 AST 取
  `{族: 标签}` 做三向对账（多写、漏写、标签不符各点名）；另一支只核运维文档**表格首列**里的
  snake_case 名字必须是已声明族。
- 判据过宽一次，当场收到反例：全文扫 `*_total/*_seconds/*_ready` 被 provider 的方法名
  `wait_ready` 打红 ⇒ 收紧作用域到"表格首列"，保住牙齿（把 `gpu_allocated` 写成 `gpu_allocations` 仍红）
  又不禁正文提方法名。两支都配了必开对照 + 恢复后复算（`rc=0`）。


### 指标三边对齐：声明 ↔ 文档 ↔ 实际暴露（N-43）
- N-42 只把"文档"和"声明"两边核过；`/metrics` 真跑出来的是什么，没人看。这类缺口在命名规则上最容易咬人：
  `warm_pool_claim_failed` 声明时不带 `_total`，prometheus 会把它派生成 `warm_pool_claim_failed_total` ——
  文档与代码互相点头，运行时却是第三套。
- 新增测试侧共享解析 `tests/metrics_spec.py`：AST 取声明、按**实测得到**的派生规则算序列名、解析 exposition 文本；
  端到端判据真跑一次启动后抓 `/metrics` 对账（四条：解析非空／序列可派生／标签 ⊆ 声明＋`le` 例外／刚发生的启动确实带标签）。
- 派生规则不靠记忆：`test_allowed_series_matches_the_librarys_own_naming` 现场建三类指标 `collect()` 后比对
  （本机 prometheus_client 0.26.0：Counter→`{基名_total, 基名_created}`，Histogram→`_bucket/_count/_sum/_created`）。
- 两次注入 + 一次反证：`app/main.py` 里注册 `gpu_stray_total` ⇒ `暴露了声明之外的序列：['gpu_stray_created','gpu_stray_total']`；
  从声明里删掉 `provider` ⇒ 进程在记账处炸、启动收敛不到终态（活进程里标签越界走不到那条分支，于是它的开火证明移到解析层）；
  已知样本喂解析 ⇒ 抓出"不校验数值列，把一句散文当成叫 `this` 的族"。判据 4 支、门禁 G0.53。


### `/metrics` 的划界改成注册表说了算（N-44，闭合上一轮的"未证实"）
- 上一轮我把"②③ 只覆盖前缀来自声明的序列"写进未证实清单：为了躲开 prometheus_client 自带的
  `python_gc_*`/`python_info`，判据按前缀划界，于是**全新前缀**的族（在 `app/metrics.py` 之外注册）能从缝里走过去。
- 改成问注册表：`REGISTRY._collector_to_names` 里 `isinstance(collector, MetricWrapperBase)` 的才是"我们注册的"，
  库自带的平台收集器不是（本机 0.26.0 实测分类：`python_gc_*`/`python_info` 归库，其余 51 个名字归我们）。
  内部结构一旦改名，`registered_names()` 显式抛错 —— 判据不许静默退回猜测。
- 两条判据一起收紧（注册表层 + 暴露层），并补一支"库自带族的识别方式若变了就红"的自检。
- 开火对照（真注入，不是推理）：在 `app/main.py` 建 `edge_heartbeats_total` 并打一次点 ⇒
  `这些序列不是由 app/metrics.py 声明的：['edge_heartbeats', 'edge_heartbeats_created', …]` 与
  `暴露了声明之外的序列：['edge_heartbeats_created', 'edge_heartbeats_total']`；撤掉注入后 `rc=0`，
  `git diff app/main.py` 为空。
- 本轮另有两处自伤被抓：`allowed_series(declared) | set(declared)`（dict 不能 | set，`rc=1` 当场报）、
  注册表把 Counter 的**基名**也算进已注册名字，`allowed` 必须含基名（用 `declared_series()` 一份实现包办）。


### 取证台的读数可以被另一条查法复算（N-45）
- N-37 给两张台子配了读者，但读者只证明"台子会跑、rc 会翻"，没证明"表里的数换条路查还是它"。
  现在每行读数带一份 `recount_offenders`：`gpu_side_counts()` 从 Gpu 侧独立数
  （`status` 计数、`Gpu.workspace_id` 联结 `Workspace.warm_pool_state` 的去重持卡数、
  以及"AVAILABLE 却还挂着 workspace"这种释放不干净），`recount_discrepancies()` 纯比较器
  在分母 ≤0 时拒绝判干净、积压形状关掉 READY 那条不误开火；两种输出模式下偏离都退 1。
- 三层判据：比较器两极（改一个数就点名）、真台子两形状复算为空、**传感器自检**
  （造一张 AVAILABLE 却挂 workspace 的卡，`available_but_bound` 必须 =1）——
  只验比较器证明不了它读的是另一张表。
- N-44 那条"未证实"顺手关掉：注册表结构变更时的报错现在打印被测对象的非 dunder 属性与
  `prometheus-client` 版本，并用假注册表把这条路径本身测了一遍。
- 一次自家事故被读者还债：给 `main()` 加退出码时把 `if __name__ == "__main__"` 整块吃掉
  （脚本静默退 0、什么都不打印）——N-37 那支"stdout 必须有表头"的读者判据当场抓住。
  记账脚本改仓库代码后必须立刻编译/真跑一次，这条纪律今天第四次生效。


### daemon 卡住与拒连走同一条干净跳过路径（N-46）
- 触发点是复算自己报的：HEAD `4b5aced` 的干净树复算里 `docker version` 超时 20 秒，docker 档 26 条用例
  全部 `failed on setup with "subprocess.TimeoutExpired"`（`validate_rc=2`、`freshness_rc=1`），
  而**同一棵树在主树里是绿的**——这类不对称只有换环境复算才看得见，也正是复算存在的理由。
- 只补我撞到的那一个文件是不够的：`pg_server`／`s3_server`／`k8s_server`／`docker provider` 四份
  `gate_reason()` 都是同一个形状（只看 `returncode != 0`，异常照穿）。抽成共用
  `tests/docker_probe.py`（`TimeoutExpired`→rc 124 + `命令超时（20s）`，`OSError`→rc 127），
  四档的 skip 文案顺带带上 docker 侧原因。
- 判据按类别写：`tests/test_docker_gate_hardening.py` 四个模块 × 两个极性（卡住⇒给原因不抛、
  健康⇒不许把"超时"写死），加 docker 档 `_usable_image()` 那一路的一支；共 9 支。
- 真实复跑四档：`rc=0`、75 条通过（daemon 现在 13 ms 应答，健康路径的判定没被改宽）。
- 记一笔纪律账：`N-45` 那次读者判据抓到了我自己吃掉的 `if __name__ == "__main__"`；这次是跨环境复算
  抓到了只在另一台"机器"上才会出现的失败。两道都是上一轮建的，本轮还了债。


### 就绪等待与探测：任何失败模式都收敛成原因，不是异常（N-47）
- N-46 按"四个模块的 `gate_reason()`"扫了一遍就收工，这轮复核发现**同类还有两处**，而且参数化判据看不见它们：
  `tests/live_server.py` 的就绪循环只吞 `OSError`（httpx 的超时是 `TransportError`，会穿出去，
  把浏览器/边缘两档变成 setup 错误、丢掉服务日志），以及 `tests/k8s_server.py:node_image_cached()`
  仍直接用 `_docker(timeout=30)`（`gate_reason` 更早 return，参数化那支永远走不到它）。
- 做法是把"探测"变成两个不抛异常的共享函数：`health_reason(url, client=…)`（""＝就绪，否则一句原因）与
  `wait_until_ready(probe, alive, deadline_seconds, sleep, log_tail, exit_code)`（失败一律收敛成
  `(False, 带日志的原因)`，连探测实现自己抛也兜住），`live_server()` 改成薄调用方。
- 判据 6 支，含一支**接线**判据：夹具源码里必须真的调用这两个函数——不然判据护的是没人走的实现。
  另有三态探测（超时／503／200）、进程先退报 `rc=`、k8s 在挂起 daemon 下答"没缓存"。
- 真实复跑两档（真起 uvicorn 子进程）：`rc=0`，13 条通过；`ruff check tests` 干净。
- 教训写进记忆：修"一类"不能按文件个数扫，要按**失败模式**扫（拒连／超时／非 OSError 的传输异常／SDK 自抛）。


### PENDING 要说清缺什么，并用真挂起验证过（N-48）
- 档位"干净跳过"的格式在 N-31/N-41 就管住了（闭集 + 哨兵 + 明细落到本次跑读数），但**内容**没人管：
  原因可以是空的、可以只把哨兵再抄一遍，跳过照样绿。
- 新增 `pending_reason_offenders()` 与门禁 `pending_reasons`：四种偏离各自点名（无"原因："段／原因为空／
  原因＝哨兵本身／原因不含任何可行动标记）。它留在**提交面**一侧——原因文本是代码里的 skip 文案常量，
  结论不随环境变；健康时没有 PENDING 档＝无可核＝PASS，所以 `make validate` 在任何机器上都同判。
- 顺手把 N-46／N-47 那条"未证实"做掉：**真挂起**而不是注入异常——`DOCKER_HOST=tcp://192.0.2.1:2375`
  （TEST-NET-1 黑洞地址，丢包≠拒连）下四档复跑：`test_docker_provider_integration rc=0 wall=20s`（27 skip）、
  `test_postgres_concurrency rc=0 wall=21s`（19 skip）、`test_s3_artifact_store rc=0 wall=20s`
  （15 skip + 5 条不依赖 daemon 的单测照跑）、`test_k8s_control_plane rc=0 wall=20s`；
  三档 `gate_reason()` 统一给出 `docker daemon 不可达（命令超时（20s））`，三次前置合计 60.0s
  ⇒ 每档的挂起代价被单次探测封顶。这份真实原因喂进新判据 ⇒ `[]`。
- 判据 2 支（五种夹具极性 + 接线次序），门禁目录 G0.58，计数面 622→624。


### 挂起代价从估算变成测量，并做成可复跑的取证台（N-49）
- N-48 留了一句估算："版本探测通、后续探测挂"时 docker 档大约付 80 秒。估算写进文档就是没人核的主张，
  而且这个形状不可复跑。现在有了 `scripts/hang_probe.py` / `make hang-probe`（`TIMEOUT=2` 走快档）。
- 造法是真挂：黑洞模式把 `DOCKER_HOST` 指向 TEST-NET-1（丢包≠拒连）；`hang-later` 在 PATH 前面放一个假
  `docker`——`version`/`info` 正常答，其余子命令 `sleep`，于是每一层探测都被自己的超时掐掉。
  每档跑在独立子进程里（互不污染环境），台子自带判据：给不出原因或原因不可行动 ⇒ 退出码非 0。
- 实测（真实 20s 超时，整轮 245s，`hang_probe_rc=0`，无一条"判据未过"）：
  blackhole 每档 20.06–20.53s；hang-later：docker **83.49s**、k8s 控制面 **40.07s**、postgres 20.23s、
  object-store 20.05s。我的估算 80s 方向对但把 docker 档的候选镜像遍历算少了。
- 常驻判据 4 支（两模式各一支 + 纯函数 rc 可翻 + Makefile 接线），门禁 G0.59；
  `docs/OPERATIONS.md` 新增 §7.3 把这张表钉成运维口径。
- 写判据过程中被 ruff 抓两处、被自己的测试抓两处：`pending_reason_offenders` 返回的是字符串而我把
  它当元组解包（`ValueError: too many values to unpack`）、`.PHONY` 多行续行的扫描写成了
  `(a and b) in line` 的胡话（`TypeError`），两处都由判据自己报出来后才修。


### 操作指针核到"引用的东西真的存在"（N-50）
- N-48 让原因必须"可行动"，但核的是关键词表：一句写着 `export …K8S_TESTS=1` 的话只要含"需要"就过关。
  而这个名字**全仓零命中**——它是我在写上一条判据前"记得"的假指针（这里刻意不写全名：被扫文件里出现假名字，就会被这条判据自己抓住）。人会记错这类名字，文档就需要一道不依赖记性的核对。
- 新增 `reference_catalog()`：env 目录 = `env_prefix` + `app/config.py` 的 Settings 字段名、`os.environ`/`getenv`
  的字面量读点名、以及 `app`+`edge_agent` 源码里出现的 `EMBODIEDCLOUD_*` 字符串常量（覆盖
  `ENV_SERVER = "…"` 这种间接读法）；make 目录 = `Makefile` 目标；extra 目录 = `pyproject` 的 optional extras。
  被扫文本 = 23 份文档 + 6 个档位前置模块；新门禁 `reason_references` 进提交面（目录全来自仓内事实，不依赖环境）。
- 两类恒真形状也算偏离：目录某侧为空、一处指针都没扫到。判据 3 支；当前真面复扫 **0 条越界**
  （目录规模：51 个候选变量名、40 个 make 目标、extras 含 `postgres/s3/dev/app`）。
  它上线后抓到的第一条就是我自己写的占位符（门禁行里 `make` 后接 xxx、点方括号里写 extra 那种占位写法，被点名成失效指针）——
  处理是改写占位符为 `make <目标>` 这类写法，不给判据开豁免。
  第二次抓到的是它自己：`docs/VALIDATION.md` 会把上一轮的 FAIL 原因抄回来，同一批假指针被反复「发现」——生成物从扫描集里剔除（`GENERATED_DOCS`），并有 `test_generated_reports_are_not_scanned_for_pointers` 钉住。
- 记账时又踩自己写的坑两次：拼接脚本把 `# PENDING 的说明…` 那行注释切成半句（`SyntaxError: invalid character '：'`）、
  以及 `root/"app".rglob(...)` 的优先级把 `rglob` 挂到了字符串上（`AttributeError`）——两处都由"改完立刻编译/真跑"抓到。
- 登记表 N-50、门禁 G0.60，计数面 628→631。


### 指针的出处改成"真的被读"（N-51）
- N-50 的 env 目录里有一条宽口径：只要 `EMBODIEDCLOUD_*` 以字符串形式出现在发行代码里就算出处。
  结果注释、文档字符串，甚至写错一个字母的名字都能给假指针盖章。
- 第一个受害者是今天的我：我在门禁注释与测试文档里写下 k8s 开关的**复数形式**，而仓里真正被读的是单数——
  用 AST 扫全部读点后确认复数形式零处被读，也就是说，若只靠旧目录，这条我自己造的假指针会永远合法。
- 收紧：`env_names_read_by()` 用 AST 只认 `os.environ[...]`、`os.environ.get(...)`、`getenv(...)` 三种读法，
  并解析 `CONST = "名字"` 之后按变量索引的间接写法；注释不算。
- 判据写成双向，避免"把判据调瞎当成调准"：喂一份混合三种情形的源码，目录只收两种；
  同时断言对象存储镜像、kind 二进制、边缘凭据这些**真被读**的开关收紧后仍在目录里。
- 收紧后的真面复扫：52 个变量名、40 个 make 目标、11 个 extras，越界引用 **0 条**；
  我把注释与测试里那处假名字改回正确拼法，测试夹具改用明显不存在的占位名（不复用真开关的近似拼法）。
  门禁 G0.61，计数面 632→633。


### 挂起取证分出"谁在等"，原因必须落成动作（N-52）
- `hang-later` 剧本是"整层全挂"的极端假设，回答不了"哪一层探测真的暴露在挂起风险里"。
  新加 `half-hang`：假 docker 对 `image inspect` 快答"没有"、对 `image ls` 挂住。三模式同轮实测
  （`--timeout 20`，偏离表全空）：
  - blackhole：四档 20.05–20.40s（版本探测先挂 ⇒ 每档只付一次）
  - hang-later：docker 80.90s、k8s 控制面 40.05s、pg 20.31s、s3 20.04s
  - half-hang：只有 k8s 控制面付 20.06s，其余 0.05–0.85s ⇒ **只有那条兜底 `docker image ls` 会等**
- 常量间接读的解析从一跳改成带环检测的传递闭包（`A = B = "名字"` 认；`A = B`/`B = A` 不炸栈也不造出处），
  判据双向，免得把判据调瞎当成调准。
- "可行动"扩成两种成立方式：说出缺什么（关键词表）**或**给出能跑的动作（`docker pull`／`pip install`／
  `export 变量`／`make 目标`）。这条改动立刻反过来说服我改文案：docker 档的原因只描述了现状
  （"没有本地缓存…试过：…"），现在补成"先 docker pull 其中任一，或用 EMBODIEDCLOUD_DOCKER_TEST_IMAGE
  指向本地已有的镜像"——里面的变量名又由 N-51 的出处判据核对，形成闭环。
- 判据新增 2 支（半挂隔离 + 传递解析），`docs/OPERATIONS.md` §7.3 换成这一轮的三模式读数；
  门禁 G0.62，计数面 633→635。
- 记账脚本又自伤一次：把新函数插进循环体内导致 `B023`（闭包捕住循环变量），
  改法是把 `resolve()` 提到循环外、把两个字典当参数传——lint 这次替我发现了结构问题，不只是风格问题。

### 文档硬指针（行号与章节）现在有人核了（N-53）
- 先普查再立门：23 份文档里 58 处 `文件:行号`、4 处 `宿主.md §节`，其中一处真断——
  `docs/code-walkthrough-2026-08-13.md:417` 指向状态文档当时的第 10 节，而那份文档后来重组为 §1–§5。
- 四处 prose 先修好：悬空章节改指现存的 §4；三处"把上游仓库文件写成本仓路径形状"的写法
  （trivy 的入门安装文档、其 SBOM 页的行号，以及把两个产物名缩写成同一后缀的写法）改成不会让人误以为本仓有该文件的写法。
- 新门禁 `doc_references`：纯函数逐条核（行不越界、宿主在、章节在），两类恒真形状也算偏离，
  分母阈值（行号指针 ≥50、章节指针 ≥3）进判据防"分母缩水后空转"。
- 有意划的边界：自由路径的存在性不进门禁——普查显示未解析的 5 条全是示例路径或 GPU 主机上的外部脚本
  （如那份兼容性检查脚本），为它们开豁免只会让判据变成例外清单。这条边界写进函数 docstring，
  并由接线判据要求"边界说明在位"，防止将来有人默默扩面或悄悄删掉说明。
- 判据 3 支、门禁 G0.63、登记表 N-53；计数面 635→638。


### 仓内路径引用也进常驻门禁（N-54）
- N-53 把路径存在性"留给一次性普查"，而那次普查的脚本自己带分母 bug（正则只捕到根目录名，把 445 条引用说成 7 条未解析）——
  边界既没被常驻覆盖，给出的数字还不可信。现在收进门禁。
- 划界方式决定它能不能零豁免：**只核以既有仓内根目录开头的路径**。示例路径（`tmp/probe.py`）、
  GPU 主机上的外部脚本天然不以这些根开头，因此不必开例外名单；通配写法视为模式而非指针。
- 第一版就自打：文件索引写死成一份目录清单，漏了 `deploy/`、`runtime/`，于是把 11 条存在的路径报成悬空。
  修法是让索引与 roots 同源，并加一支判据要求"每个根目录里真实存在的文件都在索引里"——
  分母与划界不同源的门禁，只会生产假阳性。
- 现状：8 个根目录、445 条路径引用、60 处行号指针、5 处章节锚点，偏离 0。门禁的 PASS 说明仍是静态文本；
  两处"把两个产物名缩写成同一后缀"的写法改掉，因为它们会以路径形状被读到。
- 判据 1 支新增（roots/索引同源 + 分母阈值），门禁行 G0.64；计数面 638→639。


### 通配声称与"谁在等"的对偶剧本（N-55）
- 承 N-54 的指针门禁再补一类：**仓内通配**（如 `docs/adr/*.md`）也是一条声称——目录里一个文件都匹配不到，
  就是"空目录声称"。字符类只认 ASCII 路径字符（普查时 `\w` 会把紧跟的中文一起吞掉，造出一条假 glob）。
- 挂起取证加**对偶剧本** `half-hang-b`（`image inspect` 挂、`image ls` 通）：上一轮"只有 k8s 会等"的结论
  可能只是剧本恰好掐了 `image ls`。四种剧本同一轮实测（`--timeout 20`，四张偏离表全空）：

| 档 | blackhole | hang-later | half-hang（只 `ls` 挂） | half-hang-b（只 `inspect` 挂） |
|---|---|---|---|---|
| docker | 20.22s | 80.60s | 0.66s | **80.51s** |
| postgres | 20.10s | 20.18s | 0.13s | 20.16s |
| object-store | 20.03s | 20.04s | 0.04s | 20.03s |
| k8s 控制面 | 20.03s | 40.04s | 20.04s | 20.04s |

  读法：角色确实互换（docker 档从 0.66s 变 80.51s），说明"谁在等"由代码结构决定而不是剧本偏袒；
  也修正了上一轮的结论——**docker 档才是最贵的那一层**（逐个候选镜像 `inspect`），而不是只有 k8s 暴露。
- 常驻判据 2 支：通配两极（匹配到不误报 / 匹配不到点名 / 中文尾随不生成假 glob）、
  对偶半挂的"角色互换"断言（含 pg 档两个剧本之间的 0.13s vs ≥2s 对照）。
  真面分母阈值提到 paths ≥400（现 485）。
- 这一轮跨环境复算第一次就红了，红在探针测试自己的断言上：「哪一档快、原因里必须出现哪个词」是环境相关断言，高负载时替身 sh 没在 2 秒内答话，结论就变成那台机器的负载而不是代码。改成只断契约（原因可行动、代价有上限、至少一档几乎不付费、兜底探测那档要付费、角色互换的相对大小），并给探针加「PATH 替身没拦到就报错」的自检。登记表 N-55、门禁 G0.65，计数面 639→641。

### 超时上限改成量出来的，不再写死（N-56）
- 上一轮的复算红给出一个未闭的尾巴：我说"契约断言留了 3×cap+5 的余量，仍属经验阈值"。
  现在阈值有出处了：探针先量**替身自己答一句话要多久**（`_fake_round_trip`，三次取最大），
  有效上限取 `min(10, max(请求值, 4×中位往返（N-57 起改取预热后的 p95，本句是当时的写法）))`（封顶 10s），并把 `请求值 / 实测往返 / 有效上限 / 每档的 bound`
  一起写进 JSON 与表头——判据只引用这些数，测试里不再出现字面秒数。
- 顺手修两处诚实性缺陷：
  ① `docker_probe` 的原因文本此前打印的是调用方写下的默认 20s，而子进程实际夹到 2s——
     改成打印 `TimeoutExpired.timeout`（真正生效的那次），读数与等待时间一致；
  ② 打印循环里的 `print` 被上一次补丁少缩进一级，文本模式每个剧本只印出**最后一档**：
     我当时把这份残缺输出当成"四档都很快"的证据读了一遍，缩进修正后才看到 docker 档在
     half-hang-b 下确实付了 4 次超时（8.78s，逐候选镜像）。
- 探针自检：PATH 替身没拦到就直接报错（宁可红在诊断上，也不让环境状态冒充代码结论）。
- 登记表 N-56、门禁 G0.66；用例数不变（641），本切片只收紧既有判据与修复输出。

### 取证的时间口径改成"预热后的分布"（N-57）
- N-56 留了一句未证实：4× 倍率与 10s 封顶"有实测依据，但没扫过负载"。扫了，但要分三档说：
  - **已证实**：一个**刚写出来**的替身文件，第一次 exec 比之后慢一个数量级。本机 12 次"新建目录→写脚本→连采三次"
    实测 first p50=0.059s／max=0.274s，之后 p50=0.010s；换探针真实形状（`Popen`＋`wait`）另起 6 个新替身，
    first/rest 比值 3.8～8.5。所以 `_latency_stats()` 里"先丢一次再采样"不是装饰：5 个样本时
    nearest-rank p95 恰好就是最大值，冷启动那一跳会直接被当成"往返很慢"喂进上限与可判性。
  - **未证实（更正为本轮之前的写法）**：N-55/N-56 把那次复算红的机制写成"高负载机器上替身来不及在 2s 内答话"。
    本轮在宿主 load≈13（10 vCPU）下采了 60 次预热后 burst，最慢 0.0191s、>0.5s 的样本 0 个；
    也未复现出登记行里那个"第一次起 `/bin/sh` 要 ~3s、p95=3.061s"（同一形状连采 6 次，max 0.0734s）。
    ⇒ 那句 3s 读数本轮打不出来，按**未找到**记账：冷启动惩罚是真的，但它量级在 0.05–0.3s，
      单独不足以越过 2s 上限；越过 2s 需要另外的（没复现出的）条件。
  - 既然机制只证到一半，判据就不建立在机器状态上：改由**夹具**证明。
    `tests/test_hang_probe.py::test_warm_up_is_the_only_thing_keeping_the_cold_exec_out` 造一个
    "第一次 exec 睡 5.5s、之后立刻答话"的替身，两档并排（`warm_up=False`／`True`）：
    前者 p95=6.783s ⇒ `conclusive()` 翻假（工具退 2 说"测不准"），后者判定回到"可区分慢与挂"。
    变异落地已验：把默认值改成 `warm_up=False` 后该支按预期变红（红在读数 6.783 那一行），
    还原后变绿（sha 20fa1377… 对账过，不是空改写）。
- 顺带清掉本轮自己写下的三处不诚实：
  ① `injected["bound_seconds"] = 3×有效上限+5` 是全仓零读者的孤儿公式，而每档 `bound_s` 已由探针按
    "被掐次数 × 有效上限 + 余量"算出——两处各写一份算法正是这一轮要修的事，删掉前者；
  ② `hang_subcommands` 报的是替身认识的子命令全集（五个全列），半挂剧本明明只掐一层——改为按剧本取 `HANGING[mode]`；
  ③ 模块头与 Makefile 还写着"手动跑用真实的 20/30 秒"，而 10s 封顶会把请求 20s 夹到 10s：
    现在打印 `ceiling_bit`，并把"只有 blackhole 不夹（它不起替身、没有往返可量）"写清。
- 封顶与诚实性：`derive_effective()`／`conclusive()` 成对使用；当往返大到让封顶吃光余量
  （如 p95=6.8s 时上限被压到 10s，不足 2× 余量），工具明说"测不准"并退 2，
  不交出一份把慢说成挂的读数。这台子现在把"被预热丢掉的那一次"也打出来，
  本轮 `--mode latency --timeout 2` 真读数：并发 1 p50=0.02s／p95=0.092s／被预热 0.233s（替身文件首次 exec），
  并发 4 p95=0.029s／被预热 0.013s，并发 8 p95=0.028s／被预热 0.012s，三档都判"可区分慢与挂"。
  首次 exec 那 0.233s 与之后 0.01s 量级的差，就是预热在挡的东西。
- 上一版并发判据里那句 `p95 < 0.5s` 是写死的机器事实（正是 N-55 要拆的东西），删掉；
  改为断"探针自己的 `conclusive()` 在三档都为真 + 被预热丢掉的那一次照样报出来"。
- 判据 2 支（公式两极、预热承重夹具）＋ 1 支改写（三档并发仍可判），门禁 G0.67；
  N-56 行内标注机制更正（本轮真的落了字）；计数面 641→644。


### 分流判据改成"registry 答没答话"，并把被截掉的病因接回来（N-58）
- 触发是一手红读数：HEAD `572ab15` 那轮 `make validate` 实测 `failed=1`，
  AssertionError 原文里 daemon 回的是
  `failed to resolve reference "ghcr.io/astral-sh/uv@sha256:04d0…": failed to do request: Head "https://ghcr.io/v2/a…`
  （被我们自己的 `[:200]` 截断），第二通道 `Connection reset by peer` ⇒ `red_undecided`。
  环境抖是触发因，代码里有三项放大项，全部改掉。
- **① 截断方向**：containerd 把传输原因接在最后（containerd release/2.1 的 client 包 `pull.go` 第 188 行 →
  同仓库 core/remotes/docker 包 `resolver.go` 第 649 行 → net 层，本轮重开源文件逐字核过），
  所以取头部等于专门切掉唯一有区分力的一位。改 `_daemon_reading_of()` 取最后一条非空行的全部。
  代价如实记：**今晚那条读数的病因字节已经没了**，复现只能按同一形状补尾。
- **② 症状关键词表 → 答复形状**：`TRANSPORT_KEYS` 那五个词换成 `registry_reading()` 四档
  （absent／answered／no-answer／unreadable）。分类轴取自一手来源（moby 的 `errors.As` +
  `error from registry:` 前缀；go-containerregistry 的 `Temporary()` 按 OCI code ∪ HTTP status 判；
  OCI spec 的码表与"manifest 不存在必须 404"）。**引思路不引依赖**：这里只有 CLI 文本可读。
  `unreadable` 单开一档，专留"我们连错误文本都没拿到"——不折算成环境没问题，也不折算成钉错。
- **③ 一个文件里两把尺**：SBOM 档的 `_transport_skippable` 是同一判断的第二份实现，盲区还不同
  （它把 `denied`／429 这类真答复判成代码失败）。合并消费，新判据按**形状**（不是名字）钉"不许长回两份"，
  因为本文件正文本来就要提这两个旧名字，按子串查会被自己命中——本轮在这条上连踩两次。
- 打靶 7 条读数：4 条本机一手（假摘要 `: not found`／不存在仓库 `error from registry: denied`／
  DNS 解析不到／端口拒连，命令与完整 stderr 见登记行），3 条第三方原文并注明未在本机重放
  （ghcr 403 无 OCI body、TLS handshake timeout、`toomanyrequests` 429）。
- 一支有意的放宽：`denied`/429 从"红"改成"干净跳过"。守护进程那条传输没资格对存在性表态，
  能表态的是第二通道逐字节的 sha256 重算；坏摘要仍必须 `red_pin`（组合表逐档保留）。
- 同一形状的第三处（本轮复算时自己撞上）：`test_the_probe_is_conclusive_across_concurrency_levels`
  断的是 `--mode latency` 的 `rc==0`，而那个 rc 里含「这台机器今天忙不忙」。改成
  rc∈{0,2} 且**与它自己打印的每档判定一致**：不可判集合为空 ⇔ 退 0，非空 ⇔ 退 2 且
  stdout 必须出现「测不准」。这条一致性就是它的全部牙：把退码逻辑短路成永远 0，
  只有真发生不可判的那跑会红（本机今晚三档都可判 ⇒ 这一支照绿），
  而"预热不预热会不会翻转判定"由 N-57 那对夹具极单独钉住，不靠这台机器今天忙不忙。

- 改前/改后同读数对比实测：旧尺 `red_undecided` → 新尺 `skip`，第二通道 absent 时两者都 `red_pin`。
  docker 档 27→30 支真守护进程全绿；计数面 644→647；门禁 G0.68。

### 认证台的超时与取证的上界，各管各的量（N-59）
- 两件事在同一轮顶出来。`make validate` 的 900s 预算被共驻会话顶穿后**打 traceback**
  （并留着一份会被下轮误当本轮读数的旧 junit）；挂起取证那条墙钟上界今晚红在父侧开销上
  ——黑子档 docker：`elapsed=12.62s`、`bound_s=6.0`，其中被掐等待只有 `2.02s`。
- 关键更正：N-55/N-56 当年把复算红记成「高负载下替身 `sh` 答不出话」，N-57 记成「冷启动 ~3s」，
  今晚的量说都不是——被掐等待从未接近 2s，越界的是父侧起解释器 + import 的十秒。
  既有两条 `elapsed <= bound` 在 load≈17–47 下真红，改判 `waited_s <= bound_s` 后同负载变绿；
  开销作为 `startup_overhead_s` 报出来，不进上界也不假装它恒定。
- 有界跑批：先 unlink 旧报告 ⇒ 给整个进程组 SIGINT（`start_new_session=True`）⇒ 30s grace 拿部分报告
  ⇒ 兜底 SIGKILL；越界 = 提交面具名 FAIL「本轮无法计数」，环境读数只在本次跑那一侧，
  所以提交面换台机器仍逐字节相同。预算 1500s（≈2.4× 本机实测 suite 632.3s／647 例）；
  `run()` 其余四步（lint／type／migration／build）超时一律→124 并点名是哪条命令。
- 判据 6 支（有界跑批两极、陈旧报告不得当本轮读数、判决纯函数三态且不含本次秒数、
  `run()`→124、剧本「谁会挂」派生自 FAKE 正文并带反向对照），门禁 G0.69；计数面 647→653。
- 上一轮独立复核的六处过头话同轮修完（逐条见登记表 N-59 第 ④ 项）：其中两处是本轮新写的判据
  自己踩的——按 `process.kill()` 这种**词**查旧形状被自己注释命中；`ENV_FIELDS` 添字段后
  既有遮罩判据立刻红，那是夹具没跟上声明，补字段而不是放松判据。

### sdist 那行 sha 现在可以被第三方复算了（N-60）
- 上游先查过，不是猜的：本机装的与 PyPI 最新的 setuptools 都是 84.0.0（2026-08-08 上传）
  ⇒ 等不到"上游把 sdist 的 tar 头夹住"。三个候选里择一：换后端（前一轮已实测 uv_build 与
  hatchling 同样漂，flit_core 定但不支持本仓两个顶层包）、后处理归一、只改口径。
  选了后处理：借 Debian `strip-nondeterminism` 的分类思路（核到它在 salsa
  `reproducible-builds/strip-nondeterminism` 的 master 分支，`COPYING` 是 GNU GPL-3.0，
  是 Debian 打包链里的 Perl 工具；它具体归一哪些字段本轮没取到源文件，未亲验），
  不引它——这件事只需标准库。
- `scripts/sdist_normalize.py`：成员按名排序、mtime 钉 `SOURCE_DATE_EPOCH`、`mode & 0o755`、
  uid/gid 归零并清 uname/gname、丢 pax 扩展（格式钉 GNU_FORMAT）、gzip 显式 `filename=""`。
- 取径两头的读数：改前两建 `6b6d115a25` 对 `ad66f49101`（wheel 两次都是 `374ea4b59f`）；
  改后 `make verify-artifacts` 打 `sdist=yes wheel=yes`（`sdist: 35e21305d9` 两次逐字节相同），
  `dist/checksums.manifest` 的 sdist 行从 `recomputable=no` 翻成 `yes`。
- 两处消费同一份实现：`make build` 末尾多一行归一，探针在自己那步也调它——
  否则"探针量的形状"与"发出去的字节"会分叉，那正是 N-57/N-59 反复拆的东西。
- 判据 6 支（两建合一 / 内容不许被动 / gzip 头部两头核 / 拿不到 epoch 必须失败 / 消费位恰好两处），
  门禁 G0.70。变异电池 4 支开火 3 支；"去掉 `filename=""`"那支不开火——本实现写进
  `BytesIO` 没有 `.name` 可推断，所以那句显式参数是**防未来改动的保险**，
  本轮按这个措辞写，没写成"已证必要"。
- 口径变化写进运维面：`dist/checksums.txt` 里 sdist 那行是**归一后**产物的 sha，
  第三方复算要走同一条 `make build` + 同一个 epoch + **同一个 Python/zlib**（gzip 那一层的字节随 zlib 参数与版本变，tar 的 GNU 头部布局随 stdlib 实现变）——这条限定是本轮查 `setuptools-reproducible` 时顺带挖出来的：那包只 patch `tarfile.tarinfo`（0.1 版、2024-05 唯一一次发布），从不碰 `GzipFile` 的 mtime，它的自测两建间隔在同一秒内，读码即知不抗跨秒（未实测，标未亲验）。口径已写进 `docs/OPERATIONS.md`、`docs/RELEASE_PROCESS.md`。
- 顺手清上一轮留的账：N-59 的"1500s≈2.4× 实测分布"当时只有 632.3s 一个读数，
  本轮同机另一跑 `make validate` 自报 `elapsed_seconds=331.6`（load [4.26,8.97,19.27]）
  ⇒ 分布本身跨 2 倍，1500s 是**较大那个读数**的 2.4 倍。
- 计数面 653→659；N-34 与 G0.44/G0.46 里"sdist 不可复算"的陈述本轮按事实标注/更正。

### G1–G4 那五支物理验收脚本，从今天起有常驻读者（N-62）
- 它们是物理待验那一整块的执行入口（`preflight_gpu_host.sh`／`gpu_acceptance.sh`／
  `isaac_sim_smoke.sh`／`isaac_lab_cartpole_smoke.sh`／`franka_smoke.sh`，合计 287 行），
  而本机没有 NVIDIA 设备 ⇒ 从来不进 `make validate`。已有的两条相关判据都不碰执行：
  `make lint` 里的 `bash -n` 只看语法，`test_supply_chain` 那条只看"脚本里引用的镜像串与
  Dockerfile 钉死的那份逐字相等"。本轮之前没有任何一支常驻用例 exec 过它们。
- 新增 `tests/test_gpu_gate_scripts.py`（6 支）：PATH 替身 docker／nvidia-smi／ss／ldconfig
  造三种宿主（全装／容器失败／缺件），逐支核"退出码与打印的判决是否一致"。
  替身跑在**净 PATH**（替身目录 + `/usr/bin:/bin`）上——沿用宿主 PATH 会得到一种假缺件档：本机真装着 docker。
- 两处自我更正，都写进判据不留 hindsight：
  ① 动手前我以为 `rc=$?` 会取到 `tee` 的 0 从而把失败说成通过；实测在 `set -euo pipefail` 在场时不成立，
  单独摘掉 `pipefail` 也不成立——**两处一起坏**才造得出"G2 PASS + 退 0"。判据因此钉组合后果，不钉单行存在。
  ② 缺席断言最初写成"文本里不许出现 PASS"，被脚本自己那句"不得在本机冒充 PASS"挡了（合规脚本判红）；
  改成按每支脚本自己的判决串判（`SUCCESS_MARKER`）。
- 副本注入三臂自证：`pipefail` 与 `PIPESTATUS[0]` 同坏 ⇒ 假绿被抓；缺件档 `exit 2`→`exit 0` ⇒ 假通过被抓；
  空改写先被 assert 挡掉。预检另钉住"失败总结走 stderr、逐样 MISSING 走 stdout"。
- 物理档本身仍 `PHYSICAL_*_PENDING`：本轮闭上的是"没有硬件时这些脚本会不会骗人"那一格。
- 同轮把 N-61（要不要换成 hatchling 后端）用实测结案，不 parked。
- 计数面 659→665，门禁 G0.71。

### 释放 GPU 之前先问一句"runtime 真的没了"（N-63）
- 缺陷：`orchestrator.stop()` 把 FAILED 也当作"幂等收尾"入口（改前 :320 那个
  `{STOPPED, FAILED}` 集合），直接进 `_finalize_stop` —— 结算 + `scheduler.release` +
  置 STOPPED，**一次都不叫 `provider.stop`**。于是 `provider.stop` 失败留下的那个还在吃
  `--gpus device=N` 的容器，重试之后被判已停、卡被放回池子给下一次分配；而
  `reconcile_all` 按 status 跳过 STOPPED/FAILED（现 :519），这孤儿此后再没人看一眼。
  同一条不变量其实早就写在 `destroy()` 里（"provider 清理失败 → 不得释放 GPU / 置
  DELETED：否则一卡双跑"）；`reconcile_all` 的 STOPPING+ALIVE 分支是第二处漏点
  （`contextlib.suppress` 吞掉停止失败后照样 finalize）。配额 monitor 走 `self.stop()`
  （现 :682），一并被这道闸门管住。
- 现成用例为什么没抓到：`tests/test_streaming_lifecycle.py::test_failed_stop_cleanup_is_retryable`
  断的是 retry 后 `STOPPED` ＋ 卡 `AVAILABLE`，**从没问过 provider 侧那个 runtime 还在不在**——
  观测缺口正好盖住缺陷。它 docstring 里"完成全部 cleanup"才是本轮落地的语义。
- 新增 `_release_admitted(workspace, *, command_succeeded)`（:385，一条判决只留一份实现）：
  命令成功 ⇒ 只要 provider 不再自述 ALIVE 就放行；命令失败 ⇒ 只认 MISSING。整条清理序列收在
  `_stop_cleanup`（:352），`stop()`（:323）与 `reconcile_all`（:586）两个入口都走它——判据在
  清理函数里被问两次（成功档／失败档），不是两份实现。
- 没达成目标时对外说什么（第二处设计修正）：状态**留在 STOPPING** 而不是写 FAILED，
  `error_message` 记原因；`execute_operation` 的 STOP 档（:136-146）在结果不是 STOPPED 时
  抛错，交给 worker 的 attempts 重试。理由是 `recover_stuck_gpu_allocations`
  （`app/services/scheduler.py:257-267`）只保护非终态 {PROVISIONING, RUNNING, STOPPING}：
  把“这一轮没停下来”写成 FAILED，会被它当终态孤儿把卡放掉，等于绕过本判据重演一卡双跑；
  而 STOP operation 若不抛错就记 SUCCEEDED，台账在替没做的事背书（ADR 0002 的“可重试的
  失败不得写成终态”由此从 provision 扩到 stop）。连带改判一条常驻用例：
  `tests/test_streaming_lifecycle.py::test_failed_stop_cleanup_is_retryable` 原先断第一次
  尝试失败就写 FAILED（钉 as-is），现断 STOPPING ＋ error_message ＋ 分配行仍在、卡未
  AVAILABLE，改判理由写进它自己的 docstring。
- 配额 monitor 同一条链上受益：它经 `self.stop()`（:682）触发停止，原先无条件
  `stats["stopped"] += 1` 并把原因覆盖成“credits exhausted”——放行没达成时报的是“我停下了”。
  现在只有真到 STOPPED 才计数与写配额原因，否则留 stop() 写下的阻塞原因并记 warning。
- 两处自我更正（都在本轮内、提交前发现）：① 第一版把 `_finalize_stop` 挪出了 try，于是
  `scheduler.release` 的异常会穿出 `stop()` 抛给调用方——ADR 0002 明确禁止 provider/scheduler
  边界异常外泄；现在 release 失败被收成原因字符串、状态留 STOPPING，并有常驻判据钉住
  “这一句不许抛 ＋ 钱已入账而卡仍占着 ＋ 换回可用 scheduler 后重试收敛”。② 第一版把没达成
  目标写成 FAILED，正落进上面那条回收器的盲区；改成 STOPPING 是读了 `app/services/scheduler.py:257-267`
  之后翻的。
- 择引（本轮调研真的改了设计）：Kubernetes finalizer 语义 —— `kubernetes.io/pv-protection`
  原文"in use ⇒ enters a Terminating status, but the controller can't delete it because the
  finalizer exists. When the Pod stops using the PersistentVolume, Kubernetes clears the
  finalizer, and the controller deletes the volume"，且删除请求只回 `202 Accepted`
  （k/website `content/en/docs/concepts/overview/working-with-objects/finalizers.md`，
  本机 curl 取到 5165 字节后亲读；kubernetes.io 页面本身在本机 fetch 失败两次，未引页面）。
  moby `api/swagger.yaml:8984-8995` 把 `POST /containers/{id}/stop` 的 `204 no error`／
  `304 container already stopped`／`404 no such container` 都不算失败（475309 字节，本机取回后读到位）。
  **moby 那一手改了我第一版设计**：只按"命令成功"放行的话，K8s 对已删 Deployment 抛的 404
  （`app/services/providers/k8s.py:328-329`）会把一张没人用的卡永久钉住 ⇒ 补上"命令失败但 provider 亲口
  说没了 ⇒ 仍放行"这一极。两者都是借语义，不引依赖。
- 判据：`tests/test_stop_release_admission.py`（13 支）。provider 侧的 runtime 事实由替身
  自己声明（mock 只有 UNKNOWN，证不了“还活着”）：重试必须真的再叫一次停止；命令成功但
  仍在 ⇒ 不结算、不释放、不判 STOPPED；命令失败但 provider 报缺 ⇒ 必须释放（另一极，防把
  没人用的卡永久钉死）；reconcile 的谎报停止；`scheduler.release` 失败不外泄且可恢复；
  runtime 还活着时 STOP operation 必须 RETRYING 而不是 SUCCEEDED；透支触发的 monitor 不得
  把没放行的停止计成已停；三档行为的共同不变量（“卡 AVAILABLE”与“provider 说活着”不得
  同时成立）；判据恰好一份实现且两个入口都经它（按 AST 读链：`_release_admitted` 定义 1、
  `_stop_cleanup` 定义 1、判据被消费 2 次、`stop`／`reconcile_all` 各消费 `_stop_cleanup` 1 次；
  注释里提名字不算），反向对照按锚点唯一性逐条落地（把 reconcile 的收尾换回直接 finalize
  ⇒ `reconcile_consumers` 归 0）。
- 改前复算（拿 HEAD 那份 `orchestrator.py` 就地换面跑同一批尺子，cp＋`git show`，跑完按
  sha 还原，两端 `d79baefa…` 一致）：`FFFF.FFFFF.F...F...` —— 19 支里 11 支开火、8 支照绿；
  照绿的正是合规档（reconcile 正常收敛、参数化 `stops`／`error_then_gone`、streaming 的正常
  stop／destroy／幂等三支），开火的含被改判的那条 as-is 用例、monitor 计数那支与“判据没接线”
  两支。本轮内另两次同形状复算：`FFFF.FFF.F...F...`（10 开火，sha `62998cdd…`）与最早的
  `FFFF.FF.F.`（7 开火，sha `9bfdd83e…`）——尺子加长时开火集合单调变大，缺陷读数一致。
  邻面回归全绿
  （streaming_lifecycle／durable_ops／worker／gpu_single_authority／billing_policy／credit_holds），
  `make lint` 与 `make typecheck` rc=0。
- 诚实的边界三条：① "命令成功"那一档 UNKNOWN 是**放行**的——mock/演示路径没有可观测
  runtime，不放行就没人能停得下来；残留风险是"停止命令成功之后 docker 二进制才消失"这种
  罕见形状，此时按 UNKNOWN 放行。② 只修了 stop/reconcile 两路；provision 失败那一路是
  同一形状的第二实例（`_execute_provision` 在 :249 用 `contextlib.suppress` 吞掉补偿
  `provider.destroy` 的异常，随后 `_fail` 在 :304 无条件 `scheduler.release`），它得先量清
  "destroy 失败该由谁认账"，登记为 N-67 而不是顺手改。③ 物理 GPU 上的真容器没验（本机无卡）：
  本轮核的是控制面判决与权威表一致，不是 docker 真把容器停了。
- 计数面 665→678→686→693→701→702→711→723→742→751→775→779→793→796→800→807→812→818→824→844→866→877→898→903→906→918→948→955→962，门禁 G0.72／G0.73／G0.74／G0.75／G0.76／G0.77／G0.78／G0.79／G0.80／G0.81／G0.82／G0.83／G0.84／G0.85／G0.86／G0.87／G0.88／G0.89／G0.90／G0.91／G0.92／G0.93／G0.94／G0.95／G0.96／G0.97／G0.98／G0.99／G0.100／G0.101／G0.102／G0.103／G0.104／G0.105。

### 两个钱包终于不相交：成员行不再被算进组织池（N-71，闭合登记项 N-65）
- 缺陷（本轮先量后改，/tmp 探针跑真对象）：`app/services/billing.py:191-198` 把
  `ledger.balance(user)` 与 `ledger.organization_balance(org)` 直接相加，而账本行常态是
  **两个归属一起写**（usage 见 `app/services/ledger.py:100-111`，充值/调整见 `app/routers/usage.py:68-77`），
  两个谓词按构造相交。读数：给 a1 充 1000 ⇒ `available(a1)=2000`（应然 1000）、
  同组织从未出钱的 `available(b1)=1000`（应然 0）⇒ 未出钱的成员可以径直过启动门禁；
  a1 再消耗 300 ⇒ `available(b1)=700`，别人的消耗扣到 b1 头上。
- 分池口径不是我发明的，仓里已经写着：`tests/test_credit_holds.py::
  test_org_credits_are_visible_but_hold_is_taken_once`（:274-297）给组织充值时只写
  `organization_id`、不写 `user_id`（:278-284），断言 5100 = 100 个人 + 5000 组织；
  `reserve_launch` 的注释也写“记账账户：优先个人”。所以组织池的定义是**无主行**
  （`user_id IS NULL`），个人池收该用户全部行。改后那条既有用例照绿。
- 择引（本轮真的据此选了“先改读侧”）：Odoo `account.move.line` 用 CHECK 强制
  “一条分录恰好一个归属”——`_check_accountable_required_fields = CHECK(... OR account_id
  IS NOT NULL)` 与非记账行必须 `account_id IS NULL`
  （Odoo 主干 `addons/account/models/account_move_line.py` 第 557-564 行，本机从 `raw.githubusercontent.com/odoo/odoo/master/…` 取回 203741 字节亲读）；
  PostgreSQL `ddl.sgml:602-616`（225811 字节）说 CHECK 是列值必须满足的布尔表达式——
  它只约束写入，不回头改历史行。本仓 `app/models.py:400` 明写“账本 append-only，不回填改写”，
  所以历史双主行只能靠读侧谓词归池 ⇒ 择一：借 Odoo 的“恰好一个归属”语义先做读侧分池，
  写侧 CHECK／`account_id` 列留作另轮（需要迁移与归池决策，不能顺手加）。
- 改法：`app/services/ledger.py:200-216` 的 `organization_balance` 加 `user_id IS NULL`；
  `app/services/billing.py:178-189` 新增 `gross_credits`，把重复三遍的相加合一
  （`available_credits` :198、`reserve_launch` :230、monitor 投影余额 :668 都改读它）。
- 判据 `tests/test_credit_purse_split.py`（8 支）：成员行只进个人池（a1 可用 1000、
  组织池 0）；同组织无钱者可用 0；**无主组织行对两个成员都可见**（合规侧，5000/5100 与
  既有用例同数）；别人的消耗不扣我；无组织用户不受影响；相加表达式在 app/ 里按 AST 数
  恰好一处（`def gross_credits` 也恰好一份）＋合成源码反向对照（再加一份就读到 2）；
  三个消费位各自核 `gross_credits` 调用数。
- 一处自我更正（判据写错，不是代码错）：我第一版把“可用额为 0 就该被启动门禁拒”写成应然，
  实测 `check_launch_eligible` 只挡负数（`billing.py:~80` 的 `if available < 0`），而
  `app/config.py:56` 出厂默认 `billing_enforce_preauthorization=False` ⇒ 0 余额确实能开机。
  判据因此改成两档极：预授权**开启**档必须拒 b1、放 a1；“0 余额可开机是否应然”不在本轮
  改门禁语义，登记为 N-72。
- 改前复算（把 ledger/billing/orchestrator 三个文件一起换成 HEAD 再跑同一批尺子，
  cp＋`git show`，跑完按 sha 还原：`1080fb3e…`／`bec748c5…`／`a4b4f5b5…` 两端一致）：
  `FF.F.F.F` —— 8 支里 5 支开火（三档钱＋两份接线判据），照绿的 3 支正是合规档
  （无主组织行共享、无组织用户、尺子自测的合成反向对照）。只回退 ledger.py 时读数是
  `FF.F....`（3 开火）——两份“相加副本”的判据要靠 billing/orchestrator 一起回退才会开火，
  第一遍我漏了这条，复算做实后才看得见。
- 邻面回归 47 支全绿（billing_policy／credit_holds／usage_admin_adjustment／
  stop_release_admission），`make lint` 与 `make typecheck` rc=0。
- 未证实：真 PostgreSQL 上的并发充值/扣减没重跑（pg 档本轮未开）；组织池“无主行”的
  写入路径目前只有测试与管理端调整两种，产品侧“给组织充值”的端点并不存在——
  也就是说改后可用额几乎只等于个人余额，这是口径正确化的结果，不是新问题。
- 计数面 678→686，门禁 G0.73。

### hold 的幂等键改成按启动轮次发：第二次启动重新圈住额度（N-73，闭登记项 N-66）
- 缺陷（/tmp 探针跑真对象实测，改前先量）：`reserve_launch` 用 `f"hold:{workspace_id}"`
  当键，而 `CreditHold.idempotency_key` 是全局唯一列（`app/models.py:447`）；第一次启动
  把那条 pending capture 掉之后，同一 workspace 再次启动的 INSERT 必然撞唯一键，而改前
  的兜底查询只按 key 回查、**不看状态** ⇒ 把上一轮已 capture 的行当成本轮授权返回。
  读数：首启 `('hold:ws-A','pending',300)`、available 9700；capture 后再启
  `('hold:ws-A','captured',300)`、磁盘 pending 数 0、available 回到 10000（一分没圈）；
  对照档全新 ws-B 仍拿到 pending 300。调用方 `app/services/orchestrator.py:190` 把返回值丢掉 ⇒ 全程无报错。
- 改法：`_hold_key`（`app/services/billing.py:272-291`）按该 workspace 已有 hold 行数发轮次号
  （`hold:{ws}:{n}`，n=0,1,2…），hold 表不删行 ⇒ 新键必然空闲；兜底查询（:254-268）改成
  只按 `workspace_id + status=PENDING` 收敛，找不到 pending 就照原样抛——
  宁可 provision 失败重试，也不能把一笔已花掉的额度当成新授权。
- 跳过外部调研的理由（如实记）：改动只落在一个方法的键推导与一条查询谓词上，约束全部来自
  仓内既成事实（全局唯一列 `app/models.py:447`、部分唯一索引 `app/models.py:459-465`、hold 表
  append-only）；本轮想引的两个外部源都没打开——`docs.stripe.com/connect/separate-charges-transfers`
  回 404，PostgreSQL `doc/src/sgml/ref/create_index.sgml` 取回超时（0 字节），故不引任何未读到的出处。
  另我一度加了个"撞键就换重试号再试"的循环，写完发现回滚后行数不变 ⇒ 重试必然撞同一个键，
  那是走不通的死路，删掉；相应地不测"终态行占住本轮键"这一档（hold 不删行 + 键按行数递增，
  非并发下构造不出，构造出来的话测的是我自己刚删掉的循环）。
- 判据 `tests/test_hold_round_key.py`（7 支）：capture 过一轮后再启必须拿到 pending 且
  available 真的减 300；同轮重复调用幂等（同 id、只圈一次，改前也绿的合规档）；启动失败
  退回额度后重试能重新圈；三轮启动留三行、键互不相同、状态 captured/captured/pending；
  跨四种真实序列的公共性质"`reserve_launch` 永不返回终态行"；键模板按 AST 判必须同时含
  workspace 与轮次号（arity==2）＋合成反向对照（改前形状读成 1、模板消失读成 0，两档都不合格）。
- 改前复算（`git show HEAD:app/services/billing.py` 就地换面，跑完 sha 还原两端 `7a38b955…`）：
  `F.FFFF.` —— 7 支里 5 支开火，照绿的正是"同轮幂等"合规档与尺子自己的合成对照。
  邻面 `tests/test_credit_holds.py` 13 支全绿（含 `test_org_credits_are_visible_but_hold_is_taken_once`
  那支钉"hold 记在个人账户"的既有用意），`make lint`/`make typecheck` rc=0。
- 未证实：真并发（两个事务同时 reserve 同一 workspace）下的撞键收敛只在 SQLite 上做了形状
  验证——SQLite 没有真行锁（`with_for_update` 被方言整条丢弃，见仓库既有说明），那一半仍在
  `tests/test_postgres_concurrency.py` 的档位里，本轮没新开并发臂。
- 计数面 686→693，门禁 G0.74。

### 累计 GPU 秒改成账本投影：重放不再让配额门禁读成两倍（N-74，闭登记项 N-64）
- 缺陷（N-64 登记；本轮由子代理实施、由我在主线独立复算认证）：`_settle_run` 无条件
  `workspace.accumulated_seconds += run_seconds`，而账本靠 `idempotency_key` 对同一运行段去重
  ⇒ 重复结算只扣一次钱、计数器却加两次；且 `run_seconds` 是按重放那一刻的流逝时间重算的，
  只会更大。受害的不是报表而是门禁：`app/routers/usage.py:36,40`（展示＋估价）、
  `app/services/billing.py:380`（`course_usage_seconds`，判据 `used >= quota_seconds`）、
  `app/static/app.js:425,703`。
- 改法取仓里已写着的教义（`ledger.py` 模块 docstring："balance 始终 = SUM(amount)"）：这一列既然
  与 balance 同性质，就该同样从账本派生，而不是由写侧累加。新增
  `CreditLedgerService.settled_gpu_seconds()`（`app/services/ledger.py:97-109`）作 USAGE 行唯一
  读数口径；`_settle_run`（`app/services/orchestrator.py:511-519`）结算后 **SET** 这一列，并把
  返回值从"本次重算的 elapsed"改成"账本实际认下的秒数"（`booked`），下游 `capture_hold` 与
  `record_gpu_seconds`（:441-456）据此行动；投影是 SET 而非 patch，所以历史上被 `+=` 吹起来的值
  会被纠正（C3）。
- 为什么"从账本派生"是补口径而不是新发明：`settle_workspace_run` 是 USAGE 行的唯一写入方——
  我自己重扫（不采信子代理读数）：`LedgerType.USAGE` 在 app/ 只出现在 `app/services/ledger.py:106`（本轮新增的
  读数谓词）与 `app/services/ledger.py:129`（写入本身），枚举定义在 `app/models.py:50`；`record()` 的另两处
  生产调用是 `app/routers/usage.py:68`（RECHARGE）与 :101（ADJUSTMENT）；`settle_workspace_run(`
  的生产调用点只有 `app/services/orchestrator.py:423`，其余命中全在 tests/。
- 判据 `tests/test_settled_projection.py`（8 支）：C1 同段重放留一行 USAGE、列==那一行、返回值
  也==它；C2 登记项读到的那条路（release 失败→重试）列不翻倍；C3 列上先写 999 被投影纠正成 30；
  C4 连续两段都留在投影里（改前也绿的护栏，docstring 里如实标）；C5 全 app/ 按 AST 判"不许再有
  对这一列的增强赋值"＋投影读数恰好一份定义；C5 的合成反向对照（旧形状开火、两种合法写法不开火）；
  C6 配额门禁读到的数==账本 SUM，并在配额边界两档各钉一次（`used >= quota` 拒、差一秒放行）；
  C7 零秒段不写行、投影不被空段改动、返回 0（钉 `entry is None` 那一支）。
- 改前复算（我在主线做，两臂各自单变量）：只把 `app/services/orchestrator.py` 换成 HEAD（留新
  读数方法，让失败读成"数"而不是"缺方法"）与把两个文件一起换成 HEAD，**两臂都是 `FFF.F.F.`**
  （8 支里 5 开火），开火理由逐条相同：`累计列（120）与账本唯一一行（30）分叉`／`列=60、账本=30`／
  `投影没有覆盖旧值：列=1029`／`累计列又回到写侧累加：['app/services/orchestrator.py:418']`／
  `门禁看到 60`。照绿三支＝C4（护栏）、C5 合成对照、C7（旧代码返回 elapsed 也不崩）。跑完按 sha
  `b8c48ace…`／`98f79948…` 还原一致。换面前置的合流核验：`git apply` 后两文件 sha 与子代理
  worktree 读数逐字节相同，所以"它跑的"与"我认证的"是同一份代码。
- C5 的第二条从句（"定义恰好一份"）没有常驻反证臂，就地变异补一次：给 `settled_gpu_seconds`
  加一份同名重复定义（sha 先变 `1209733a…` 证落地）→ 恰好只有那一条从句开火
  （"该恰好一份定义，实际 2"），其余七支照绿；还原后 sha 复验一致。这一趟留下一个坑：第一版我把
  副本改名成 `..._dup`，那是合法 Python 但"数名字"的从句看不见 ⇒ 白跑一次，注入必须与被数的那个子串同形。
- 更正一处指针：判据原文写 `app/services/billing.py:367`，那是子代理 worktree（base `75da506`）
  的行号；N-73 之后 `course_usage_seconds` 实际在 :380，已按盘面重解改掉。本仓指针门禁只核"行号落在
  该文件行数范围内"，`:367` 当时也在范围内 ⇒ 这类漂移它结构性看不见，只能靠落笔前重开盘面。
- 邻面（我自己复跑，不引用子代理读数）：`test_ledger`／`test_billing_policy`／
  `test_streaming_lifecycle`／`test_stop_release_admission`／`test_credit_holds`／
  `test_credit_purse_split`／`test_hold_round_key` 与新判据合跑 76 支全绿；
  `ruff check app tests` All checks passed；`mypy app` Success: no issues found in 41 source files。
- 未证实＝两条**量出来的**残留，已按 N-75／N-76 登记，不在本轮设计范围内动它：
  ① 指标计数器 `gpu_seconds_total` 在 stop 重放里仍加两次——实测
  `start=0.0 after_first=+30.0 after_retry=+30.0`（累计 60），而同一世界里 `ledger_sum=30`、
  `accumulated=30`；本轮只把"记的是账本没认过的 elapsed"换成"记的是账本认下的 booked"，
  没管"每次进入 `_finalize_stop` 都记一次"。
  ② `destroy` 的 `provider.destroy` 抛错那一路（`app/services/orchestrator.py:464-502`，故意上抛让 DESTROY 重试）
  会留下"RUNNING 且这一段已结算过、`started_at` 未清"的窗口，`course_usage_seconds` 的 live 项
  （`app/services/billing.py:396` 的状态门）于是把同一段再算一遍——实测
  `status=running accumulated=30 ledger_sum=30 而配额读到 60`。这条比登记项原措辞更精确：
  release 失败那一档状态是 STOPPING，live 分支根本不进，真正可达的是 destroy 失败档。
- 计数面 693→701，门禁 G0.75。

### 节点不一致那路先停 pod，认账了才放卡（N-77，闭登记项 N-68）
- 缺陷（N-68 登记于 N-63 那一轮，本轮按登记的顺序修）：`reconcile_all` 的 K8s 节点不一致分支
  是 settle + release + FAILED 三连，全程没叫过 `provider.stop`。观测本身说明那个 pod 活着
  ——`actual_node` 就是从 `provider.inspect(w)` 读来的。于是「pod 还在错的节点上吃卡、卡却已回池」，
  而 `reconcile_all` 之后按 status 跳过 FAILED，再没人看一眼。这是 N-63 那条不变量的第三实例
  （第一实例 stop 重试、第二实例 provision 失败＝N-67、第三实例这里、第四实例 warm pool＝N-69）。
- 改法：这一条分支改成「先 `error_message` 落原因，再走 `_stop_cleanup`」——它内部是
  streaming terminate → `provider.stop` → `_release_admitted` 准入 → `_finalize_stop`（结算/放卡/STOPPED）。
  认账了才把 STOPPED 改成 FAILED 并计 `failed`；**没认账就保持 RUNNING**（不写终态）：既没有可核对的
  runtime 事实支持写 FAILED，也因为 RUNNING 在 `recover_stuck_gpu_allocations`
  （`app/services/scheduler.py:257-318`）的保护集内——写成 FAILED 会被它按「非占用态」把卡清成
  AVAILABLE，正好绕过刚加的准入。下一轮这条分支接着重试（沿用 N-63 的「重试真的再叫一次」）。
- 选型门禁跳过理由：不是新的技术方案，是 N-63 已定设计的第三个接线点，候选集在那一轮已经比过
  （借 Kubernetes finalizer 的「在用资源不得判为已删除」＋moby 把 304/404 都算停止成功）。
  两个候选自问自答：① 复用 `_stop_cleanup`（选它——本仓有一条 AST 判据钉「释放准入判据恰好一份实现」，
  自己写第二份当场就红）；② 直接 `provider.destroy` + `_release_admitted(command_succeeded=False)`
  （不选：节点不一致要的是「停掉」，不是「连资源定义一起删」，且会把已建好的 Service/PVC 一起带走）。
- 判据：`tests/test_k8s_node_truth.py` 重新造了一只有状态的夹具（`stop` 计数、`reconcile` 随
  停没停改口；`stop` 的返回形状照抄 `app/services/providers/k8s.py:317` 的 None）。两支各钉一极——
  诚实档：`stop_calls == 1` ＋ FAILED ＋ 分配行没了 ＋ 卡 AVAILABLE；
  撒谎档（stop 正常返回但 provider 仍自述 ALIVE）：`failed == 0` ＋ 状态仍 RUNNING ＋
  分配行还在 ＋ 卡仍 ALLOCATED，第二趟 `stop_calls == 2`（不是记一笔就算），第三趟 provider 改口才放卡写终态。
  连同既有的「节点一致→保持 RUNNING」合起来 6 支；`tests/test_stop_release_admission.py` 的接线棘轮
  随之改判：`reconcile_consumers` 1→2，反向对照改成三档（全不改 2／只断一条 1／两条都断 0）——
  原来只数「0 与 1」，新增第二条分支后「少接一条」会被读成通过。
- 改前复算（把 `app/services/orchestrator.py` 换成 HEAD 那份就地换面，跑完按 sha `b8c48ace…` 还原）：
  `...FF.........FF...`（19 支里 4 开火）——
  `节点不一致没有去停那个 pod（stop_calls=0）`／`没认账却计了失败：{'failed': 1, …}`／
  接线棘轮 `{'reconcile_consumers': 1} != 2`／反向对照自己的锚点门 `锚点不是恰好两处（1）`。
  注意第一支：改前「FAILED＋卡回池」这两条它是满足的，开火的只有「没去停」那一句 ⇒ 原用例把缺陷
  当成了契约，本轮把它拆成两极才是真判据。
- 顺手更正一条对运维的假承诺：`monitor_runtime_quotas` 的 docstring 写着「停止走 `_finalize_stop`」，
  而代码走的是 `self.stop` → `_stop_cleanup`（N-63 之后就不一样了）；改成按实现说话，并补上
  「没被认账的停止不写终态、也不计入已停数」。
- 邻面（我自己跑）：`test_stop_release_admission`／`test_k8s_node_truth`／`test_worker`／
  `test_streaming_lifecycle`／`test_durable_ops`／`test_provision_rollback`／`test_settled_projection`／
  `test_warmpool`／`test_warmpool_claim` 合跑 100 支 rc=0；`ruff check app tests` All checks passed；
  `mypy app` Success（41 files）。
- 未证实：真集群里的「缩容到 0 之后 pod 还在」（finalizer 未放行）没验——本机无 K8s 档，
  撒谎档是靠夹具的自述模拟的，属机制限制而非本轮没做完；节点不一致在真机上的 GPU 双跑后果
  同样只到控制面判决为止。另外本轮**没有**新开登记项：回收器那条「终态即放卡」的后门已由
  N-63 的处置行与 N-69 的三重不可见分别记下，不再开双份。
- 计数面 701→702，门禁 G0.76。

### 「不存在」只能由引擎说（N-78，闭登记项 N-70）
- 缺陷有两处伪造缺席，都落在 `app/services/providers/docker.py`：
  ① `reconcile` 开头 `if not workspace.container_name: return MISSING` —— 名字是 provision
  先 `docker run --name ec-…` 建容器、之后才持久化到那一列的，中间崩了就留下「容器活着而列为空」；
  而同一时间 `destroy`/`pull_artifact` 按 `ec-{id[:12]}` 推导去找它 ⇒ 一个容器同时是活的和没的。
  ② `inspect` 把**任何** rc!=0 都读成 `{'state': 'absent'}` —— 连「守护进程连不上」也算。
  后果接到 N-63 的准入上：`_release_admitted(command_succeeded=False)` 只认 MISSING，
  于是 daemon 抖动那段时间里每个 workspace 都「看起来没了」，卡被放回池子。
- 本机 docker CLI 实测（daemon 在跑，非推断）：两种形状同为 rc=1，只差 stderr ——
  不存在 `error: no such object: <name>`；连不上 `Cannot connect to the Docker daemon at
  tcp://127.0.0.1:1. Is the docker daemon running?`；另有 `docker rm -f <absent>` rc=0 无输出、
  `docker stop/start <absent>` rc=1 `Error response from daemon: No such container: X`、
  `docker ps -a --filter name=^X$` rc=0 空输出。⇒ 任何按 rc 判决的实现必然把两者混为一谈，
  分档只能读文案。
- 选型门禁（本轮真做了外部核对）：候选 A `docker` 7.2.0（docker-py；PyPI JSON 实取 107 KB，
  `LICENSE` 正文取回 10758 字节首行 Apache 2.0，近三档 releases 7.0.0b3/7.1.0/7.2.0，
  `docker/errors.py` 取回 5379 字节并读到 :93 把 404 单独收成 `NotFound(APIError)`、
  :83-90 还分 client/server 错）；候选 B `podman` 5.8.0（podman-py，PyPI license 字段直接读到
  Apache License；面向 podman 的 REST，与本机 docker CLI／`--gpus` argv 路径不对口）。
  择一：**自研＋借 A 的类型语义**（不引依赖）——本仓 `provision` 故意把 argv 交给守护进程自己
  验收（G0.19 钉着那批判据），换 SDK 会把它拆掉，且 `edge_agent` 是 stdlib-only 包；
  借到的具体东西是「404／不存在」与「问不到」是两个判决，不是一个布尔。
- 改法（三份尺子共同的落点）：`DockerProvider._name()` 成为容器名唯一推导口径（provision/start/
  stop/destroy/inspect/logs/wait_ready/exec/pull_artifact 全改读它）；`inspect` 失败经
  `_absent_or_unknown(stderr)` 分 absent／unknown，`_run_checked` 的两档确认共用同一份名字表
  （原来两份文案各写各的，`"no such"`／`"not found"` 那一份比实测文案宽，读成 absent 就放卡）；
  `reconcile` 把 unknown 读成 UNKNOWN。`KubernetesProvider.start/stop` 原来是
  `if not workspace.container_name: return` —— 空列时「命令没报错但其实什么都没做」，
  改为与 `destroy`/`reconcile` 同源的 `_deployment_name`。
- 判据 `tests/test_provider_absence_evidence.py`（8 支）＋真引擎那一条
  `tests/test_docker_provider_integration.py::test_reconcile_asks_the_engine_when_the_name_column_is_empty`：
  空列＋引擎说在跑 ⇒ ALIVE（改前 MISSING）；引擎说 `no such object` ⇒ MISSING（合规侧，改前也绿，
  如实标成护栏）；连不上 ⇒ inspect unknown／reconcile UNKNOWN；跨文件后果那一支直接调
  `_release_admitted(..., command_succeeded=False) is False`，并把「`command_succeeded=True` 时
  UNKNOWN 放行」这一 by-design 口子也断出来（限定①登记于 N-63 处置行）；stop/start 在空列时必须
  真发出带推导名的命令；结构判据按 AST 数「以空列断定 MISSING／返回 absent／裸 return 空转」的形状
  必须为 0，且每个 provider 模块的容器名推导式恰好一份（配三种改前形状的反向对照 + 真源码放回
  一行的正向对照）。
- 自己踩到的一处判据缺陷（记下来因为它改变了结论）：结构判据第一版用 `"ec-" in unparse(node)` 数推导位点，
  把 `_pvc_name` 的 `f"ec-pvc-{...}"` 也数进去，读出「k8s 有两份推导」的假违规 —— 收紧成「首个字面量
  恰为 `ec-`」，并给这个假阳性补了一条常驻反向对照（PVC 那份不许被算成容器名推导）。
- 改前复算（把 `docker.py` 与 `k8s.py` 一起换成 HEAD）：单元档 `F.FFFF..`（8 支里 5 开火），
  一手读数 `assert MISSING is ALIVE`／`assert 'absent' == 'unknown'`；真引擎档也红：
  `空列被当成缺席：引擎说这个容器在跑`（那是一个真在跑的 `ec-…` 容器）。跑完按 sha
  `18235e7e…`／`e6b04bce…` 还原一致。
- 邻面：`test_docker_provider`／`test_k8s_provider`／`test_docker_gate_hardening`／
  `test_stop_release_admission`／`test_streaming_lifecycle` 与新判据合跑 66 支 rc=0；
  docker 集成档（真容器）31 支 rc=0 无 skip；`ruff check app tests` All checks passed
  （中途被自己一条对未启用规则 `ARG002` 的多余 noqa 判红一次）；`mypy app` Success（41 files）。
- 未证实：daemon 真被掐断这一档在常驻侧只用夹具文案模拟（把守护进程停掉会打断同树上的其它用例，
  属可控性限制）；`restarting`/`created`/`paused` 与 K8s `available_replicas=0`（含 Pending pod）
  现在仍被判 MISSING —— 那是另一根轴（「存在但没在跑」到底算不算还在吃卡），本轮没自行改语义，
  登记为 N-79。
- 计数面 702→711，门禁 G0.77。

### 预热池认领撤销那一路的放卡，改由 provider 认账（N-80，闭登记项 N-69）
- 缺陷（N-69 登记于 N-63 那一轮；本轮由子代理实施、我在主线独立复跑与复算）：`claim` 的
  credential-rotation 失败补偿分支把 `provider.destroy` 的异常收成一行 `logger.error` 就继续往下
  —— 照样 `scheduler.release`、照样清 `container_name`、照样写 `status=FAILED`。容器还在吃
  `--gpus device=N`，卡却回了池子。这是「释放由命令返回码背书而不是由 runtime 事实背书」的第四处
  （N-63 stop 重试、N-77 reconcile 节点不一致、本轮 warm pool；provision 失败那一路＝N-67 仍未动）。
- 三处失明逐条对上：① `_cleanup_failed` 只选 `warm_pool_state == FAILED`
  （`app/services/warmpool.py:236-268`）⇒ 现已扩成 `in_([FAILED, DRAINING])`，
  模块 docstring 承诺的 `READY/其他 → DRAINING → FAILED` 那一跳第一次有了驱动者（`:6-8` 注记）；
  ② `reconcile_all` 按 status 跳过 STOPPED/FAILED（`app/services/orchestrator.py:524-527`）⇒
  不放行那一档**不写终态**，所以还看得见；③ `recover_stuck_gpu_allocations`
  （`app/services/scheduler.py:271-283`）只保护 {PROVISIONING, RUNNING, STOPPING} ⇒
  正因如此不能写 FAILED，写了就等于把刚立的准入判据绕过去（新模块里用真函数两档证了这一点，
  含"改成 FAILED 立刻被放卡"的反证）。
- 改法：撤销分支清账分两根轴。归属与凭据（user_id/organization_id/password/ide_url）两极都收回；
  资源那一轴只在 `self.orchestrator._release_admitted(workspace, command_succeeded=...)` 认账后才
  `scheduler.release` + 清寻址字段 + 写 FAILED；不认账就 `error_message` 同时写下触发点、provider
  亲口的回话（`_observed_runtime_state`，只做诊断不参与判决）与 destroy 的错误串，状态原地不动，
  收敛交给 ① 那条 DRAINING 档 → durable DESTROY 真再叫一次 `provider.destroy`。
  `billing.release_hold` 保持无条件：交付没发生就不该继续圈额度（计费轴），资源退不退是另一根轴。
- 我对自己任务书的一处自我更正（子代理读码反驳，我复算后接受）：原书写的是无条件传
  `command_succeeded=False`。读 `app/services/providers/mock.py:71-73`（mock 的 `reconcile` 恒回
  UNKNOWN）与 `tests/test_warmpool_claim.py:373` 的 `CleanupTrackingProvider(MockProvider)` 后确认：
  写死 False 会让「destroy 成功 + provider UNKNOWN」这一档永不放卡（把卡死钉在一张没人用的卡上），
  并翻掉两支常驻绿用例。现按 `_stop_cleanup` 既有的两个消费点同形接线（`app/services/orchestrator.py:366` 成功档
  传 True、`:370` 报错档传 False），并把这一分歧常驻钉成
  `tests/test_warmpool_claim_admission.py::test_claim_abort_releases_the_mock_demo_slot_where_nothing_is_observable`。
- 判据 `tests/test_warmpool_claim_admission.py`（11 个 def / 收集 12 例，参数化那一支两档）＋重判的
  `tests/test_warmpool_claim.py::test_warm_pool_rotation_failure_only_when_admitted`：
  报错＋alive／报错＋unknown 两档都断「分配行还在、卡仍 ALLOCATED、不写终态、container_name 保留」；
  报错但 provider 说 MISSING、destroy 成功、mock UNKNOWN 演示档三档都断「照旧放卡并收敛」；
  两支收敛档断 DRAINING 被扫到且重试真的再叫 destroy、provider 改口后放卡置终态；
  结构判据按 AST 断「撤销分支里的 `scheduler.release` 落在准入谓词之下」，配一份合成不守卫形状
  （两臂都绿的开火控制）与一份真源码变异（锚点门）；扫池选择集同样有合成反证。
- 我在主线独立复算（`git show HEAD:app/services/warmpool.py` 就地换面，跑完按 sha 还原）：
  新模块 `FF...FFF.FFF`（12 例里 8 开火），一手读数
  `destroy 抛错、provider 说的是 alive，卡却被放回池子里了（一卡双跑）`
  `{'alloc': False, 'gpu_free': True} != {'alloc': True, 'gpu_free': False}`／
  `DRAINING 不在扫池子的选择集里（改前形状）：{'destroyed': 0, …}`／
  `{'claim': 1, 'judgments': 0, 'releases': 1, 'guarded_releases': 0}`；
  既有 `test_warmpool_claim.py` 16 支在改前全绿（它钉的是"无条件放卡"，改前当然满足）。
  如实标注开火集合里的两支（真源码变异那两支）报的是 `锚点不是恰好一处（0）` ——
  那是落地门在说"改前源码里没有这段锚点"，不是尺子看见缺陷；尺子的牙在合成控制那一支上，
  而它两臂都绿。合规三档（provider 说 MISSING／destroy 成功／mock 演示档）两臂都绿，是护栏不是反证。
- 被我这两轮改到的一处连带更正（协租户式改动的必然代价）：子代理的任务书与代码注释都写着
  「清了 `container_name` 就永远停不掉，因为 `DockerProvider.stop` 只按这一列找容器」——
  那是 N-78 之前的事实；`_name()` 落地后 stop 会回退到约定名，后果降级为"这一格对应哪个容器的事实
  被抹掉、重试只能靠推断"。已把 `app/services/warmpool.py:413-416` 与测试 docstring 两处按现状改写，
  并把行号指针重解到 `app/services/providers/docker.py:305-307`（stop）与 `:60-68`（`_name`）。
- 邻面（我自己跑）：新模块＋`test_warmpool_claim`／`test_warmpool`／`test_streaming_lifecycle`／
  `test_durable_ops`／`test_worker`／`test_provision_rollback`／`test_stop_release_admission`
  合跑 98 支 rc=0；`ruff check app tests` All checks passed；`mypy app` Success（41 files）。
- 未证实：真 docker/k8s 守护进程下的两极没跑（本轮禁容器；常驻用例用的是自述 runtime 事实的替身，
  mock 只会说 UNKNOWN —— 属机制限制）；`recover_stuck_gpu_allocations` 与本次 commit 在同一事务里
  的真实交错窗口没有常驻并发用例（那一半仍在 `tests/test_postgres_concurrency.py` 的档位里）；
  `error_message` 拼接后的长度不截断是按 `app/models.py` 的 `Text` 列判定的，非实测。
  本轮**没有**新开登记项（并发窗口属既有 PG 档，不重复立项）。
- 计数面 711→723，门禁 G0.78。

### provision 失败那一路的放卡也要 provider 认账（N-81，闭登记项 N-67）
- 缺陷（N-67 登记于 N-63 那一轮；本轮由子代理实施、我在主线独立复跑与复算）：
  `_execute_provision` 的补偿回滚用 `contextlib.suppress(Exception)` 吞掉
  `provider.destroy` 的异常（`app/services/orchestrator.py:264-280`（`_execute_provision` 全体 :185-314）），异常信息就地丢掉；
  随后 `_fail` 无条件 `scheduler.release` 并清 `gpu_id/gpu_index/gpu_name` ⇒
  容器没销毁成功，卡照样回池、还写成终态。这是「释放由命令返回码背书」家族的第二实例
  （stop 重试＝N-63、节点不一致＝N-77、warm pool＝N-80、provision 失败＝这里）。
- 更正我自己在登记项里写错的前提：原文写「`_fail` 的三个调用点各自的 runtime 形状要先量清」。
  实测 `WorkspaceOrchestrator._fail` 只有 `app/services/orchestrator.py:301` 一处调用（定义在 :316-404）
  （`app/services/deployment.py:297/304/310` 的 `_fail` 是另一个类的方法，与本仓账本无关）；
  真实的两档是同一调用点上的 `terminal=`（由 `_failure_is_terminal` :36-42 决定：
  同步 `_start` 路径 = 终态；worker 还会重试 = 非终态）。
- 改法：补偿域不再吞信息 —— 记 `cleanup_attempted`/`cleanup_error` 后原样 re-raise（原始异常
  不被盖掉，ADR 0002 的边界不破）；`_fail` 新增**必填**关键字 `command_succeeded`
  （「没进补偿域」按 lenient 档处理，与改前一致），release 与清列整体搬进
  `if self._release_admitted(...)`（`app/services/orchestrator.py:367`）；不放行档不放卡、不清列、
  状态写 **STOPPING** 而不是 FAILED/QUEUED，`error_message` 同时带上原始失败＋provider 亲口的
  回话＋destroy 的错误串，并自己 `db.commit()`。`billing.release_hold` 留在门外（计费轴与资源轴分家）。
- 为什么是 STOPPING（子代理把两个候选都量过，我核过它的读数）：留 PROVISIONING 两头都不成立——
  幂等预筛会让 durable 重试变成 no-op 却记 SUCCEEDED，而 `reconcile_all:675-681` 会把
  PROVISIONING＋ALIVE **adopt 成 RUNNING**（没有端口也没有凭据）；STOPPING 既在
  `recover_stuck_gpu_allocations` 的保护集内（`app/services/scheduler.py:272-280`，我另用真函数在两档上证过：
  stopping/provisioning/running 留卡，queued/failed 无 active operation 就被强制放卡），
  又已被 `reconcile_all:687-692` 的 STOPPING＋ALIVE 分支真去再叫一次清理。
  两档收敛读数：`stop_calls` 1→2→3 三趟，provider 改口那趟才 `stats["stopped"]=1`；
  DESTROY operation 轴 `destroy_calls` 1→2（仍留卡）→3（放卡＋DELETED）。
  我据此定档：**不给交互式 provision 孤儿另加一个 DESTROY 入队器** —— 重试驱动位点已经存在且被量到，
  再加一个就是同一跳两个司机。
- 判据 `tests/test_provision_release_admission.py`（17 支）＋我在合流后补的 2 支：
  destroy 抛错＋provider 自述 alive/unknown 两档都留卡留列不写终态；destroy 抛错但 provider 说
  MISSING（K8s 对已删 Deployment 的 404）与 destroy 成功的 missing/unknown 两档照旧放行；
  `command_succeeded=True`＋仍 ALIVE 那一档钉「命令成功不替 runtime 背书」；retryable 档
  terminal=False 放行时仍停 QUEUED（不抢 N-79 的立场）；原始异常形状单独钉
  （`pytest.raises(ProvisionBoom)` 且消息里没有补偿错误串、`__cause__ is None`）；
  结构判据按 AST 数「release 与清 GPU 列是否落在准入谓词之下」
  （`guarded_releases`/`guarded_gpu_clears`）＋合成不守卫形状的开火控制＋真源码变异控制。
- 我这 2 支补的是子代理如实报的那格未证实：`_fail` 里的判据没有被 `reconcile` 自己抛保护，
  新异常会盖掉正在处理的失败。修法放在**判据本体**（`orchestrator.py` 的 `_release_admitted`
  里 try 住 `provider.reconcile`，问不到一律按不放行，ADR 0008 同一口径），而不是在消费位各包一层 ——
  因为 `_stop_cleanup` 的报错档是在 except 处理器里第二次问的，那里漏出去就直接穿出 ADR 0002 边界。
  新判据两档：provision 档「问不到 ⇒ 留卡留列＋STOPPING＋原始失败仍在消息里」，
  stop 档「问不到 ⇒ `stop()` 不外抛＋STOPPING＋原因写的是 provider 的回话」。
  反证：把判据里那层 try 摘掉（锚点命中 1、sha 先变），两支分别红在
  `RuntimeError: engine unreachable` 与 `assert 'provisioning' == 'stopping'`；还原 sha `ed9a1d19…`。
- 连带改判两处既有断言（都属消息面，不是判决面）：不放行的原因原先写死
  「runtime still reported ALIVE after stop」，而没放行其实有两种子情形（自述还活着／压根问不到），
  只点名一种的消息在另一种场合就是假话 ⇒ 改成引用 provider 亲口值
  （`release not admitted: provider reports runtime alive`），`tests/test_stop_release_admission.py:243,:408`
  的 needle 随之从 `"ALIVE" in …` 改成 `"provider reports runtime alive"`。
  另外我第一版把 guard 写在调用位（`admitted = ...` 再 `if admitted:`），被结构判据读成
  `guarded_releases=0` 而红 —— 这条尺子钉的是「调用直接坐在 If 测试位」这一种拼写；
  我没有去放宽尺子，而是把异常安全搬进判据本体（更好的设计，且尺子的失效方向是**误报**不是漏报）。
- 改前复算（`git show HEAD:app/services/orchestrator.py` 就地换面，我自己在主线跑）：
  本文件 17 支里 `FFFF...F.FFFF..FF`＝11 开火／6 放行档与合成控制照绿，一手读数
  `destroy 抛错、provider 说的是 alive，卡却被放回池子里了（一卡双跑）`
  （`{'alloc': False, 'gpu_free': True} != {'alloc': True, 'gpu_free': False}`）、
  `destroy 返回了成功，provider 却说 runtime 还在，卡却回池了`、`assert 'failed' == 'stopping'`、
  `{'judgments': 0, 'guarded_releases': 0}`；两支真源码变异控制报的是
  `锚点不是恰好一处（0）` —— 那是落地门在说改前源码没这段，不是尺子看见缺陷，尺子的牙在合成控制上。
  同一臂里被改判的两支常驻（接线棘轮 `admitted_uses` 2→3、`_fail` 新必填关键字）也各自红一次，
  所以三个文件合起来的改前红数是 13（11＋2）。
- 邻面（我自己跑）：`test_provision_release_admission`／`test_provision_rollback`／`test_worker`／
  `test_durable_ops`／`test_streaming_lifecycle`／`test_stop_release_admission`／`test_k8s_node_truth`／
  `test_warmpool_claim_admission`／`test_warmpool`／`test_api` 合跑 114 支 rc=0；
  `ruff check app tests` All checks passed；`mypy app` Success（41 files）。
- 未证实：真 docker/k8s 守护进程下「destroy 失败之后 provider 会不会自述 ALIVE」没验（本机无卡无档，
  替身自己声明事实，属机制限制）；retryable 不放行档现在写 STOPPING，读者侧
  （`GET /api/workspaces/{id}`）从 QUEUED 变成 STOPPING 的对外语义我没有另开用例钉，
  只钉了「不写终态」这一条；attempts 2/3 不会重进补偿域（`allocate` 先失败），
  真正的再叫位点是 reconcile 的 STOPPING 档 —— 用例里如实写着，不是"修好了"。
  新登记 N-82：`recover_stuck_gpu_allocations` 强制放卡时从不清 `workspace.gpu_id`
  （`app/services/scheduler.py:305-316` 只动 `Gpu` 与分配行），于是 Gpu 行说"没主"、workspace 行说"我占着"；
  而 `orchestrator.py` 的节点不一致检查正是读 `w.gpu_id` 找 reserved node 的。
- 计数面 723→742，门禁 G0.79。

### 启动预授权从「建好了但默认不走」改成出厂就走（N-83，闭登记项 N-72）
- 缺陷不是算错钱，是**一条谁都没走的路径**：§18 把预授权整套建好了（`CreditHold` 圈额度、
  结算转正、超时回收、`reserve_launch` 要求 `available >= minimum_launch_minutes × 60`），
  而 `Settings.billing_enforce_preauthorization` 出厂是 `False` ⇒ 生产部署里这整套一行都不跑。
  于是启动门禁只剩 `check_launch_eligible` 的 `available < 0`（`app/services/billing.py:69`），
  零余额账户可以开机、钱在第一次结算时变负、只靠配额 monitor 兜。
  全仓唯一的 RECHARGE 生产写点是 `app/routers/usage.py:68` 的充值端点（也没有「注册即发额度」这一步）
  ⇒ 新账户可用额恒为 0，也就是**每个新账户**都能白跑一段真 GPU。
- 选型门禁（这轮真做了外部核对，两份原文本机打开读过）：
  ① Vast.ai 计费文档 —— 新算力要先有钱「Vast requires pre-payment of credits for GPU rentals」；
  运行中的实例耗尽时被「Your instances are stopped automatically」，并明确允许一段宽限
  「the system allows a short grace period where your balance may go negative」
  （腾讯云那篇《Billing Modes》只讲按小时结算口径，余额/停机策略指向另一篇《Overdue Payments》，
  故不引它作依据 —— 这一点如实记：未在该页读到超时/隔离时长）。
  ② 上一轮为 N-70 打开过的 docker-py `docker/errors.py`（:93 把 404 单独收成
  `NotFound(APIError)`）说明的是同一件事：一个默认值能让一套已建好的机制整体变哑。
  ⇒ 择一定档：**启动前圈额度（把默认打开）＋ 运行中由 monitor 兜（已有那半，正好对上宽限）**。
  候选二「只把 `available < 0` 改成 `<= 0`」被否：它仍留着一台空转的预授权机器（默认档照旧不走），
  且实测同样要动一批夹具；候选三「只翻默认、不发体验额度」被否：实测新增红 40 支，
  说明整个测试语料（以及 demo 流程）默认假设「注册即可跑」，不发额度就是把它变成"注册后什么都干不了"。
- 改法：`billing_enforce_preauthorization` 出厂改 True（演示部署显式设 false 即回旧行为）；
  新增 `billing_signup_credits = 300`（恰好＝`minimum_launch_minutes × 60`），
  `POST /api/auth/register` 注册时向**个人**池记一笔 RECHARGE，幂等键 `signup:<user_id>`。
  只进个人池是 N-71 的口径 —— 组织池只数 `user_id IS NULL` 的无主行，注册时把
  `organization_id` 一起写上就等于复现 N-65 那个双主行。
  `BillingPolicy` 构造默认**故意保持** False：几十个夹具直接 new 它，改它等于把一堆用例静默换档；
  要钉的是"生产那一个实例走的是 Settings 的值"，用 identity 判据（§8 立的规矩）。
- 判据 `tests/test_launch_pricing_default_is_enforced.py`（9 支）：出厂默认＝True（读 `FieldInfo.default`，
  不是抄一个字符串）；`deps.billing.enforce_preauthorization is settings....`（恒等，专治"字段存在但
  对象用自己的默认"）＋另两格同参数；构造默认仍是 opt-in 的另一极；注册后余额==体验额度且只有一笔；
  带机构名注册时那一笔的 `organization_id` 必须为 None（前提用注册响应里的 `user.organization_id` 自证）；
  把体验额度调成 0 ⇒ 启动被 402 拒且原因是预授权那一档（这一支是"默认开着"的行为证明，
  光有字段值不算）；有额度照样 201（合规侧，防"默认开了＝谁都开不了"）；
  结构判据按 AST 只认幂等键以 `signup:` 开头那次 `record(...)` 的归属参数，
  配四种写法（写 org／漏 user／合规／充值那笔不开火）。
- 连带改判（三处，全是消息/夹具面，不是判决面）：
  `tests/test_billing_policy.py::test_preauthorization_disabled_by_default_allows_zero_balance`
  → 更名 `..._off_in_an_explicit_deployment_allows_zero_balance`（它测的仍是"显式关档"那一极，
  但"默认"两个字在 N-83 之后是假话）；`tests/test_api.py::test_standalone_user_usage_balance_matches_ledger`
  从硬编码 0/500 改成"端点余额 == 账本之和"的相对式（用例主题本来就是两边对得上）；
  `tests/test_isolation.py` 里「B 的账本必须是空表」改成「B 只看得到自己那一笔」
  —— 隔离要断的是 A 的 1000 不出现在 B 账上，不是 B 一无所有。
  `tests/test_courses.py::new_user` 用生产同一个 `CreditLedgerService.record` 补注册那一笔
  （数额读 `settings.billing_signup_credits`，夹具里不写魔法数）。
- 改前复算（`config.py` 与 `routers/auth.py` 一起换回 HEAD）：9 支里 4 开火，其中
  `test_factory_default_enforces_preauthorization` 是干净的数值判决（`assert False is True`，
  读的就是 `FieldInfo.default`）；另外 3 支报的是 `AttributeError: 'Settings' object has no
  attribute 'billing_signup_credits'` —— 那是"字段在改前根本不存在"的证据，不是数值判决，
  如实分开写。跑完从 `/tmp/n72_postfix` 还原，9 支复绿。
- 邻面：17 个 launch 相关套件（125 例）改后只剩 1 支红，且它就是**改前基线里同一支**
  `test_workspace_progress::test_wait_status_reaches_running_with_the_worker_thread_blocked`
  —— 单独跑该文件 2 支全绿，红只出现在这个 17 文件子集里。当时把它记成『共库/GPU 种子池  累积占用、整轮认证里它是绿的』，**这句是错的**：随后那次整轮认证（collected 751）判红的就是这一支，读数 `provision:failed(attempts=3, error=No GPU available with >= 8 GB VRAM)`。真因由 N-85 量出来并闭合：该文件自己留了一支未执行的 provision op，正例的 tick 循环把它排掉并占走一张卡，而 `ensure_free_gpus(need=1)` 只看 AVAILABLE 卡数、看不见队列里那些还会去占卡的 op ⇒ 前置条件承诺的是 1 张、实际需要 2 张。另有 `test_config_docs`（.env.example 双向核对：新键必须写、
  写了必须有读者）＋`test_validation_matrix`＋`test_worker` 等合跑 73 支 rc=0；
  `ruff check app tests` All checks passed；`mypy app` Success（41 files）。
- 未证实：真钱包/真充值渠道下的额度语义（本仓只有 demo 端点）；把 300 定成体验额度是**产品口径**，
  我按「刚好等于一次最低启动窗口」定档，若运营要改成别的数，`billing_signup_credits` 一个键即可，
  判据读的都是 Settings 值不写死数；402 之后是否需要引导文案（前端提示去充值）属 UI 面，本轮没做。
- 计数面 742→751，门禁 G0.80。

### 回收器强制放卡必须连格子上的绑定一起清（N-84，闭登记项 N-82）
- 缺陷是**两张权威表互相打脸**：`recover_stuck_gpu_allocations` 删分配行、把 `Gpu` 置
  AVAILABLE 并清 `workspace_id`，却从不碰 workspace 行上的 `gpu_id/gpu_index/gpu_name`。
  而 `reconcile_all` 的 K8s 节点不一致判决正是拿 `w.gpu_id` 反查预留 host 的 ⇒ 卡被强制
  放掉之后若转授他人，那一格拿去比的是**别人**的主机名。这一条是 N-81 那轮的对照表里
  每行都打印 `workspace.gpu_id=kept` 量出来的，当时只登记（N-82）没顺手改。
- 修法取「成对」而不是「补丁」：函数自己判定为孤儿并放掉的那几格，在**同一事务、commit
  之前**用一条 `update(Workspace)` 把三列一起清空；受保护／占用的格不在 WHERE 里。
  记名（`orphan_ids`）必须在 `db.delete` 之前——实例进了 DELETED 态读属性不再可靠；
  两个放卡分支各自先 `select(Gpu.workspace_id)` 再记名，因为 UPDATE 之后无从回看。
  清列放在 commit 之后会留下「卡已放、列还指着它」的窗口，崩在那儿就是新漂移。
- 选型门禁跳过理由：单模块一致性修复、无新依赖、无新状态机，判据是仓内既有不变量
  （「分配权威放卡 ⇒ 绑定成对清」），不需要外部候选对比。
- 判据 `tests/test_recover_gpu_column_drift.py` 24 支：行为档按 workspace 状态逐档核
  「放卡与清列成对」（FAILED／QUEUED／STOPPED 必须成对，PROVISIONING／RUNNING／带 active
  operation 的格一列都不许动）；结构档按 AST 数 `release_sites`／`clear_sites`／
  `unpaired_release_paths`，另钉「清列在 commit 之前」「范围只由记名集合决定」。
  六支变异控制各自只翻一格：PRE-FIX→`unpaired=3`、少记名→`unpaired=1`、漏列→`cols=2`、
  全局清→`scope=0`、名单混进保护集→`leak=1`、挪到 commit 之后→`before_commit=0`。
- 我在主树亲手复算（子代理实现，判据进门禁档）：把 `app/services/scheduler.py` 换成
  HEAD 那份 → `16 failed, 8 passed in 3.21s`；换回 → `24 passed in 3.16s`，
  还原后 sha `acb4be6b…` 一致。邻居合跑（worker／gpu_pool_guard／scheduler／
  stop_release_admission／k8s_node_truth／warmpool_claim_admission／provision_rollback／
  gpu_single_authority／runtime_readiness／streaming_lifecycle 共 11 个文件）111 点全绿、
  零 F 零 E；`ruff check app tests` All checks passed；`mypy app` Success（41 files）。
- 一处**前提为假**由子代理读出来、我复核后如实登记：任务书里「`_fail`／`_finalize_stop`／
  warm pool 已各自清自己那三列」只有 `_fail`（`app/services/orchestrator.py:386-388`）成立——
  `_stop_cleanup` 的放行档（step 4）与 warm pool 认领撤销的放卡档
  （`app/services/warmpool.py:400-405`）都不清列，主树 `orchestrator.py` 也只有 `:315-317` 一处清。
  这两处产出的漂移**回收器看不见**（既无分配行、卡也不再指向那格），故本轮不变量的作用域
  限定为「回收器自己放掉的格」，缺口登记为 N-86 而不是顺手改。
- 未证实：ghost 分配（卡 ALLOCATED 但没有 workspace 行）仍会让卡钉在那儿，两条 UPDATE
  都碰不到它——本轮未扩权，属未完成项而非机制限制；`reconcile_all` 端到端在真 provider
  下把清列后的状态收敛没跑（真 K8s pod 侧属未做）。

### 台账的表格面被自己的记账脚本粘坏了，补一把看得见接缝的尺（N-87）
- 缺陷是我自己造出来的：多点锚点补丁把新行**不加换行**地拼在上一行之后
  （`add(path, 带换行的整行, 带换行的整行 + NEW_ROW)`），`NEW_ROW` 于是与它下面那行物理上
  粘成一行。`docs/ACCEPTANCE_GATES.md` 连着四轮这样落盘：G0.76（c680e12）吞掉了
  `## G1 物理 GPU 主机预检（…）` 这行标题，G0.77／G0.79／G0.80 依次把 G0 表的后续行推到
  标题之后、把 G0.79／G0.80 塞进下一张表的表头与分隔行之间——渲染出来的是一张错位表。
- 第二层损失更隐蔽：**我用来复核面的读数本身瞎**。`[ln for ln in lines if
  ln.startswith("| G0.")]` 这种前缀取数看不见被吞掉的东西，所以「GATES G0.80 行 1」
  这类读数一路全绿而面是坏的；既有门禁同理——`docs_row_order`／`docs_state_rows`
  都按行首前缀取数，粘连只让它们少看一支，不会让它们判红。
- 修法：按结构边界重排那一段（把 G0.76 行与标题之间的接缝插回换行、G0 行按编号连续、
  标题独占一行、下一张表的表头与分隔行紧随其后），并证明**内容守恒**——去掉全部换行后
  与改前逐字节相等，且第二次挪位用「逐行多重集不变」核。全仓同类粘连普查：
  CHANGELOG／CURRENT_STATE／OPERATIONS／ARCHITECTURE 均为 0 处。
- 判据 `tests/test_docs_table_rows.py` 4 支，尺子是纯函数（文本进、接缝出），真文件与
  合成反证喂同一把：两支核真面（门禁目录每支逻辑行独占一行且行尾收 `|`；状态件 §2 不吞
  标题），两支核尺子（吞标题／吞下一支行两种真实形状各点名一次，合规排版沉默）。
  历史面复算：b3ecdfb 接缝 0／截断行 0（沉默）→ c680e12 截断行 [87] → 79e4402 接缝 1 →
  dcf7837（HEAD）接缝 4＋截断行 [87]，改前每一档都开火。
- 记账纪律同步改：OPERATIONS §7 新增一条「补丁里每支新行必须自带行尾换行，写后按
  `row_seams`/`unterminated_rows` 这两把尺复算」；本脚本自身的每条 add() 都以 \n 收尾。
- 未证实：Markdown 渲染效果没有用浏览器量过（判据核的是物理行与逻辑行的对应关系，
  这是缺陷的成因面）；`docs_row_order` 那把既有尺是否还会被别的形状绕过，本轮没扩测。

### 计费 GPU 秒的 Counter 改吃「账本增量」，不再吃「这一段值多少秒」（N-88，闭登记项 N-75）
- 缺陷形状是 N-74 收尾时量出来登记的那一半：`gpu_seconds_total` 是 **Counter**，而
  `_finalize_stop` 拿 `_settle_run` 的返回值（＝账本认下的**那一段**秒数）去 `inc`。幂等键
  让同一运行段只扣一次钱，但重放时那一段的**值**照样是 30 ⇒ counter 再加 30。探针实测
  （真对象、默认注册表）：stop 重试 `+30/+30`、合计 60，而 `settled_gpu_seconds` SUM=30、
  投影列=30；`reconcile_all` 那一路同样 `+30/+30/60`。钱扣一次、暴露量翻倍，看板上就是
  「这个用户烧了两倍的卡」。
- 选型门禁（两份原文本轮由我本机重新取回并读过，不是转述）：
  ① prometheus.io/docs/concepts/metric_types/（curl 220027 字节）
  「A counter is a cumulative metric that represents a single monotonically increasing
  counter whose value can only increase or be reset to zero on restart」＋
  「Do not use a counter to expose a value that can decrease」；
  ② prometheus.io/docs/practices/naming/（204747 字节）「an accumulating count has total
  as a suffix, in addition to the unit if applicable」⇒ 现名 `gpu_seconds_total` 合规，不改名；
  ③ 本机 `prometheus_client` 0.26.0（外部包，源码不在本仓）的 `metrics.py` 第 337-341 行：`inc(amount)` 对负数抛
  `ValueError('Counters can only be incremented by non-negative amounts.')`，且 Counter 没有
  `set`（Gauge 有）；`app/metrics.py:34` 就是 `_total` 后缀那条规则的代码。
  ⇒ 择一：**保持 Counter，喂给它账本的净增量**（不引新依赖）。候选 B「改 Gauge＋`set(账本和)`」
  被实测否掉一半——子代理把声明换成 Gauge 后 `test_observability.py`／`test_metrics_spec.py`
  **全绿**，也就是 §8 那把门只核族名与标签、看不见类型；这条不是否决 B 的理由（B 会把
  「累计量」降级成「当前值」，与 Counter 的语义和 reset-on-restart 假设都不合），但它暴露了
  一个真洞，本轮顺手补成判据（下面第三条）。候选 C「每次抓取现算的 custom collector」被
  `tests/metrics_spec.py:95-98` 的断言否掉：非 `MetricWrapperBase` 的收集器会被归进
  `python_/process_/bridge_` 那一组断言。
- 改法：`_settle_run` 变薄壳（**公开契约不动**：仍返回账本认下的段值，`capture_hold` 与
  N-74 的 C1 极都依赖它）；新增 `_settle_run_delta(db, ws) -> (booked, delta)`，结算前后各读
  一次 `CreditLedgerService.settled_gpu_seconds`（复用 N-74 那份口径，不重写 SUM），
  `delta = after - before`；投影列由同一个 `after` SET（少一次查询，语义不变）。首结算
  `delta == booked`；重放行已存在 ⇒ `after == before` ⇒ `delta == 0`。账本 append-only，
  两次读数之间只有 `settle_workspace_run` 写，所以 delta 不为负；真为负就让 `Counter.inc`
  抛 ValueError——**宁可红，不静默夹平**。
- 判据 `tests/test_gpu_seconds_metric_delta.py` 14 支：stop 重放只加一次、第二次 finalize
  加 0 但段值仍是 30、`reconcile_all` 那一路重放也只加一次、hold 转正仍读段值（不是 delta）、
  连续两段各加各的、零秒段加 0；结构档钉「counter 只在一个点被抬」（AST）；另两支把**类型**
  钉住（Counter 而非 Gauge，且族名/标签不变）——这条就是上面那个「换 Gauge 全绿」的洞补上的门。
- 我在主树亲手复算（判据进门禁档）：`orchestrator.py`＋`metrics.py` 一起换回 HEAD →
  `FF..F........F`，4 支开火，名字逐一点到（`test_stop_retry_increments_the_counter_once_for_one_segment`／
  `test_second_finalize_of_the_same_segment_reports_zero_but_keeps_the_segment_value`／
  `test_reconcile_all_path_increments_once_even_when_it_replays`／`test_hold_capture_still_uses_the_ledger_segment_value`），
  一手读数 `重放同一段把 counter 又加了 30.0 / assert (60.0 - 30.0) == 0`；换回后与
  13 个邻面文件合跑 **170 点全绿 rc=0**（含 N-74 的 `test_settled_projection.py`、
  `test_credit_holds.py`、`test_observability.py`、`test_metrics_spec.py` 与本轮 N-84 的
  `test_recover_gpu_column_drift.py`），`ruff check app tests` All checks passed，
  `mypy app` Success（41 files）。改前也通过的 5 支（连续两段／零秒／结构与单调性）在文件里
  标注为护栏而非反证。
- 两条由本轮量出、**不在本轮修**的缺口进登记表：N-89（DESTROY 与 reconcile RUNNING→FAILED
  两条结算路实测给 counter 加 0 而账本入了 30 秒——HEAD 与本树读数相同，是既有少计，
  不是本轮引入）；N-90（`reconcile_all` 的 per-workspace 循环里没有异常边界，:708-714 那一路
  `scheduler.release` 抛错会穿出整趟 reconcile）。
- 未证实：只从默认注册表读 `gpu_seconds_total`，没走 `/metrics` 那个 HTTP 面，也没覆盖
  multiprocess 模式（测试里不用该模式，属机制限制）；License 一格我用自己的读法没读到
  （`importlib.metadata` 的 `License` 字段返回 None，版本能读到 0.26.0），故本轮不引该结论。

### 共享测试库的 GPU 池前置改成「净额」：空闲卡数要扣掉队列里欠的账（N-85）
- 症状是认证台上那一支反复变红的用例：整轮 `collected 751` 唯一一条红就是
  `tests/test_workspace_progress.py::test_wait_status_reaches_running_with_the_worker_thread_blocked`，
  一手读数 `provision:failed(attempts=3, error=No GPU available with >= 8 GB VRAM)`。
  上一轮我把这句写成"共库/GPU 种子池累积占用、整轮认证里它是绿的"——**那句是错的**（本轮开头更正），
  因为"它是绿的"从来没人核过，而这一次它就是红的来源。
- 机制不是"运气差"，是**夹具的承诺本身在说谎**，用一支一次性诊断插件量出来的：单跑该文件，
  正例结束时池子是 `gpu 总=8 可用=6 分配行=2 op=2 未终态=0`，两格 workspace **都在 running** ——
  包括那支"被动档"故意留成 queued、没执行过的 provision op。也就是说：正例自己的
  `worker.tick_once()` 循环（`tests/workspace_progress.py:58`）替兄弟用例排掉了那支 op，
  而那次排空吃掉一张卡。也就是说：本文件实际需要 2 张，`ensure_free_gpus(db, scheduler)` 按默认
  `need=1` 只要 1 张就宣布满足；`tests/conftest.py:42` 那道 autouse 闸又只在
  `count_big_enough(db, 8) == 0` 时动手 ⇒「只剩 1 张可用」是一个合法但致死的到达态。
  N-7 那轮立的规矩（"需要空闲卡的用例显式达成前置"）方向对，**算术错**：
  它数的是 AVAILABLE 行，没数队列里那些"还会去占卡、但还没占上"的 op。
- 修法在共享夹具一处，不在调用点：`tests/gpu_pool.py` 新增 `unfulfilled_provision_ops(db)`
  （`WorkspaceOperation` 里 `operation_type=provision` 且状态 ∈ {pending, running, retrying}，
  枚举取 `app/models.py:97-102`，`app/models.py:480,511` 说同一格同时至多一个活动 op），
  `ensure_free_gpus` 的触发与判决都改成 `空闲数 − 欠账 ≥ need`，失败消息把**两个数都写出来**
  （下一个读者不必再去猜是谁拿走的卡）；返回值语义**不变**（仍是原始 AVAILABLE 数），
  所以 `tests/test_api.py:37`／`tests/test_gpu_admin.py:36`／`tests/test_workspace_credential.py:99` 三个调用点
  一字未动。`tests/conftest.py` 那道 autouse 闸同改净额口径（是原"raw 0"档的严格超集）。
- 判据 3 支进 `tests/test_gpu_pool_guard.py`：欠账吃掉最后一张时 `ensure_free_gpus` 必须不许原样返回／
  净额本来就够时不许误触发（防「只要有队列就回收」的假合规）／失败消息必须同时报两个数。
  数欠账用的是一份**就地独立重数**（`_queue_debt`），不 import 被测的那个新函数——
  否则改前那一档只会得到 ImportError，那不是开火。
- 我在主树亲手复算：把 `tests/gpu_pool.py`＋`tests/conftest.py` 换回 HEAD（sha 等于
  `f3cdc7e7…`／`1450f5b3…`）→ 同一批判据 `2 failed`，一手读数
  `原始空闲 1 张 − 未执行 provision op 1 个 = 净额 0 < need=1，ensure_free_gpus 却原样返回 1`；
  换回后 `tests/test_gpu_pool_guard.py`＋`tests/test_workspace_progress.py` 合跑 10 点全绿 rc=0，
  六个邻面文件（gpu_pool_guard／workspace_progress／gpu_admin／workspace_credential／scheduler／worker）
  合跑 45 点 rc=0；`ruff check tests` All checks passed。子代理那边另有一组一手读数：
  受控排水（真 `GpuScheduler.allocate` 挂走 7 张、只剩 1 张）下改前
  `1 failed, 2 passed` 且报错与认证台那条逐字同形，改后同一对 `3 passed`（17.5s → 2.62s），
  并且**真全套跑过 `free=1 + debt=1` 这一档**（`[conftest] … 回收 7 张` 那行不是我造的）。
- 两处由我改判（超出子代理补丁）：`test_gpu_pool_guard.py` 的 `rig` 原先手写"≥2 张"，
  现改成同一套净额口径；`test_ensure_free_gpus_leaves_draining_cards_alone` 的
  `== free_before - 1` 前面补一道**净余量前提**断言。理由：那条等式把"当时恰好没欠账"
  钉成了现状（as-is 断言），一旦真出现"卡停在放不掉"的缺陷态，它会以"DRAINING 判据坏了"
  的面貌报红，把一个真实缺陷伪装成夹具故障；补上named前提后，缺这一档就直说缺这一档。
- 未证实：子代理只开了 76 个模块里的 34 个（其余 42 个由本轮收尾的整轮认证覆盖）；
  另外三个调用点在"净余量不足"这一档只是**传递性**成立（它们调同一个 helper，但没有各自的
  排水档）；`reclaim_gpus` 之外还有没有别的"合法但致死"的到达态（例如 ≥6 张停在
  DRAINING/UNHEALTHY）本轮没有判据——那属于"有卡停在放不掉"的缺陷族，与本条不同轴。

### 断言本体也得走那条异常吸收：registry 卡住记 PENDING，不是记一条代码失败（N-91）
- 认证台这一轮的红不是 GPU 池那支（那条已由 N-85 闭合、整轮没再出现），而是
  `tests/test_docker_provider_integration.py::test_pinned_base_of_the_control_plane_recipe_is_fetchable`
  报 `subprocess.TimeoutExpired: Command '['docker', 'pull', 'ghcr.io/astral-sh/uv:0.12.19@sha256:04d0…']'
  timed out after 300 seconds`。同一台 daemon 上我随后实测：`docker pull` 同一条引用 **7.4s 回 rc=0**、
  `docker manifest inspect` **10.3s 回 1308 字节** —— 也就是 300s 预算被一次外网抖动吃掉，
  而这轮的宿主 load 只有 3.76／10 核，不是 CPU 争用。
- 缺陷不是"超时太短"，是**这一处绕过了本仓已经立好的那份异常吸收**。
  `tests/docker_probe.py` 的模块 docstring 写的就是同一件事（"daemon 卡住时
  `subprocess.run(timeout=…)` 抛 TimeoutExpired，异常穿过前置检查，整档用例以 failed on setup 收场"），
  而四个 docker 档的 `gate_reason()` 都接了它；唯独这条**断言本体**用裸 `_docker("pull", …, timeout=300)`，
  于是它绕过了自己文件里那套已经写好的三档分流（`_pull_failure_action`：第二通道说没有⇒钉错、
  说存在⇒通道故障记 PENDING、两边都定不了案⇒记 PENDING 并打两条读数）。
  全仓扫过同一形状：54 处裸 `_docker(` 里，只有这一处是"替判据本体决定环境读数"的
  （其余是前置——已接吸收——与测试体内真实的容器操作），所以修一处而不是加一层通用兜底。
- 改法：这两次调用改走模块内的 `_docker_probe`（吸收 TimeoutExpired ⇒ rc=124 ＋
  「命令超时（300s）」），读数于是进到既有的三档分流里去定案。**没有**改判据强度，也**没有**
  改成 `manifest inspect`（那会把"构建用的那条传输取得到"降级成"registry 认这个摘要"），
  也**没有**加大超时——加大超时是把进度外包给外网，与 N-58／N-59 已定过的口径相反。
  换来的差别是：通道慢的那一轮，这一格从"代码失败"变成"本轮未获证（PENDING）＋两条读数"。
- 判据 4 支进 `tests/test_docker_gate_hardening.py`（该文件本来就管这件事，只是覆盖面缺了断言本体）：
  卡住的 pull 必须落进"定不了案 ⇒ 记 PENDING"那一档（断 `GATE_SENTINEL` 在原因里）；
  反向极性——daemon 卡住但第二通道逐字节说"没有"时**必须红**（不许把坏摘要洗成跳过）；
  合规侧对照——健康 pull 既不跳也不红（防止把修复写成无条件跳过这块橡皮章）；
  分流第一根轴——吸收器留下的「命令超时（300s）」必须被 `registry_reading` 判成 `no-answer`
  （判成 `absent` 就等于把慢通道当存在性主张，判成 `unreadable` 会让一次抖动落 red_undecided）。
  引用串一律由配方现取（`_base_refs` ＋ `_is_pinned`），判据里不写死镜像。
- 落地证明（我自己跑的两极）：只把那一行换回裸 `_docker` ⇒ 该支当场
  `subprocess.TimeoutExpired … timed out after 300 seconds`，**与认证台那条一手读数逐字同形**；
  还原后 `tests/test_docker_gate_hardening.py` 12 点全绿。`ruff check app tests` All checks passed，
  `mypy app` Success（41 files），全套收集数 796→800。
- 未证实：真把 ghcr 拖到 300s 以外做不到（那是外网条件，不是本机可造），所以我证的是
  「同一异常形状被吸收后落在哪一档」而不是「今晚的 ghcr 会不会再抖」；第二通道
  （`_independent_digest_read`）在这三档里都被替换成桩，真实通道的判别力由同文件既有的
  `test_independent_digest_read_discriminates_present_from_absent` 钉着，不重复立项。

### 放卡的成对清列收进分配权威本身，而不是在每个调用点各写一遍（N-86，闭登记项 N-86）
- 缺陷是 N-84 复算时量出来的：N-84 给回收器补上了「放卡必同时清 `workspace.gpu_id/gpu_index/gpu_name`」，
  但那是把同一条规矩抄成第三遍。剩下两个放卡口 —— `_stop_cleanup` 的放行档与 warm pool 认领撤销档 ——
  只调 `scheduler.release`，不清列 ⇒ `Gpu.workspace_id` 说卡空了、`Workspace.gpu_id` 说它还绑着，
  两张表互相打脸，而回收器只读前一列。
- 选型：跳过外部检索。这不是新机制，是把本仓已经立好的规矩（N-84 的同一条成对性）搬到权威本体上；
  候选只有「每个调用点各写一遍」与「收进 `release`」两种形状，后者是本仓既有的单一权威做法。
- 改法：`Scheduler.release` 用 ORM 取回 holder 后在同一事务里清三列（不用裸 UPDATE，避开与 identity map 打架），
  docstring 写明「三件成对」；调用点不再各自重复。
- 判据 `tests/test_release_pairs_binding.py` 7 支（成对性直接读三列、两个调用点各自走真 release、
  结构判据钉「清列只住在 release 里」）。改前复算 4 failed / 3 passed，一手读数
  「release 之后同一会话仍读到绑定」——这组读数抄自提交 2dd2ba2 当轮实测，本轮记账未复算。
- 未证实：三列之外的绑定事实（如端口表）是否也该同批清，未扩面。

### 抬 gpu_seconds_total 挪进结算本身，入账的两条旁路不再少计（N-89，闭登记项 N-89）
- 缺陷：N-88 把 Counter 改成吃账本净增量之后，抬 counter 的点按设计只剩 `_finalize_stop` 一处
  （常驻判据 `test_the_counter_is_incremented_at_exactly_one_site` 钉着）。但 `destroy()` 与
  `reconcile_all` 的 RUNNING→FAILED 档也各自经 `_settle_running_segment` 入账：探针实测
  两路 `counter +0.0` 而 `settled_gpu_seconds=30` ⇒ 指标说 0，账本说 30。HEAD 与本树读数相同，
  不是 N-88 引入的。
- 选型：跳过外部检索（counter 的语义依据已在 N-88 引过 Prometheus 官方两页）。两条候选
  「在两个旁路各补一次 inc」vs「把 inc 收进 `_settle_run_delta` 本体」，选后者：
  N-88 刚立的规矩是一处口径，补两处就是把分叉写回去。
- 改法：`record_gpu_seconds(after - before)` 挪进 `_settle_run_delta` 的 return 之前；`_finalize_stop`
  退回成只调它。停机那一极的行为逐位不变（同一 `started_at` 条件），所以 N-88/N-64 的 22 支一字未改照绿；
  N-88 那条「app/ 里只有一个调用位点」的尺子钉的是文件，因此它仍然绿——这不是漏钉，位点尺的口径按文件认。
- 判据 `tests/test_gpu_seconds_booked_paths.py` 6 支（两条旁路各钉「入账即抬、数值等于净增量」、
  重放不重复抬、停机那一路读数不变）。改前复算 4 failed / 2 passed，一手读数
  「destroy 之后 counter 只动了 0.0」——抄自提交 e4bae32 当轮实测，本轮未复算。
- 未证实：`metrics.py` 里其它 Counter 是否也有「入账了但没抬」的同形旁路，未普查。

### 逐格收敛各有一个异常边界，一格的故障不再中止整趟 reconcile（N-90，闭登记项 N-90）
- 缺陷：`reconcile_all` 的逐格循环里只有 `provider.inspect` 那一小段有 `try`；MISSING 档的
  `_settle_running_segment` 与 `scheduler.release` 两次调用没有。任一格抛错就中止整趟，
  后面的格这一轮没人看——与 ADR 0002 给 provider/scheduler 边界立的规矩（边界内异常转成原因、
  不外泄给调用面）正相反。
- 选型：跳过外部检索（修法由本仓既有边界规矩决定，形状取自同文件 `_stop_cleanup` 的分档写法）。
- 改法：循环体原样搬进 `_reconcile_one(db, w, stats)`，外层每格一个 try；
  **每格各自 commit** —— 只在循环末统一 commit 时，一格的 `db.rollback()` 会把前面已收敛格子的判决
  一起退回（这一条不是推理，是判据第一轮就红了才发现的）。`stats` 增加 `errors` 计数并写进日志。
- 判据 `tests/test_reconcile_cell_isolation.py` 5 支（一格失败其余格照看、errors 计数、
  已收敛格不被回退、两种合成对照）。改前复算 4 failed / 1 passed，一手读数 RuntimeError 穿出整趟
  ＋ errors 缺席——抄自提交 e737378 当轮实测，本轮未复算。
- 未证实：per-cell commit 对写放大的影响（每格一次事务）未测；本轮按「判决不能互相退」定档。

### 会话收尾收掉本进程遗留的 test-*.db，别的一律不碰（N-95）
- 缺陷是量出来的卫生问题，不是算错钱：N-31 给测试库名加了 pid 后缀，解决「两个 pytest 并跑互相清库」，
  但每个模块级 `ENGINE` 都留下一份文件，而 `conftest.py` 的会话收尾只删它自己那一份
  `test-embodiedcloud-<pid>.db`。2026-09-28 实测仓根堆到 **3497 个 test-*.db / 1.59 GB / 316 个不同 pid**；
  一整轮跑完给同一个 pid 留 28 份（316 个 pid 的中位数 11 份，即多数趟中途崩或被掐）。
- 选型：跳过外部检索（pytest 自己的 `tmp_path` 管不到本仓自命名的这一族库文件）。
  候选「按 mtime 扫」被否：它会把并发跑的活库一起删掉，正是当年加 pid 后缀要防的故障。
- 改法：`dbfiles.sweep_own_test_dbs()` 只删文件名尾部 pid == `os.getpid()` 的那些，
  扫 CWD 与 REPO_ROOT 两个落点（`db_url` 用相对路径而 `db_path` 用 REPO_ROOT，CWD≠仓库根时不是同一目录），
  `OSError` 跳过不当失败；挂在 `pytest_sessionfinish`。崩溃那一趟仍归 `make clean`。
- 判据 `tests/test_test_db_sweep.py` 6 支，危险方向排在效率之前：别人的 pid 必须原地不动且字节相同；
  `pid_of` 形状表（含 `test-pg16-42.db` 这种名字中间带数字的）；两个根各删一次；
  接线由 AST 尺钉（写出来没人调等于没写）。
- 实测（同支用例换树并排）：`tests/test_billing_policy.py` 在改前树 dcf7837 遗留 1 份、
  在改后树（main）遗留 0 份 ⇒ 清扫既有效又不是恒真。
- 未做到：那 3497 份历史遗留本轮没批量删（多数 pid 已死，但删别人跑出来的库不在本轮半径内）。

### 存在但没在跑的 runtime 不再被读成缺席（N-96，闭登记项 N-79）
- 释放准入在清理命令**失败**那一档只认 MISSING（`app/services/orchestrator.py:477-509`），
  所以 provider 只要把 present-but-not-running 说成缺席，卡就从还活着的 runtime 底下放走（一卡双跑）。
  两处：① `k8s.reconcile` 只读 `available_replicas`，docstring 承诺的「0 副本 → MISSING」代码从来没做，
  pod Pending（拉镜像／等 device-plugin 分 GPU）被读成 MISSING；② `docker.reconcile` 的兜底把
  **所有**非 running 态落到 MISSING，`created`（还没 start 过的对象）与 `removing` 一起被说成缺席。
- 前提更正（本轮本机 daemon 实测；登记原文的一半是假的，先更正再记账）：原措辞写
  「restarting/paused/Pending 被判成缺席」——`inspect` 先读 `Running` 标志（`app/services/providers/docker.py:479`，
  改前同一格是 :474），引擎对 paused 与 restarting 都自述 `Running:true`，这两档改前就已经是 ALIVE，
  无需修改。承重的只有 K8s Pending 与 docker 的 created/removing。
- 选型门禁（四处原文本机打开或取回并逐字核对）：
  ① moby `api/types/container/state.go` 定义七个状态常量，`StateCreated` 的注释是
  "created, but not (yet) started"；`api/types/container/container.go:76` 的字段注释把枚举原文列全
  （created / running / paused / restarting / removing / exited / dead）。
  ② kubernetes/website `content/en/docs/concepts/workloads/pods/pod-lifecycle.md` 第 114 行
  （本机由 raw.githubusercontent.com 取回）：Pending 是「已被集群接受、但容器还没起来」，含等调度与拉镜像的时间。
  ③ 本机 `.venv` 实测 kubernetes SDK 31.0.0 的 `V1Deployment.attribute_map` =
  {apiVersion, kind, metadata, spec, status} ⇒ 换用 `read_namespaced_deployment` 不更贵，只是不再少读退役证据。
  ④ N-91 那轮打开过的 docker-py `errors.py`（把 404 单独收成 NotFound）说的是同一件事：
  一个默认档能让整套机制变哑。
  ⇒ 定档：**缺席只能来自引擎亲口说的话**（404／自述 exited·dead／自己把副本缩到 0），
  问不到与「还没起来」都算 UNKNOWN；docker 侧用白名单而不是兜底，枚举将来多第八个态不许默认算缺席。
- 判据 `tests/test_runtime_presence_not_absence.py` 20 支，两极都钉：k8s 五档（Pending 非缺席／
  0 副本→MISSING／0 副本赢过 stale available／available≥1→ALIVE／404→MISSING／其它 API 失败→UNKNOWN）、
  产品级放行与不放行各一支、docker 实测表参数化（含引擎枚举外的态）、
  AST 尺两条 clause（C1 判决落点／C2 承诺与实现分叉）各有独立开火对照与合规控制，真语料 splice 会翻红。
- 代价如实登记（另立 N-98）：Pending 改 UNKNOWN 后失败档拒绝放卡，而 STOP 打满 `MAX_ATTEMPTS=3`
  之后进程内没人再试——这是拿「一卡双跑」换「一张卡可能被钉住」，与本仓既定立场一致
  （比较 `_fail` 里 STOPPING+protected 的两条理由，`app/services/orchestrator.py:333-345`），但缺一个周期驱动者。
- 改前复算（就地 splice 改前函数体，还原后逐字节比对一致）20 支里 9 开火，读数是判决不是崩溃
  （`AssertionError: Pending 被判成缺席：missing`）。邻面 12 文件 147 支 rc=0；ruff/mypy rc=0。
- 未证实：`dead`/`removing` 是瞬态、本机造不出来（这是没做到，不是没找到），按 moby 两处枚举推理并标推理档；
  真 GPU 集群上 Pending 的收敛没跑（无集群，属外部条件）。

### 入账过的运行段不再被读者各算一次，第三个读者（前端）也收进同一谓词（N-97，闭登记项 N-76）
- 可达窗口：`destroy()` 先结算（`app/services/orchestrator.py:615` → `_settle_running_segment` :594-598，内部已提交），
  随后 `provider.destroy` 抛错并原样上抛让 DESTROY 重试（:623-629）⇒ 库里留下一行
  RUNNING＋已入账＋`started_at` 未清。`accumulated_seconds` 是账本投影（N-64），而读者还在它外面
  再加一次墙钟 live ⇒ 实测 QUOTA 门禁读到 60、同一 workspace 的账本 SUM 只有 30。
- 选型门禁（两处原文本机取回并逐字核对）：
  ① Microsoft 市场按流量计费 API FAQ 的「What happens when you send more than one usage event in the same hour?」：
  "If more than one usage event is emitted for the same hour, any subsequent usage events are dropped as duplicates."
  ② AWS Marketplace `MeterUsage` 参考：对 Timestamp 取整后相同的请求 "the API is idempotent and returns the
  metering record ID"，而同键不同量报 `DuplicateRequestException`。
  ⇒ 借的是这两条形状而不是它们的实现：**一段用量有身份键**，且「这段算过没有」是一次可点查的事实；
  `DuplicateRequestException` 恰好说明为什么键必须喂原始列值——归一化过的串是另一把键，点查必然落空，
  双计原样留在（这条陷阱由 C5b 钉住）。候选二「随结算推进一个水位列、live 从水位起算」被否：
  要加列与迁移，而且它仍是第二套口径；候选三「读者改读账本 SUM 的差额」被否：差额分不开「这一段」
  与「这一段的重放」。
- 改法：全仓唯一键模板 `ledger.usage_idempotency_key`；唯一口径 `ledger.workspace_seconds_used`
  （投影＋仅当未入账的 live），`app/routers/usage.py` 与 `BillingPolicy.course_usage_seconds` 都改为调它
  （列表端点用一条 IN 取键，不做 N+1）。第三个读者在浏览器里——`app/static/app.js` 自己算
  `accumulated_seconds + live`（两秒一轮重新渲染，数字要接着跳），服务端给的数管不到它，管得到的是谓词：
  `WorkspaceOut` 新增 `usage_segment_booked`，赋值位点全仓唯一（`ledger.mark_usage_segments`），
  7 个返回 WorkspaceOut 的路由都带上，前端两处 live 只在它为假时才加。该属性是非映射的
  （SQLAlchemy 只认 `Mapped[...]`，实测列与 mapper 都不含它）。
- 判据 `tests/test_live_usage_not_double_counted.py` 22 支：C1 窗口里三个数（门禁／配额边界两极／真 HTTP）、
  C2 未入账照加（防修过头）、C3 重启后的新段照加（杀掉「有 USAGE 行就跳过 live」的捷径）、
  C4 唯一定义与读者不再抄投影列（AST，各带必须开火的合成对照）、C5 键的往返真落库回读、
  C6 前端旗标（HTTP 一请求两极＋JS 落点尺＋路由接线尺＋唯一赋值尺，分母都现算）。
- 改前复算（整轮回退 HEAD，还原后 8 个文件逐字节比对一致）22 支里 14 开火，一手读数
  `QUOTA 门禁读到 60，账本 SUM 只有 30`、`GET /api/usage 报 60`，路由尺点名 7 个出口。
  邻面 113 支 rc=0（含 test_settled_projection／test_api／test_migrations／test_courses／test_warmpool）。
- 连带改判：`tests/test_settled_projection.py` 里那句「不覆盖 usage.py 的展示算术与 app.js，本轮设计没有动它」
  已经不成立，按事实改成「那两位由本文件的 C1c/C6 覆盖，本文件不重复钉」。
- 未证实：真实前端渲染没在浏览器里跑（无 UI 档），JS 侧靠文本面尺＋HTTP 契约钉；
  PG 侧键形态未实测（本仓 PG 档没跑过这条路径）——SQLite 落库回读已实测。

### 被拒的格有了周期驱动者，扫描是定向档而不是全量（N-99，闭登记项 N-98）
- 缺陷是 N-96 改完之后露出来的那一半代价：释放准入拒下一格（不给卡，那是故意的），
  而 STOP operation 打满 `MAX_ATTEMPTS=3` 之后进程内没人再试；`OperationType.RECONCILE`
  有消费者（`app/services/orchestrator.py:163-164`）却没有任何入队点（本轮现算 grep：除消费者那一行外，
  app/ 与 scripts/ 零命中），`reconcile_all` 也不是周期任务 ⇒ 一张卡可以永久钉在没人用的 runtime 上。
- 选型（四个候选；依据全部本机重开原文逐字核对）：
  ① 每格入队一条 RECONCILE operation —— 否：同 workspace 的活跃 operation 被部分唯一索引
  （`app/models.py:520-521`）串行化，后台重试会饿死用户的 start/stop。
  ② 周期全量扫描 —— 否：每格一次 provider 往返。本机实测 mock 侧每格 0.02 ms、200 格一趟 3.2 ms
  （纯 DB），按 N-91 量到的 `docker inspect` p95 217 ms 外推到真引擎即 200 格 ≈43 s，
  而这趟跑在 worker 线程里 ⇒ 一格慢把同线程的用户操作一起拖住。
  ③ 事件驱动 watch（docker events / k8s watch）—— 未做：本仓没有可用的常驻事件通道，属另一条改造。
  ④ 选定：**周期定向档**——只碰「队列没在做」且「比阈值老」的格，一趟最多 8 格、30 tick 一次。
  三处一手依据各管一件事：controller-runtime `pkg/reconcile/reconcile.go:44`
  "RequeueAfter if greater than 0, tells the Controller to requeue the reconcile key after the Duration"
  （有意轮询与失败退避是两个通道）；`node_lifecycle_controller.go:939` 配 :850/:863 的两个 grace
  （**扫描周期与动作阈值是两个独立旋钮**，period 5 s／grace 50 s，不是一个数）；
  `nomad/nomad/config.go:273` "...but this sweeps up on quiescent clusters"（这类扫描的定位是静息兜底）。
  oslo.service 的 `periodic_task.py` 第 202 与 204 行（外部包，源码不在本仓）讲的抖动是本轮没做的那一件（见下）。
- 改法：三个常量进 `OperationWorker`（30 tick／60 s／8 格）；`reconcile_all(limit=, older_than_seconds=)`
  两个参数默认 None ⇒ **默认档就是改前的全量扫描**（启动恢复 `app/main.py:52` → `run_crash_recovery` 与
  既有判据走的就是这一档）；准入判定收在 `_reconcile_cell_admitted` 一处，活跃态沿用
  `_has_active_operation` 那份定义（pending/running/retrying），不另起一套；
  `deps._reconcile_stuck_cells` 注册进既有周期表，不新建线程。时间基准取
  `stopped_at or started_at or created_at`（最后一次真实状态变化）。
- 判据 `tests/test_periodic_reconcile_driver.py` 11 支：接线（周期表里真有它、间隔取自常量、
  打到那个 orchestrator 单例）、AST 尺盯两个界参数（缺关键字／写成字面量／函数不存在三种破坏各开火）、
  行为四极（被拒的老格真被收走且卡回池／队列在做的格不许抢／FAILED 之后回到驱动者手里／
  太新的格不看）、上界（5 格 limit=2 只看最早两格）、默认档仍是全量、
  持久化 RECONCILE 消费者不许被本轮拆掉（把调用换成 pass 就塌）。
- 复算：四臂单变量（每臂跑完按字节还原，末尾断言全树文本一致）——摘接线只红接线那支；
  准入恒真红「不许抢」与「阈值」两支；去掉 limit 只红上界那支；把默认值改成有界只红「默认档全量」那支。
  邻面 10 文件 130 支 rc=0；ruff/mypy rc=0；连带把
  `tests/test_reconcile_cell_isolation.py` 那条 stats 精确等值补上 `scanned`/`skipped` 两格（不是放宽断言）。
- 未证实/未做：多副本同相位的抖动没做——`docs/OPERATIONS.md:9` 写的是 "Control Plane (1+ replicas)"，
  所以这是真问题不是假想；量级是每副本每趟 ≤8 次 provider 往返，且重复扫描不产生第二种判决
  （settle 命中同一幂等键、release 的条件更新落空、enqueue 被部分唯一索引挡住——三条从既有代码读出，
  标为推断；重复收敛的幂等另有 N-90 的常驻用例钉着）。并发扫描若成为成本问题再加相位偏移。

### 周期回路与开机接线各有一条常驻判据，删掉接线不再是无声的（N-100）
- 缺陷（覆盖缺陷，不是行为缺陷）：`OperationWorker` 的周期任务表、`start()` 那条线程、`main.py` 的开机崩溃恢复接线，此前只被「跑到过」从没被「断言过」——把 `_run_periodic()` 整个删掉，全套仍绿。普查这一格是把 N-99 的驱动者接进周期表时引出的：接线的价值取决于它是否在场上，而接线本身没有任何位点读它。
- 判据 `tests/test_periodic_loop_and_boot_wiring.py` 11 支：取模闸门逐 tick（every=2/3 在 6 tick 里恰好 3/2 次）、一支周期任务抛错不许停兄弟、`_run_forever` 真调 `_run_periodic`（探针 + `tick_once`/`_stop.wait` 双计数）、真线程 `start()`/`stop()` 跑一轮并核成员集合与不增长、`start()` 幂等、四张注册表都在且间隔取自常量（绑定方法按 `==` 比而不是 `id()`）、lifespan 真叫 `run_crash_recovery`、`crash_recovery_delegation_offenders()` 那把 AST 尺子带 X1/X2/X3 三支控制、`main.py` 的文本面、`renew_lease` 的两极读回。
- 牙齿（五臂单变量，每臂跑完按字节还原）：摘掉 `_run_periodic()` ⇒ 3 红；取模恒真 ⇒ A1；撤掉 hold 的周期注册 ⇒ C1；去掉 lease 的时间谓词 ⇒ E2；去掉 lifespan 那一脚 ⇒ D1+D3。邻面 73 支 rc=0；ruff/mypy rc=0。
- 未证实：多副本同时开机（各自跑一遍 crash recovery）没测——单实例档只证明接线在场上。

### 「release 失败会被 reconcile 补做」这句承诺第一次有判据（N-101）
- 缺陷（覆盖缺陷）：`_fail` 的释放准入已放行、`scheduler.release` 自己抛错那一档（`app/services/orchestrator.py:374-389`）**故意**留下跨表冲突——卡仍 ALLOCATED 且指着这格、格上三列已清——日志与注释都写 (will be reconciled)，但没有任何常驻用例回头看卡那一行，也没有用例跑过那个「补做」的入口。既有那支 `tests/test_provision_rollback.py:396-397` 只断到格子侧三样为空 + 状态 FAILED。
- 改法：跨表量具 `gpu_ownership_disagreements()`（两个方向的冲突都点名）从 drift 判据里**原样抽出**成 `tests/gpu_drift.py`，一份而不是两份；新判据 `tests/test_release_failure_self_heals.py` 四档：T1 冲突必须可点名且卡不许已被读成空闲；T2 同格还挂着活跃 operation 时驱动者一躺不许动；T3 operation 走成终态后 `reconcile_all()`（N-99 注册进周期表的那个入口）必须把卡放回池子、冲突清零、再跑一趟不产生第二次变化；T4 反证不调驱动者就永远停在冲突态。
- 牙齿（六臂变异电池，各臂恢复后 `cmp` 逐字节相同、末跑全绿）：基线与无关注释臂 0 红；删掉驱动者末尾那一脚 ⇒ 只红 T3；删掉 `recover_stuck_gpu_allocations` 的 active-operation 保护 ⇒ 只红 T2；让 `_fail` 顺手放卡 ⇒ 四档全红（四档共用同一个前提断言，这一臂说的是「冲突根本不该留下」那条路）；把量具的 holder 方向改成读不到 ⇒ T1+T2 红、T3/T4 绿，证明前两档真在消费这把尺子的第二个方向。
- 未证实：本机跑的是 mock provider；docker/k8s 上 release 抛错的真实形状（DB 写失败 vs provider 边界）未测。

### STOP 那一侧的 release 自抛：按 provider 能不能答话分成两极钉（N-102）
- 缺陷（覆盖缺陷）：`_stop_cleanup` 的 release 自抛档（`app/services/orchestrator.py:471-474`）返回`stop finalize failed: ...` 并注释「留在 STOPPING，由 reconcile/重试补做」。全仓现算 grep：这条原因串只在生产侧出现一处，`tests/` 零处。
- 形状与 N-101 相反，所以判据不能照搬：STOP 这一侧什么都不清，两张表仍互相认账，残留的是「一张被队列占着的卡」。新判据 `tests/test_stop_release_failure_self_heals.py` 三档：S1 停在 STOPPING（不是 STOPPED、不写 FAILED）、原因留在 `error_message`、卡仍 ALLOCATED 指着原主、分配行还在、冲突为零；S2 provider 答不上来（mock 恒 UNKNOWN）时驱动者一躺不动它——问不到不等于没人吃卡（ADR 0008）；S3 provider 亲口说 MISSING 时同一个驱动者必须收完：STOPPED + 卡回池 + 三列清空 + 再跑幂等。S2/S3 同夹具同入口、只动 provider 自述这一个变量。
- 牙齿（六臂）：无关注释臂 0 红；把 STOPPING 档的 UNKNOWN 当「没了」⇒ 只红 S2；删掉 MISSING 档的补做 ⇒ 只红 S3；抛错档顺手清列 ⇒ 只红 S1；把可重试的失败写成 FAILED ⇒ 三支全红（共用前提断言）。各臂 `cmp` 还原、`git diff app/` 为空；邻面 40 支 rc=0。
- 未证实：账本侧「补做不得重复入账」由 `tests/test_gpu_seconds_metric_delta.py` 守着，本轮不复述也没换真值源复算。

### release 自抛的四处兜底，补齐后两处（N-103）
- 缺陷（覆盖缺陷）：`scheduler.release` 被吞掉的位置一共四处——`_fail`（N-101 已钉）、`_stop_cleanup`（N-102 已钉）、`destroy()`（`app/services/orchestrator.py:630-638`）、warm pool 认领撤销的放行档（`app/services/warmpool.py:399-409`）。后两处本轮之前零覆盖：没有任何用例让 release 从这两个入口抛错。同一趟普查还点名两条「promise 没人驱动」的残留，登记为 N-104／N-105。
- 形状：`destroy` 在 release 抛错后只 `db.rollback()`，接着照样写 DELETED + `deleted_at`；而「卡回池 + 三列一起清」住在 `Scheduler.release` 本体里（N-86），抛错就没执行 ⇒ 格上还绑着卡、卡还指着这个墓碑，两张表仍互相认账。补做的是 `reconcile_all()` 末尾那一脚回收器（终态不在保护集内）。
- 判据：T5/T6 进 `tests/test_release_failure_self_heals.py`（残留形状 + 驱动者收回并成对清列、不许动墓碑状态、再跑幂等）；warm pool 那档进它自己的文件`tests/test_warmpool_claim_admission.py::test_claim_abort_with_release_raising_leaves_a_reclaimable_terminal_holder`——归属跟着判据所在文件，不为凑新文件另开一份夹具。
- 牙齿（五臂，含两支基线）：无关注释臂 0 红；抛错档清列 / 抛错档直接放卡 各红 T5+T6，其中 T6 红在**夹具前提**而非自身判决（两支共用坐标系，写明而不是假装各有牙）；删掉驱动者末尾那一脚 ⇒ T3+T6（两处残留由同一个补做者收）；把回收器「成对的第二半」改成恒不执行 ⇒ warm pool 那文件 13 支里只红新加的这支。邻面 13 文件 151 支 rc=0；ruff/mypy rc=0。
- 未证实：`hold` 与 destroy 结算那两条（N-104／N-105）本轮只登记未动手。

### hold 抛错留下的那一笔 pending，第一次有人负责收（N-104，闭登记项 N-104）
- 缺陷（覆盖缺陷）：`_fail` 第一件事是退回圈住的额度，而那一步被 try/except 包着（`app/services/orchestrator.py:360-366`「释放失败不得掩盖状态置位，盲捕获有意，见 ADR 0002」），except 里只有一条 warning——额度被**故意**留在未完成态，指望 TTL 清扫补。既有覆盖各在一头（合规档走 worker 真链路、TTL 清扫本身、清扫在周期表里注册），中间那段没有：没有任何用例让 `release_hold` 真抛错。
- 判据 `tests/test_hold_release_failure_self_heals.py` 三档：H1 抛错不许泄给调用面、状态与原因照写，而那笔仍 PENDING、额度仍被圈住（钉的是「漏在哪张表上」，不是「没漏」）；H2 死线之前清扫不许提前收；H3 死线之后走 `orchestrator.release_expired_holds()`（`deps.py` 注册进周期表的那个入口）必须收掉并还额度。H2/H3 之间只动 `expires_at` 一个变量。
- 牙齿（五臂）：基线与无关注释臂 0 红；E1 摘掉 try/except ⇒ 三支全红（共用夹具，红在异常穿出）；E2 把时间谓词换成恒真 ⇒ 只红 H2；E3 把 RUNNING 保护反过来 ⇒ 只红 H3。各臂 `cmp` 还原、末跑全绿；邻面 7 文件 72 支 rc=0。
- 未证实：清扫的周期档（60 tick）与真实 TTL 时长的比例没量——本轮只证入口可达与判决正确。

### 「可补偿」是一句没有归属者的主张，改成判决并钉住（N-105，闭登记项 N-105）
- 缺陷（措辞与事实相反，不是漏账本身）：`destroy()` 的结算抛错档注释写「修复方向是「可补偿」」。查清后三个候选补做者都不在场——`reconcile_all` 的循环先跳过 tombstone（`app/services/orchestrator.py:687-688`）、`monitor_runtime_quotas` 的选择集要求 `deleted_at IS NULL`、`recover_stuck_gpu_allocations` 只管卡不管账。
- 判决：**这一段不入账，且不许事后补**。理由住在时钟里而不是省事：唯一还能为这一段算秒数的 `_settle_run`（`:550-556`）取 `utcnow() - started_at`，对墓碑格那就是把等待时长计成运行时长——补做会多计，而多计比少计更坏（本仓 N-64／75／76 全是这条轴）。注释按这个判决重写。
- 判据 `tests/test_streaming_lifecycle.py` 两档：E1 当场零 USAGE 条目、墓碑 `started_at` 已清、`accumulated_seconds` 不动，换回真账本再跑一趟周期驱动者仍然零条目；E2 对照档——同一夹具同一入口、账本没坏时必须正好入一段（3590–3600 秒）。E2 的另一重作用：证明 E1 那个「0」不是恒真，查询与写入面都在场。
- 牙齿（四臂）：基线与无关注释臂 0 红；F1 让墓碑保留 `started_at` ⇒ 两支一起红（都读这一列）；F3 把 `_settle_running_segment` 的状态守卫改成恒假 ⇒ E2 红，并连带打红既有那支 `test_destroy_settle_failure_marks_auditable_and_still_releases`——它钉的「留下可审计痕迹」其实依赖结算**被尝试过**，这是本轮顺带量出的一条既有依赖。E1 的「零条目」那一面没有任何现存改动能让它红（今天没有路径为墓碑写 USAGE），它防的是尚不存在的补做者，这一点写在 docstring 里。邻面 8 文件 81 支 rc=0；ruff/mypy rc=0。
- 未证实：真实账本故障（PostgreSQL 断连）下的行为没测——本轮注入的是 stub 抛错。

### 异常边界退回非终态之后，下一趟必须把这格收完（N-106）
- 缺陷（覆盖缺陷）：`_reconcile_one` 里的 `scheduler.release`（`:800`）与 `_finalize_stop`（`:833`）都不带守卫，抛错会落到「一格失败不得中止整趟」那个边界（`:703-712`）。既有两支注入判据炸的是`provider.reconcile`——在判据**之前**，结算根本没跑过；而本文件唯一连跑两趟那支用的是 `explode=set()`（健康档）。于是「本格退回原状态、下一趟接着收」这句话只钉了前半句。
- 判据 `tests/test_reconcile_cell_isolation.py` 三档：R1 release 在 RUNNING+MISSING 档抛错 ⇒ `errors=1`、本格留 RUNNING、卡与格上绑定都不动、两表一致，而**已提交的结算不被 `db.rollback()` 一起吞掉**（usage 恰好一条；结算自己 commit，边界只能退回未提交的那半）；R2 第二趟（release 已放行）必须收完：FAILED + 卡回池 + 三列清 + 冲突零 + usage 仍只有一条；R3 STOPPING+MISSING 档同形状走一遍。
- 牙齿（六臂变异电池）：无关注释臂 0 红；G2 让 RUNNING 档不再结算 ⇒ 只红 R1+R2；G3 把幂等键从 `started_at.isoformat()` 换成 `utcnow().isoformat()` ⇒ **只红 R2**（「补做不得把同一段算两次」这一面有独立的牙）；G4 让 STOPPING 档不叫 release ⇒ **只红 R3**；G1 让异常边界顺手写 FAILED ⇒ R1+R2 红并连带打红既有两支「本格留在原状态」的判据与 R3（四支读同一列，不是四支各有牙）。各臂 `cmp` 还原、`git diff app/` 为空；邻面 8 文件 86 支 rc=0。
- 未证实：这一档跑在 mock provider 上；docker/k8s 里 release 抛错的真实成因（daemon 断连、APIServer 5xx）未注入过——形状相同但错误来源不同，登记在本轮的「未证实」而不是缺陷。

### 崩溃那一趟遗留的库文件，从今天起有人收（N-107）
- 缺陷：N-95 的清扫只删「本进程 pid」的那些，docstring 把崩溃／被掐那一趟的遗留交给 `make clean`——而本仓没有任何循环会跑它。实测仓根就留着 2 个死 pid 的孤儿（本会话 N-99 基准那一趟 479 KB、更早一轮 n74b 探针 455 KB）：1.59 GB 那次堆积的机制从未被堵住，只是慢一点。
- 改法：新增 `sweep_dead_test_dbs`，接在 `pytest_sessionstart`（收尾那一脚只管本趟）。判据单边保守——只删「名字带 pid 且该 pid 确定已不在」的 `test-*.db`；无后缀／本进程 pid／活着（EPERM 与其它 OSError 一律算活着）全部不碰，所以 pid 被复用只会更保守。
- 判据 5 支（`tests/test_test_db_sweep.py`）＋七臂电池：H2 谁都不收 ⇒ D1+D5；H4 死了说活着 ⇒ D1+D2+D5；H5 问不到说死了 ⇒ 只红 D2；H1 不看死活 ⇒ 只红 D1；H3 摘接线 ⇒ 只红 D3。H1／H4 为了不在真仓库根上按改过的规则删文件，同时中和了 hook，故 D3 那一笔红记成「安全中和」而非臂的语义；首轮 H1 的 expect 写成 D1+D2+D5，实测只有 D1+D3 ⇒ expect 按观测改、判据一字未动。
- 现场证据：本档第一次跑之前仓根有 2 个孤儿，`pytest_sessionstart` 一落地就收掉（同一次运行 2→0）。

### 设备 `online` 要有心跳背书，超时改判 offline；`OFFLINE` 第一次有了写入者（N-108）
- 缺陷：`AgentStatus.OFFLINE` 零写入者、`last_heartbeat` 零读者——`heartbeat` 一写 online 就永久停在那儿，断掉的设备在控制面与页面上一直在线。运维看到的是一台可以派活的机器人，而控制面早就没听到过它的声音。与本仓反复修的那条教义同形：存在性主张要由事实背书，不能由「没报错」背书。
- 改法：`EdgeService.expire_stale_agents(db, offline_after_seconds=…)` 只把「ONLINE 且心跳早于阈值」改成 OFFLINE；阈值只有一个来源 `settings.edge_agent_offline_after_seconds`（默认 90 s＝设备端 5 s 一轮 × 18；对照 AWS IoT 对 MQTT 的 1.5× keep-alive 判死口径，本机打开读过 device-connectivity-status 页），组合根把它传进周期表第五档。这里不 gate 派工：本设计是设备侧拉取，派给暂时离线的设备是正常用法，状态列只负责说真话。
- 判据 7 支（`tests/test_edge.py`）＋六臂电池：K1 摘状态条件 ⇒ 红幂等与阈值两支（「从没通过话」那支的保护其实来自 `last_heartbeat IS NULL`，这是 K1 量出来的）；K2 比较号判反 ⇒ 四支红；K3 写死阈值 ⇒ 只红阈值那支；K4 忘 commit ⇒ 红三支靠重读库的；K5 组合根传裸数字 ⇒ 只红接线那支。
- K5 第一版是**存活**的：接线尺子当时用 `ast.dump(fn)` 找设置名，而闭包 docstring 里正好写着它——注释把代码的洞填平了；改成只看 `ast.Attribute` 的访问名之后才真红。教训写在尺子函数里。
- 连带：`.env.example` 补该档（`test_config_docs` 当场拦下「设了没写进样例」）；周期表成员判据从 four 改名 five 并加一行——成员是精确等值断言，新增注册必须同步改它，登记为有意的行为变化。
- 未证实：真设备断网／断电下的行为未测（本机只有 mock 驱动）；本轮证的是控制面在「心跳不再到达」这一事实下的判决。

### 设备端两处「没报错即成功」：发现响应的形状与丢掉的上报（N-109）
- 缺陷一：`AgentClient.list_assigned` 把「200 但不是 JSON 数组」折成 `[]`，而那与“今天没活”同形——设备不取件、不报错、退 0，运维看到的是一台在线且空闲的机器人。同一个文件对非 JSON 响应体本来就是抛错的（`_json`），清单这条路径不该例外。
- 缺陷二：`edge_agent/agent.py` 里遥测上报失败会被 `run_once` 的通用分支折成 `("error", before, before)`——一次**已经发生**的物理运行连同它的观察结果一起被读成“没发生”。
- 改法：形状不符即抛；上报失败那一格仍是 `ran`＋`verified`，另带 `reported=false` 与一行 stderr，退出码由 `main()` 据它抬。ADR 0007 后果段与 OPERATIONS §8 排查表各补两行（跑的人与运维那一侧都要看得见）。
- 判据 15 支（`tests/test_edge_agent_client.py` 12→19、`tests/test_edge_agent_unreported_run.py` 新增 8）＋九臂电池：M1 6 红、M3 2 红、M8 2 红（误伤方向的反证——把“真的没活”也判成形状错误，必须红在空闲档），M2／M4／M5／M6／M7／M9 各 1 红；九臂无存活。
- 中间读数：N-109 单独认证过一次（933 点、零失败），当时唯一的红就是这份文档面还没跟上——`docs_test_counts` 如实报"文档写 918、实测 933"。计数面只许有一处的理由就在这里：多一处必有一处会过期。

### 节点的 `online` 要有同步背书，inventory 要有周期驱动者（N-110）
- 缺陷：`gpu_hosts.status` 默认 `online`，全仓唯一写入点是 `sync_host`，而它只被开机那一脚调用 ⇒ 一台离开集群的节点对 admin 永久报在线；同一段里的“未再上报的 GPU → DRAINING”在进程存活期内没有驱动者，节点少一张卡要等重启才看得见。
- 改法沿用 N-108 的形状，不引入新判决：新增证据列 `gpu_hosts.last_synced_at`（alembic 第 15 节 `4f2b7c9a1e60`，**可空**——迁移时给存量行填一个假时间戳等于把所有主机同时判成“刚刚同步过”，判死窗口反而被拉长），每次成功同步盖章；`GpuScheduler.expire_stale_hosts` 只按证据改判，`NULL` 一律不动（从没同步过 ≠ 同步失败）；阈值单一来源 `gpu_host_offline_after_seconds=600`（＝刷新档 120 tick × 5 个窗口）；周期表新增两档，其中重报那一档调的就是开机同一个函数，不写第二套同步逻辑。
- 判据 14 支（`tests/test_gpu_host_liveness.py`）＋十一臂电池：A2 比较号判反 5 红、A5 改判不提交 4 红、A7 摘掉注册 2 红，其余各 1 红。
- 两支臂第一版**存活**，都按观测补判据（原判据一字未改）：A8 —— `(120, fn)` 与 `(常量, fn)` 在本轮默认值下**数值相等**，数值判据结构上看不见“常量成了装饰”，于是新增源码面尺子 `cadence_literal_offenders`；A10 —— 那张 UNHEALTHY 的卡当时**还在**上报清单里，压根走不到被改写那一步，改成“缺席且 UNHEALTHY”才量得出缺席降级的状态集合边界。
- 连带三条：`GpuHostOut` 吐 `last_synced_at`（OPERATIONS 的排查表指着这一列，端点不给就是空头承诺）；`test_periodic_loop_and_boot_wiring` 从 five 改名 seven 并补一根“周期表与判据清单必须同集合”——逐条判据看不见表上长出新档（本轮加两档时它照绿就是证据）；`test_version_consistency` 新增 openapi 工件时效判据，起因是重生成 `make api-docs` 时补出了一个更早一轮的 `usage_segment_booked`：那份对外工件已经漂了一轮以上，而没有任何常驻读者发现（新判据自己用“删一个字段”验过会红）。
- 选型：本轮不引新依赖，沿用仓内既有形状（证据列＋周期改判／alembic batch 模式，ADR 0005 的 SQLite‑PG 语义对等）；600 s 的判死取向借的是 N-108 那一轮读过的 AWS IoT「1.5× keep-alive 才判死」口径——**本轮未重开该页**，出处见上一节。
- 未证实：provider 会不会真在运行中少报一张卡（mock 永远报同样 8 张，本机也没有真 GPU／集群）。被钉住的是“同步本身能收敛”与“有周期驱动者在叫它”，实机那一档仍挂在物理面。

### 分配要求「这张卡所在的节点今天看得见」（N-112，闭 N-111）
- 缺陷：N-110 只让 host 的 `online` 说了真话，**没有改行为**——一台彻底不再同步的节点，它名下仍标 `AVAILABLE` 的卡照旧被 `allocate` 选中，新工作区被派到一张根本不存在的卡上。
- 改法：候选语句带 `EXISTS (gpu_hosts WHERE id = gpus.host_id AND status='online')`，并且**「还在等锁的那把尺」`still_waiting` 吃同一个谓词**——两处不一致时，一张失联节点上的卡会被数进“正被别人锁着”，于是容量结论被说成等待（运维去等一把不存在的锁）。
- 为什么这一层刻意**不写** `gpus.status`：管理员 `/drain`、`/unhealthy` 的判决有自己的生命周期，而 inventory 每 2 min 重报一次——“重新出现即回池”会让手工 drain 活不过两分钟；反过来“节点暂时失联”必须可逆。读证据列同时满足两边：节点回到 online 的下一趟分配自然看得到那张卡，全程没有任何人改写过卡的状态。
- 判据 6 支（`tests/test_gpu_allocation_visibility.py`）＋四臂电池：A1 候选不吃可见性 ⇒ 4 红；A2 只有候选吃、计数不吃 ⇒ 2 红（红在“报成等锁”那两支）；A3 谓词判反 ⇒ 5 红；A4 少了关联条件 ⇒ 3 红。一处 expect 被读数推翻（判据一字未动）：A4 第一版把 release 那一支算进去，实测不红——那条用例跑到最后两台节点都是 offline，无关联的 EXISTS 与有关联读出同一个答案，可见“关联条件”只有 V6 一个主人。
- `of=[Gpu]` 从“文档主张”变成“量过的东西”：真 PG 上三档实测（证人 `tests/test_postgres_concurrency.py`）——① EXISTS＋OF：主机行仍可被别的会话锁走；② 只摘 OF、谓词仍是 EXISTS：**也锁不到**，所以新增的那一支在当下打不到任何东西（这句如实写进用例 docstring，免得下一轮把它读成已有牙）；③ 把 EXISTS 改写成 `.join(GpuHost, ...)` 且没有 OF：主机行**会被锁**，22 支 PG 档里只有这一支红。③ 是一次完全正常的重构形状，OF 与这支判据就是为它留的。文档出处：本轮打开 sql-select 页读到的 "A locking clause without a table list affects all tables used in the statement."
- 未证实：真实故障注入（把一台 docker 宿主从集群里摘掉）没做，本机没有真 GPU 节点；本轮证的是“判决读的是同步证据”与“锁面在哪种写法下会变宽”。

### 设备回报终于有人据此收口：`edge-run` → 部署终态（N-113）
- 缺陷：ADR 0007 的后果段写着“设备把运行结果作为 `kind=edge-run` 的遥测回传，控制面据此收口”，而 app/ 里没有任何一处读这条遥测——`DeploymentStatus.RUNNING` 的唯一出口是用户手工 `POST /api/deployments/{id}/complete`（`app/routers/deployments.py:129`）。设备能做的四件事（begin → 取件 → 报摘要 → 上机回报）里，最后一报落进 `telemetry_events` 就到此为止：一条真跑完的部署永远挂在 `running`，那句 ADR 对运维是个假承诺。
- 改法：在遥测写入的同一路径上收口（`DeploymentService.complete_from_agent_report`）。授权与幂等都不写在 Python 里，而是同一条条件 UPDATE 的 WHERE（`id + status=running + edge_agent_id=这台设备`，与本文件既有的 `begin_agent_download` 同一形状）——payload 点谁的名“不重要”：设备收得了的只有自己名下那条正在 running 的部署；终态一旦写下，后到的相反结论改不动它；没 run 过就没有可收口的状态，凭空判成功走不通。遥测事件本身照旧全部留档。
- 信任边界如实写进 ADR：`ok` 与 `detail` 由设备自报，控制面无从复核机器人是否真动了；设备能做的最坏事情是把**自己名下**那条判成 success/failed，碰不到别人的行，也改不了已成终态。
- 判据 7 支（`tests/test_edge_run_closes_deployment.py`，全走真 HTTP 端点＋真产物，不手工塞库）＋八臂电池：N1 摘掉收口 ⇒ 3 红；N2 去掉状态守卫 ⇒ 2 红；N3 去掉归属列 ⇒ 1 红；N4 成功档串写 error_message ⇒ 1 红；N5 失败档不留因 ⇒ 1 红；N6 不看 `ok` 一律判成功 ⇒ 1 红；N7 不分 kind 全都收口 ⇒ 1 红；N8 服务端自己把线协议名改掉 ⇒ 1 红（跨包常量两侧各一份、刻意不互相 import，相等关系由 `test_run_kind_wire_name_matches_the_device_spelling` 钉住）。N3 第一版锚点命中 2 次被闸门拦下（`edge_agent_id == agent.id,` 在 `begin_agent_download` 里同形），换成两行锚之后才有牙——那是锚点歧义，不是判据弱。
- 连带两面文档：ADR 0007 后果段补上实现位点与信任边界；OPERATIONS §8 写明“部署莫名变 success”的第一因是设备报的，去遥测里找那条 `edge-run`；反过来一条停在 `running` 意味着设备还没报或报丢了（N-109 那一档），不是控制面卡住。
- 一条自伤留痕：本轮代码提交 `3c21389` 的标题行误抄了 N-110 的题句（正文无误）——按 sha 回看的读者以本节为准。
- 未证实：真机回报（本机只有 mock 驱动）；超时那一档另登记 N-115——事件驱动的收口天生管不到“跑完就被掐、永远没人报”的那条部署。

### runtime 失踪那一档要把串流会话一起终结（N-116）
- 缺陷：`_reconcile_one` 的 RUNNING→MISSING 那一档（`app/services/orchestrator.py:805`）结算、放卡、写 `FAILED`、记 `stopped_at`，却不叫 `streaming.terminate_for_workspace` —— STOP 档（`:448`）与 DESTROY 档（`:618`）本来就调它，唯独这条「runtime 自己没了、没人叫过 stop」的路漏了。后果是两张表互相打脸（与 N-82 同族）：`workspaces` 说这一格已经死了，`streaming_sessions` 还留着 `connected` 与两侧端口，`GET /api/streaming/workspace/{id}` 与前端那一行端口成了假活。
- 分母由一手扫描给出（AST 判，终态枚举名取表达式根节点，不用行窗口）：全仓 `app/` 写 `WorkspaceStatus` 终态共 5 处 —— `_finalize_stop:597`（STOPPED）、`destroy:648`（DELETED）、`_reconcile_one:796`／`:814`（FAILED）、`warmpool.claim:408`（FAILED）。改前 4 处在同一条路径上先它执行过终结会话，唯一没配对的正是本轮补的那一处（`:814`）；补完重扫仍是 4/5，剩下那一处是 `_finalize_stop`，本轮登记为 N-117。
- 改法：MISSING 档在结算之前调 `terminate_for_workspace`（`:811`）。选它而不选「在 reconcile 循环末尾统一收尾」：该函数只动库、不叫 provider，无活动会话时返回 0、重复调用同结果，因而在「一格失败退回非终态」那一趟（N-106 立的边界）也不会留下半个收尾。
- 判据 6 支（`tests/test_reconcile_closes_streaming.py`）。行为面四支：MISSING 档会话置 `failed` 且会话与 workspace 两侧端口都交还、卡回池；ALIVE 档不开火（会话仍 `connected`）；UNKNOWN 档按 ADR 0008 谁都不许动；复跑一趟 `failed=0`、`scanned=0`、usage 条目不翻倍。结构面两支：一把尺子把「把 workspace 写成终态之前必须在同一条路径上终结过会话」钉住（只认排在它前面的同层兄弟与上层兄弟，「祖先块里调用过但排在写之后」不算成对），真文件读数 `[]`；另一支是这把尺子的反向对照，三张合成图分别必须点名 2／0／1（漏两处配对／同层在前与上层在前都算成对／调用排在写之后）。
- 三臂电池（靶 `app/services/orchestrator.py`，三档套件 58 点）：P1 把新加的那行拿掉（回到改前形状）⇒ 2 红（行为档＋尺子）；P2 只改顺序、把终结挪到写终态之后（行为不变）⇒ 1 红（只有尺子点名，说明它钉的是配对而不是行数）；P3 拿掉 STOP 档既有的那行 ⇒ 2 红，红在别的文件（`tests/test_streaming_lifecycle.py:137`／`:186`）。P3 的 expect 由第一遍读数补齐，不是按「我以为谁红」写；它同时量出这把尺子的作用面：只覆盖 `_reconcile_one`、不进被调函数体找终结调用——这条限度写进了用例 docstring，不冒充全仓规则。恢复后复跑 0 红。
- 门禁 G0.106；§2 新行 N-116；新登记 N-117（`_finalize_stop` 的配对证据在调用链上而不在函数里）。
- 未证实：真机流媒体面（本机没有 WebRTC 会话；会话行由 ORM 直建，被测的是「终结」那一半，`streaming.start()` 要 RUNNING 之外的整套所有权／凭据前置，在这里只会稀释判据）；`stop():421-427` 的「已 STOPPED 幂等补做」一档靠「STOPPED 蕴含会话已闭」这条没有机械核的不变量。

### 池子的余量与分配器吃的不是同一条证据（N-122，闭 N-114 的 (a) 半边）
- 缺陷（两个谓词各说各话）：`WarmPoolManager._free_capacities_gib`（`app/services/warmpool.py:194`）只按 `gpus.status == available` 数空闲显存，而 N-112 之后 `allocate` 在同一个 WHERE 里多要一条 `host_is_visible()`（`app/services/scheduler.py:67`，一条 EXISTS 在 `gpu_hosts.status == online` 上）。于是一台停止同步的节点上那张仍标 AVAILABLE 的卡被池子记成余量 ⇒ 补位闸门（`app/services/warmpool.py:156-161`）开出一格 PREWARMING ⇒ worker 起来占不到卡 ⇒ 收割成 FAILED、入队 DESTROY、进冷却，一整个预热周期白烧。这是 N-114 (a) 那条账里真正会烧钱的一半。
- 改法是把那条谓词**借过来**（`app/services/warmpool.py:44` 从 `.scheduler` import），不手写第二份规则——第二份会自己漂，漂了就是这次的缺陷重演；这条规矩本身由结构判据钉（见下）。顺带更正 `expire_stale_hosts` 的一句假话：「host 的 `status` 只有一处读者」自 N-112 起就不成立，grep 读数是三处（`app/services/scheduler.py:81` 那条 EXISTS → `allocate` 的 `:202`／`:251`、`app/services/warmpool.py:211`、admin 的 `GET /api/gpus/hosts`）。
- 改前复算（一次性夹具，不改仓库代码）：同一份库里两张卡（在线节点 8 GiB、失联节点 24 GiB）、一个 `gpu_requirement_gb=16` 的模板 —— 旧谓词数出 `[8, 24]` ⇒ 判“开格”；新谓词数出 `[8]` ⇒ `skipped_no_capacity`。失联节点那张卡 `status=available`、`workspace_id=None`，全程没人改写它（N-112 那条不变量仍在）。
- 判据 5 支（`tests/test_warmpool_capacity_visibility.py`）。行为面：决定档（唯一够用的卡挂在失联节点 ⇒ `created == 0` 且零行 workspace）、恢复档（节点重新同步的下一趟 `maintain()` 照开 ⇒ 证据驱动而非永久抽干）、健康舰队对照（并钉住“池子挑的卡＝分配器真占的卡”这一同向性）。结构面：AST 钉「调的是那条共享谓词、名字来自 `.scheduler`、函数体里没有 `GpuHost`」，外加这把尺子自己的三态反证（合规／重抄一份／根本没有规则，三种读数必须互不相同）。失联一律走证据列（固定 `last_synced_at` ＋ 仓库自己的 `expire_stale_hosts(now=…)`），不手写 `UPDATE gpu_hosts SET status='offline'`——那样测的是我自己写下的结论列，不是那条 EXISTS。
- 三臂电池（`/tmp/n122-battery.py`）：D1 摘掉谓词 ⇒ 4 红（决定档＋恢复档＋两支结构档）；D2 换成**等价的手写第二份**规则 ⇒ 2 红且只红在结构档、行为档全绿——这才是“行为同、形状违反”那一极；D3 把 `host_is_visible` 反号 ⇒ 8 红，其中 5 支是 N-112 在 `tests/test_gpu_allocation_visibility.py` 里的既有判据（同文件控制随本轮重跑，全绿才是意外、红才是应该）。恢复后复跑 0 红。
- 两条自伤留痕（都在本轮内发现并修）：① D2 第一版五支全红，红因是 `warmpool` 没 import `GpuHost` ⇒ NameError，那一臂什么都没判别，补上 import 才成为等价臂；② 电池在变异与恢复之间崩在参数形状上，把 D3 的反号变异**留在了工作树里**，`git diff` 当场查到——之后给每支臂加了 try/finally 还原。
- 门禁 G0.110；§2 新行 N-122；ARCHITECTURE 的可见性条目补上第三个消费者；登记项 N-114 行内更正：(a) 已闭，(b) 仍开放（`DRAINING` 一个值同时承载管理员判决与自动判决，要给自动判决定义归位就得先给管理员判决换一个值，否则“重报一次就把手工 drain 抬回池子”）。
- 未证实：worker 那一侧的失败代价（占不到卡 → FAILED → DESTROY → 冷却那一串）没测——本轮只证「池子不再开这一格」，那一段属 `tests/test_warmpool.py` 与 provision 失败档的范围；真机 GPU 的节点失联（把一台宿主从集群里摘掉）仍无证据，本机只有 mock。

### 机器判决要能归位，人工判决不跟着归位（N-123，闭 N-114 的 (b) 半边）
- 缺陷（一个值承载两种判决）：`DRAINING` 同时是「这次上报里没有这张卡」（自动缺席降级）和「管理员把它摘出去了」（`POST /api/gpus/{id}/drain`）两种判决。于是两边都动不了：`sync_host` 对重新上报的卡只更新 model/memory/index（`app/services/scheduler.py:165-168`），一次抖动缺席＝永久掉容量；而 `mark_draining` 的前置是 `status == AVAILABLE`，已经被自动降级的卡再下人工判决会静默 no-op、路由仍回 204——「没报错即成功」那一族（N-109／N-113 同族），也与 `docs/adr/0006-lifecycle-conflict-is-409.md:54-55` 同族：路由拿到的假成功不是证据，冲突就说冲突。这是 N-112 当年拒绝用状态列实现 N-114 (a) 的理由本身。
- 改法借 SLURM 的三分法（`sinfo` 手册本轮 19:37 亲手抓取：DRAIN 是“per system administrator request”，DOWN 是“Slurm can automatically place nodes in this state if some failure occurs”，`*` 是不响应），把「谁下的判决」提到值本身：`app/models.py:50` 新增 `GpuStatus.DRAINED`；`DRAINING` 从此只表示缺席，`app/services/scheduler.py:172-174` 重报即归位；`app/routers/gpus.py:39-52` 的人工 drain改判 `DRAINED`，前置不满足回 409 并点名当前状态。`gpus.status` 是 `String(32)` 且全仓没有任何 CHECK／ENUM 约束（`alembic/versions/572b9ffa9375_initial_schema.py:132` 只建索引），所以加成员不需要迁移，迁移数仍是 15。
- 第三处是顺带抓到的同类：`release` 与 `recover_stuck_gpu_allocations` 的三个批量 UPDATE（`app/services/scheduler.py:310-320` 与 `app/services/scheduler.py:468-495`）无条件把带绑定的卡写成 AVAILABLE，而 `mark_unhealthy` 没有状态前置——管理员先隔离一张在用的卡，工作区一停，判决就被自动路径抹掉、卡回池子。改法是 `values(status=case(...))`：占用还池，人工判决只清绑定不改写。状态列写成 `case` 而不是拆成两条 UPDATE，是为了不打断 `tests/test_recover_gpu_column_drift.py` 那把配对尺的分母（它按 `update(Gpu)`＋`workspace_id=None` 数真位点，拆语句会把 `release_sites` 从 3 顶到 5，八处断言一起漂）。
- 判据 16 支（`tests/test_gpu_drain_provenance.py`）。行为八极：缺席→重报归位、归位后真的能分配出去、一直在清单里的卡不开火、人工判决活过缺席与重报、自动降级被人工判决升级后不再归位、占用的卡两边都不动、unhealthy 不被重报抬回、隔离中的卡放卡清绑定留判决；HTTP 两极走真实产品路径（建工作区→RUNNING→`/drain` 回 409→stop 后仍回池），另一支钉 204 之后读端确实吐 drained 且重复调用仍成立。结构面两支：provenance 登记册（`GpuStatus` 成员→写它的函数名，钉 DRAINED 只有 `mark_drained`、DRAINING 只有 `sync_host`）＋归位必须排在 `status == DRAINING` 谓词之下的 AST 尺；配四态合成反证（有守卫／没守卫／守卫是别的判决／没有 sync_host）与「`case` 分支要认得、`WarmPoolState`／`WorkspaceStatus` 不许混进分母」两向判别（枚举名判别是 N-122 的教训）。前端一支以 `GpuStatus` 自己为分母核 `GPU_STATUS_CN`，新增成员忘配中文名即红。
- 五臂电池（`/tmp/n123-battery.py`）：E1 摘掉归位谓词 ⇒ 5 红（行为四极＋结构尺）；E2 人工判决退回写 DRAINING ⇒ 4 红（含登记册）；E3 三处批量 UPDATE 退回无条件写 AVAILABLE ⇒ 1 红（隔离档）；E4 路由丢掉 409 ⇒ 1 红（HTTP 那一极）；E5 只改注释 ⇒ 0 红（必须不开火，那支证明尺子读的是 AST 而不是文本）。
- 电池第一版的自伤（留痕）：E3 的还原锚 `status=GpuStatus.AVAILABLE.value,` 少了行首换行，16 空格前缀是 `sync_host` 里 24 空格构造参数行的子串，还原时多换了一处把构造参数改成了 `case(...)`，随后 E4／E5 跑在坏文件上各读出 48 条收集错误——那不是判据开火，是我的夹具坏了。第二版改三处：缩进敏感锚一律带行首换行、落盘前普查全部锚点命中数、还原锚数不符即中止后续臂并把「未测」写进汇总。工作树的残留由 `git diff --numstat` 当场抓到并用 `git checkout` 还原。
- 门禁 G0.111；§2 新行 N-123；N-114 的 (b) 半边随本轮划销（(a) 已由 N-122 闭）；新登记 N-124：占用中的卡挂不上人工判决——本仓把「占用」与「判决」挤在同一列，`mark_unhealthy` 可以直接把 allocated 的卡判掉，那正是 N-123 不敢让 `/drain` 学它的理由；要做 K8s 式「意图与观察分栏」得加一列（迁移＋`GpuOut`＋openapi＋模型↔迁移对账）。文档面同步：ARCHITECTURE §3 两种 drain 的分工与那句「重新出现即回池会让手工 drain 活不过两分钟」的更正、PRODUCT_SPEC 的状态集合、API.md 的 `/drain` 行、OPERATIONS 容量模型一行。
- 未证实：前端渲染没在浏览器里核（`make validate` 的 browser 档 NOT_RUN），这一支只有文本面与 AST 面；真机节点被摘出集群后重编报上来的归位（本机只有 mock inventory）；PostgreSQL 上 `case` 型 UPDATE 的语义与 SQLite 同（由本轮认证的 PG 档跑过 release 路径撑，但没有一条判据专门盯两种方言的差异）。

### 一列装不下两个主人：管理员的下架意图单独立一列（N-125，闭 N-124）
- 缺陷（一列两主，登记项 N-124 的原话）：`gpus.status` 既回答「这张卡被占了没有」又回答「管理员有没有把它摘走」。N-123 因此只能让 `POST /api/gpus/{id}/drain` 作用于池子里的卡，占用中的卡回 409——那一版不是保守，是**一列写不下两个事实**：把 allocated 覆盖成 drained，对外的 `gpu_allocated` 指标（`app/main.py:34-37` 数 `status == allocated`）当场少一张，而那一格的 `workspaces.gpu_id` 还指着它。
- 改法是按 K8s 的 `spec.unschedulable`／`.status` 分工把意图单独立列：`app/models.py:346` 新增 `GpuStatus` 之外的证据列 `gpus.drain_requested_at`，alembic 第 16 节 `alembic/versions/f3a91c64d0b7_gpu_drain_requested_at.py`（nullable、无 server default——回填时间戳等于宣称管理员要求摘走每一张在册的卡，那是凭空造出一批从未发生过的判决）。`/drain` 占用中也判得动：当场只记意图、状态仍是 allocated；`release` 与 `recover_stuck_gpu_allocations` 的三处批量 UPDATE （`app/services/scheduler.py:310-320`、`app/services/scheduler.py:500-540`）的 `case` 多一条 WHEN：带意图的占用卡落成 drained 而不是 available；新增 `POST /api/gpus/{gpu_id}/undrain`（`app/routers/gpus.py:54-66`）作为**唯一**解除路径——没有它，一次误点就是一张永久掉的卡，只能改库。
- 选型（这一轮真改了决定）：候选一是 SLURM 的值分档（`sinfo`：DRAIN 是“per system administrator request”，DOWN 是 Slurm 自动置），N-123 走的就是这条；候选二是 K8s 的字段分栏（`kubectl cordon` 写 `.spec.unschedulable`，心跳只改 `.status.conditions` 里的 Ready），本轮换成它；候选三是引第三方状态机库（PyPI `transitions` 0.9.3，MIT）——否掉：它不回答「这个值承载谁的判决」，而那是缺陷本身。为什么不在 `status` 里再加一个 「allocated＋被摘走」的组合值：那要把占用与判决的每个组合都编成一个值，而 `case`／登记册／前端标签三处都要跟着乘。
- 改前复算（同一份判据在改前树上的读数）：占用中的卡 `/drain` 读到 **409**、`status` 仍是 allocated 且没有任何地方记下这份要求（F2 臂的读数：两支行为档红）；改后同一脚回 204、`GET /api/gpus` 里 `drain_requested_at` 非空，工作区停止之后这张卡读到的是 drained 而不是 available（F1 臂：三支红，含登记册那支）。
- 判据 21 支（`tests/test_gpu_drain_provenance.py`，本轮 +5）。行为面四支新档：占用中判下架⇒当场只记意图、释放时兑现成 drained；提前撤回意图⇒同一张卡释放回 available（证明意图列不是单向门）；`/undrain` 只抬自己判下去的 drained，`draining`（机器）与 `unhealthy`（健康）一律不动；HTTP 那一极从「409 并点名状态」换成「204＋意图当场读得到＋停止后落成 drained」，并核 `GET /api/gpus` 吐出新列。结构面新增一支意图列写入者登记册（真树读数必须是 `{mark_drained, mark_undrained}`，合成反证 `sync_host` 归位时顺手置 NULL ⇒ 立刻点名 `sync_host`）；判决写入者登记册同步改判：`DRAINED` 的合法写入者从 1 个变成 3 个（管理员那一脚＋两处“按意图兑现”的释放路径），这条改判是**本轮有意为之**，不是判据漂了。
- 五臂电池（`/tmp/n125-battery.py`）：F1 释放不兑现意图 ⇒ 3 红（两支行为＋登记册）；F2 占用中的卡不记意图 ⇒ 2 红；F3 `undrain` 撤不回 drained ⇒ 1 红；F4 自动路径清掉管理员意图 ⇒ 1 红（只有登记册那支，行为档看不见——这正是结构档存在的理由）；F5 只改注释 ⇒ 0 红。电池第一版被自己的普查拦下：它把「开局 `app/` 必须干净」当不变量，而本轮的代码本来就还没提交，判据改成「每臂跑完 `git diff app/` 与开局快照一致」。
- 三处更正与连带面：① 我上一轮写进注释的「`app/main.py:36` 是 stuck 保护」是假话——那一行是 `gpu_allocated` 指标，占用保护在 `recover_stuck_gpu_allocations` 里按 workspace 状态判（`app/services/scheduler.py:429-448`），注释与本节已按实况改写；② API.md 的 `/drain` 行、ARCHITECTURE 的 drain 条目、OPERATIONS 的容量读数都从「占用中的卡回 409」改成「记意图＋`/undrain` 解除」；③ 迁移数 15→16（CURRENT_STATE §1 的 Migration 行），`docs/openapi.json` 由 `make api-docs` 重生成（`GpuOut` 多一列＋新路径），读者是 `tests/test_version_consistency.py:215`；新端点的 403 档补在 `tests/test_gpu_admin.py` 的非管理员那支里。
- 登记项 N-124 随本轮划销；新登记 N-126：`/unhealthy` 仍然**直接覆盖**占用事实（`mark_unhealthy` 没有状态前置），一张在用的卡被判 unhealthy 之后，`gpus.status` 不再能说「这张卡在用」而 `workspaces.gpu_id` 还指着它——与 N-124 是同一味药没吃完的那一半。
- 未证实：新迁移在 PostgreSQL 上的 `batch_alter_table` 路径由本轮认证的 PG 档跑到（`tests/conftest.py:84` 每条用例走完整迁移链），但**没有**一条判据专门比 SQLite/PG 两种方言在这一列上的语义差异；~~前端仍不显示「在用但已被摘走」这种形状~~（**已由 N-128 闭合**：`已请求排水` 徽章进状态格，健康与下架各有两颗按钮），（`GPU_STATUS_CN` 按 status 上色，意图列没进表格里）；存量库（本机 `embodiedcloud.db`）没跑过 `alembic upgrade head`，本轮只在一次性库里验过 up/down/up。

### 健康不再挤在状态列里：占用与判决第一次能同时为真（N-126，ADR 0010 落地）
- 缺陷（一列两主，N-124 那味药没吃完的一半）：`gpus.status` 既回答「这张卡被占了没有」又回答「这张卡被判定不健康没有」。`mark_unhealthy` 没有状态前置，于是管理员对一张正在被用的卡点 `POST /api/gpus/{gpu_id}/unhealthy`，`status` 就从 `allocated` 变成 `unhealthy`——而 `status == allocated` 是这张卡在 `gpus` 一侧唯一的占用载体（对外的 `gpu_allocated` 指标就数它，`app/main.py:34-37`），`gpus.workspace_id` 与 `workspaces.gpu_id` 却都还在原地。两张表互相打脸，且**没有一条判据会红**：既有判据只钉「unhealthy 不许被自动路径抬回 available」那一半。
- 形状由 `docs/adr/0010-gpu-status-is-three-dimensions.md` 在上一批定下（候选：组合值／独立列／第三方状态机库 `transitions` 0.9.3，六维对比在 ADR §2），本节是它的实现。`status` 从此只答调度可用性：`app/models.py:55-58` 新增 `GpuHealth`，`app/models.py:358` 新增 nullable 证据列 `gpus.health`，`GpuStatus` 里的 `UNHEALTHY` **删除**——留着就是一个枚举不再拥有、却仍能被写进去的值。迁移 `alembic/versions/391540c0fcb4_gpu_health_column.py`（第 17 节）：列 nullable 而不回填 `'healthy'`（回填＝宣称管理员判过每一张在册的卡，NULL 才是「从没人判过」），同时把存量 `status='unhealthy'` 翻译成 `status='drained'`＋`health='unhealthy'`，`downgrade` 以 `health` 为准反向翻译。
- 谓词只许有一份（N-122 立的规矩）：`health_is_usable()` 住在 `app/services/scheduler.py:97`，`allocate` 的候选在 `app/services/scheduler.py:238`、`still_waiting` 的等锁计数在 `:288`、warm pool 的余量在 `app/services/warmpool.py:213`、连测试夹具数空闲卡也吃它（`tests/gpu_pool.py:106`）。少一条就会出现「夹具宣布净额够而分配器拒发」——N-85 量的正是那一格。
- 人的判决要有人的解除路径：新增 `POST /api/gpus/{gpu_id}/healthy`（`app/routers/gpus.py:44`，admin，204）把 `health` 交回 NULL；它与 `/undrain` 同形，只清自己那一维，不替 `draining`／`drained` 改口。前端 `app/static/app.js:33` 加 `GPU_HEALTH_CN`、`:1249` 在状态徽章旁边渲染健康徽章——否则这张卡在表上只写着「可用」，等于把刚拆出来那一维又藏回去。
- 判据 24 支（`tests/test_gpu_drain_provenance.py`，本轮 +2）。行为面：占用中判健康 ⇒ `status` 仍是 allocated 且 `health` 是 unhealthy，`release` 之后可用性回 available 而判决仍在，且这张卡**真的派不出去**；缺席与重报只动可见性轴，健康轴全程不动；`/undrain` 不抬健康判决。结构面：状态登记册去掉 `UNHEALTHY` 一项，新增 `health_writers` 登记册（`UNHEALTHY` 只由 `mark_unhealthy`、`NULL` 只由 `mark_healthy`），配「`sync_host` 写健康必须被点名」的合成反证；前端那一档分母取自 `GpuHealth` 自己。连带重指四处既有档位（`tests/test_gpu_admin.py` 的三维度流转与两条解除路径、`tests/test_gpu_allocation_visibility.py` 的两轴同读、`tests/test_gpu_host_liveness.py` 的三张缺席卡、`tests/gpu_pool.py` 的数空闲卡）。
- 四臂电池（`/tmp/n126-battery.py`，还原一律写回注入前读到的原文，跑完与开局 `git diff` 快照逐字节相同）：G1 健康判决退回覆盖 `status` ⇒ 7 红（含 G0.103 与 G0.109 那两支既有判据，同文件控制随本轮重跑，红才是应该）；G2 分配器与等锁计数不吃健康谓词 ⇒ 2 红；G3 自动路径顺手清健康判决 ⇒ 2 红（只有登记册与那一档行为红，别的看不见）；G4 只改注释 ⇒ 0 红。
- 门禁 G0.113；§2 新行 N-126；G0.103 那行的「不许改写 UNHEALTHY」按新词汇更正为「人工判决（`DRAINED`）不被缺席降级覆盖，健康列一律不动」；CURRENT_STATE §1 迁移链长 16→17（这条由常驻用例 `test_the_status_page_agrees_with_the_committed_report` 机核）；`docs/ARCHITECTURE.md`／`docs/PRODUCT_SPEC.md`／`docs/API.md`／`docs/OPERATIONS.md` 四处把健康从状态词汇里摘出来；`docs/openapi.json` 由 `make api-docs` 重生成（`GpuOut` 多一列＋新路径）。新登记 N-127：`gpus.health` 今天**只有人在写**，provider 与设备侧没有任何观察来源——拆出这一列只是把判决放对了格子，没有凭空造出「谁来发现一张卡坏了」这条链路。
- 未证实：真机硬件故障的发现链路（本机没有 NVIDIA 设备，provider 只会报同样的 8 张卡）；第 17 节在真 PG 上的整链执行由本轮认证的 `pg_integration` 档跑（`tests/conftest.py` 每条 PG 用例走完整迁移链），但没有一条判据专门比两方言在 `health != unhealthy` 上的 NULL 语义（`or_` 那一条在两侧都实测过谓词形状，方言差异本身未单独钉）；`gpu_allocated` 指标没有「按健康单独上报」的 gauge，本轮不做；`downgrade` 的一处已知不保真：upgrade 之后新写入的「真 drain ＋ 又被判不健康」的卡，在 `downgrade -1` 时会按 `health='unhealthy'` 抬回 `status='unhealthy'`——`drained` 与「不健康的存量判决」在 `status` 上同形，是 ADR 0010 §3 让 `drained` 承接存量判决的代价。





### 管理台按维度给按钮：死比较与没有读者的列都判违规（N-128，管理台按维度给极）
- 缺陷（N-126 留下的读者面）：`app/static/app.js:1253` 的动作门写的是 `g.status !== "unhealthy"`。健康搬进 `gpus.health` 之后状态列不再有那个值，这个条件从此恒为真——「标记异常」在任何一张卡上都不消失，而它本该只在没被判过的卡上出现。这一族缺陷的形状是「比较还在、对端已不存在」：不抛错、不影响任何既有断言，`tests/test_browser_console.py` 的六视图那支又按守卫跳过 gpus 视图，所以树上没有任何读者能看见它。
- 同一次普查另两处缺席（都是「列有了、没人读」）：`gpus.drain_requested_at` 由 `GET /api/gpus` 吐出来（`app/schemas.py:144`）却从不渲染，管理员给占用中的卡登记了意图，表格里看不出来——N-125 那行的未证实面「前端不显示『在用但已摘走』」到本轮才真的补上；`/undrain` 与 `/healthy` 两条解除路径在控制台里根本没有按钮，而 `app/routers/gpus.py:76-77` 给 `/undrain` 写的存在理由恰恰是「不然一次误点就是一张永久掉的卡，只能改库」。
- 改法：行模板按维度给极——健康一列给「标记异常／恢复健康」、下架一列给「进入维护／撤回下架要求」（`app/static/app.js:1252`、`:1253`，处理器 `:1271`、`:1289`，委托表 `:1330`、`:1332`），占用中的卡也给下架按钮（N-125 之后判得动），状态格在健康徽章旁边挂 `已请求排水`（`app/static/app.js:1249`）。`GPU_STATUS_CN`／`GPU_HEALTH_CN` 的分母判据（N-123／N-126 立的）不动。
- 新判据两把（`tests/test_gpu_drain_provenance.py`，本轮 +4，含一支反证）。`gpu_row_offenders`：GPU 行模板里对 `status`／`health` 的字面量比较，被比的值必须落在**该列自己的枚举**里；别名从 `gpus.map((别名)` 现取（改名躲不过），模板体一律读到 `)` + `.join(` 收口并用 `</tr>` 自证整行读完——嵌套模板串（按钮就是这么写的）会把行读成一截，读成一截时后面的比较静默消失、判据就会在带缺陷的树上假绿；分母为空即判红（模板读不到／一处比较都没有）。`test_every_admin_gpu_verdict_has_a_console_reader`：分母由路由 AST 现取（`@router.post("/{gpu_id}/X")`），`gpu-X` 既要在行模板里、也要在 `actionHandlers` 里——只写按钮不接处理器，点了什么都不会发生，而这两种缺席在页面上同形。
- 常驻浏览器档补上 gpus 视图（`tests/test_browser_console.py::test_admin_gpu_row_shows_the_matching_pole_of_each_verdict`）：测试自己把用户提升成 admin（`UPDATE users SET role='admin'`，rowcount 判 1）再进控制台，两个维度各走一次极性翻转，并核「占用中的卡挂上意图时 `status` 仍是 allocated、工作区停掉之后才落成 drained」；收尾断 `page_errors` 为空，所以任何一次 409／5xx 也会记在案上。
- 四臂电池（`/tmp/n128-battery.py`，靶 `app/static/app.js`，还原写回注入前读到的原文，跑完 `git diff app/` 与开局快照逐字节相同）：A 把健康极改回死比较 ⇒ 尺子与浏览器两侧都红；B 删掉 `已请求排水` 那一档 ⇒ 只浏览器红（结构尺看不见渲染缺席，这正是两条判据互补的证据）；C 摘掉 `gpu-undrain` 处理器 ⇒ 尺子红（`actionHandlers` 少了键）＋浏览器红（点了不发请求）；D 只改确认弹窗正文 ⇒ 两侧都不红（对照：判据不许对无关字面量过敏）。四臂读数全部落在预期上。
- 措辞面：`app/services/scheduler.py:7-11` 的模块 docstring 与 `app/services/scheduler.py:336` 的注释不再把 `unhealthy` 列成状态值；`/undrain` 的 docstring 指向 `/healthy`（`docs/openapi.json` 随 `make api-docs` 重生成，一致性由 `tests/test_version_consistency.py` 机核）；`docs/API.md` 的 `/undrain` 行、`docs/ACCEPTANCE_GATES.md` 的 G0.111 那格（还写着「占用中的卡 `/drain` 回 409」与「`unhealthy` 是一档状态」，两条都被 N-125／N-126 换掉了）与 G0.112 那格的同形措辞一并改口。
- 记账：新增门禁 G0.114；§2 新行 N-128；本轮 +4 支（认证读数 collected 1011／failed 0，迁移链 17 节不变）。N-125 那行「未证实：前端不显示『在用但已摘走』」随本轮闭合。新登记 N-129：管理台的 GPU 表没有周期驱动者——`pollTick` 只刷工作区与指标，且在没有瞬态工作区时整体停摆，于是 `draining`／`drained` 这些由后台同步改判的事实，运维不重新进视图就看不见。
- 未证实／限度：死比较那把尺只看「字面量在 `===`／`!==` 右边」的形状，右端换成变量或 `GPU_STATUS_CN[...]` 索引不判；它的作用域是 `gpus.map` 那一个渲染位（今日全仓只有 `refreshGpus` 一处渲染 gpus，逐处 grep 过）；确认弹窗的话术与后端语义是否一致没有判据（本轮只顺手把「已有分配不受影响」改成按 N-125 的说法）；浏览器档按可用性整档可跳过，缺 Chrome 时这一支不产读数（环境读数在 `dist/VALIDATION_RUN.md`）；端点↔按钮对账不看按钮点了以后请求真的发出去（那半边由浏览器用例覆盖，但它只在有浏览器的那一遍跑）。

### GPU 表的快照要有驱动者：进视图之外还得有人定时重读（N-130，闭 N-129）
- 缺陷（登记项 N-129 的原话）：`refreshGpus()` 只在进视图和点完一次动作之后被叫，`app/static/app.js:644` 的 `pollTick()` 只刷工作区与指标，且 `has_transient()` 一为空就整体停摆。而 `draining`（缺席降级）、`drained`（意图被释放兑现）、`hosts.last_synced_at` 全部由后台同步改判——`docs/OPERATIONS.md` 的排查表让运维「看到一批 `draining` 先查那个节点的同步」，那句话在管理台上读的是一张静态快照。N-93／N-99／N-110 立过的规矩是「有周期驱动的收敛才叫被验证过」，这一格缺的正是驱动者。
- 改法：`app/static/app.js:655` 新增 `GPU_POLL_MS = 15000`（取这个量级的理由写在常量旁：后台改判最慢 120s 一轮，`app/services/worker.py:100`，再密只是重读同一份事实），`app/static/app.js:657` 的 `startGpuPolling()` 与`app/static/app.js:662` 的 `stopGpuPolling()` 接在 `showView` 这个唯一的导航入口上（`:305-306`，进 gpus 起、离开即停），回调里带 `document.hidden`——与 `pollTick` 同一规矩，切到别的标签页不打请求。
- 判据两把位点（`tests/test_gpu_drain_provenance.py`，本轮 +3）。接线尺 `gpu_poll_wiring` 分三格核「有驱动／停得下来／按视图作用域起停」，区段按顶层函数边界切；三种残缺形状（一次性刷新／摘掉 `clearInterval`／起了不随视图停）各打自己那一格，合规形状三格全真，**接线点读不到就判红而不是返回空串**——否则「驱动者被删了」会读成「没有违规」。浏览器档一支常驻用例三档互为前提：① 视图可见时确有 `GET /api/gpus`（没有这一档，③ 的缺席就是空转，从未存在的轮询同样一条都不发）；② 由 API 侧改一张卡、页面完全不碰，行内极性必须在下一个周期自己翻过来；③ 离开视图后的窗口里一条 GPU 读请求都不许有。节奏毫秒数由页面自己报（`page.evaluate("() => GPU_POLL_MS")`），用例不钉常量。
- 四臂电池（`/tmp/n130-battery.py`，靶 `app/static/app.js`，跑完 `git diff app/` 与开局快照逐字节相同）：E1 摘掉 `clearInterval` ⇒ 尺子红（stops）＋浏览器红（③）；E2 把 `setInterval` 换成一次性 `refreshGpus()` ⇒ 尺子红（driver）＋浏览器红（①）；E3 起了不随视图停 ⇒ 尺子红（scoped）＋浏览器红（③）；E4 只把 15000 改成 12000 ⇒ 两侧都不许红（对照：判据不许钉具体毫秒数）。四臂读数全部落在预期上。
- 排障留痕：浏览器判据第一版在 ① 处红——`time.sleep()` 期间 sync Playwright 不派发 `request` 事件，探针结构上收不到任何请求，于是「驱动者不在场」与「事件没送到」同形。改法是等待一律走 `wait_for_timeout`。这条不是判据太严，是采集手法会把假阴性写成结论。
- 记账：新增门禁 G0.115；§2 新行 N-130；本轮 +3 支（读数以 `docs/VALIDATION.json` 为准，两面计数由 `docs_test_counts` 机核，此处不重抄）；`docs/OPERATIONS.md` 那句排查表补上刷新口径（15s 自读、切标签页不读）；登记项 N-129 就地划销。
- 未证实／限度：没有 Chrome 时浏览器档整档干净跳过，那一遍只剩接线尺（它看不见回调真的发出请求，只看见接线在）；多个标签页各起一份轮询，本轮未做去重（`gpuTimer` 只在同一文档内幂等）；15s 与后台 120s／30s 的节奏匹配只按常量写死，没有一条「控制台滞后不超过某值」的判据；缺席降级在真机上要多久被看见仍未测（本机无 NVIDIA 设备）。

### 指针要说得出符号的名字：行还在、说的不是它，从今天起判红（N-131，闭 N-120 的第二半）
- 缺陷（N-120 那条待收口项自己预告的病）：`doc_references` 只判「文件在不在、行有没有越界」。账面里另一类结构上看不见——行还在范围内，说的却不是那句点名的符号。本轮一手代价量（`/tmp/n131-cost*.py`，四轮迭代）现读到 14 处「命名符号 ↔ 位置」配对里 9 处对不上：`_settle_run` 被指到 `app/services/orchestrator.py:405-439`，那里今天是 `stop()`，函数在 511-519；`mark_unhealthy` 在 `app/services/scheduler.py` 里已经是第三次漂（N-126 那行自己就记着从 366 漂到 375，今天它是 401-418）；`docs/code-walkthrough-2026-08-13.md` 那条「某 recover 函数从未被调用」是双错——仓内没有那个符号，而 `recover_stuck_gpu_allocations` 有调用者。
- 判据 `symbol_pointer_offenders`（`scripts/validate_release.py`）：符号的**定义区间**必须覆盖所引行（AST 取函数／类／模块级赋值名的 `lineno..end_lineno`），或符号就在那几行上出现。配对只认两种有明确语法的形状：A) `符号`（`path.py:A-B`；B) `path.py:A-B` 的／处／里 `符号`。
- 窄口径是被代价量逼出来的，不是保守：第一版把「同一行里最近的一个反引号 token」当对端，186 处指针判出 87 条不合格，逐条读大半是配对错——SLURM 的 `sinfo` 被当成 Python 符号，「`app/models.py:55-58` 新增 `GpuHealth`」里符号在指针**之后**却被前一处的 `status` 顶了名。假阳率盖过真漂移的判据等于没有判据，所以宁窄不宽，并把配对总数写进 note；分母为空即判红，与 `doc_reference_offenders` 同一规矩（那种「绿」与「判据被删」同形）。
- 顺带闭一个盲区：指针语料那一句 `glob("*.md")` 不递归，整个 `docs/adr/` 面此前不在任何指针读者眼里。扩面后只多出一条违规——ADR 0009 把 site-packages 的文件写成仓内 `path.py:NN` 形状；外部源要写「第 N 行」，已按那个形状改回（该行确认为 `KUBE_CONFIG_DEFAULT_LOCATION` 的定义，主张本身不变）。
- 判据 3 支（`tests/test_validation_matrix.py`，本轮 +3）：六形状合成反证（合规两态／漂移／符号不存在／文件读不到／分母为空），其中 `inline` 那一档真语料今天走不到（14 处全走定义区间覆盖），只能靠夹具证明那条分支活着；真账面读数 + 分母自证 `pairs>=12`；接线与 ADR 扩面。四臂电池（`/tmp/n131-battery.py`，跑完 `git diff` 与开局快照逐字节相同）：Q1 把一处指针改回「行在范围内但说的不是它」⇒ 红；Q2 同时废掉两种配对语法 ⇒ 红，且红因是分母自证而不是全绿；Q3 摘掉报告里的注册 ⇒ 接线判据红；Q4 只改注释措辞 ⇒ 不红（对照）。
- 落账：新增门禁 G0.116；§2 新行 N-131；N-120 的第二半就地划销，N-121 节里那句「仍未闭」同步改口；9 处漂移指针改到符号今天的定义区间（更正里不重抄旧的坏配对，否则判据自己再造一条要判红的句子）。
- 未证实／限度：只覆盖「紧邻配对」这一窄口径——句子里不带符号名的裸指针（形如「见 path:行号」）仍只有存在性与边界读者，另登 N-132；写在代码注释里的指针（例如 `app/services/warmpool.py:429` 引用 那条注释引用 scheduler 的行号时省掉了仓内前缀）不在语料面内；判据要求符号名与路径在同一句里相邻，跨句引用看不见；`scripts/` 不在 `make typecheck` 的射程（`mypy app edge_agent`），新函数只过了 ruff 与常驻用例；区间判定用 AST 的 `end_lineno`，对被装饰器改写过行号的定义不作保证（本仓未见该形状）。

### 记账脚本在仓内留一个 tmp/，就把文档门顶红了（N-118）
- 缺陷（工具的自我遮蔽，不在产品代码里）：`scripts/validate_release.py:732 _doc_roots()` 按 `ROOT.iterdir()` 现取顶层目录当「仓内根」，而文档门的存在性核对（`doc_reference_offenders` 的 :615-621 那段划界）写死了「只核以既有仓内根目录开头的路径」——它的前提是**根面等于仓库的组成面**。记账脚本把备份落进仓内 `tmp/anchor-patch-n116/` 之后，这个前提就塌了：`tmp` 成了根，CHANGELOG 与 CURRENT_STATE 里那两句**故意不作为指针**的示例路径 `tmp/probe.py` 各产一条假悬空引用，`doc_references` 当场翻红。红因不在文档，也不在产品代码，而在量具自己的落点。
- 一手读数与机制隔离（不靠「删掉之后变绿」倒推）：同一进程里只换 `roots` 入参、其余全部取现值 ⇒「现行 8 个根 → 干净；roots 里加一个 `tmp` → 恰好 2 条同名 offender，差集 2」。`git check-ignore --stdin` 的语义在一次性夹具里实测：输入 `tmp/ dist/ app/ nosuch/` 只回吐被忽略的那两个；不在仓库里时 `fatal: not a git repository`、rc=128、stdout 为空 ⇒ 干净导出下新过滤器退化成 ∅，行为与改前逐位一致。
- 改法：`_doc_roots()` = 顶层目录 ∖ 产物缓存类 ∖ **被 git 忽略的目录**（新增 `_git_ignored_topdirs()`）；`_repo_file_index()` 跟着根面走，不用另改。`.gitignore` 补 `tmp/` 那一行，把这轮的落点规矩写进文本面。
- 判据两支（`tests/test_validation_matrix.py`）：`test_scratch_directories_are_not_documentation_roots` 自己建 `tmp/`（try/finally 收回）⇒ 根面必须不含它、**活调用** `dangling_doc_reference_offenders()` 仍为 `[]`、`doc_reference_stats()["paths"] >= 200`（防我把分母收坏）；`test_run_report_lives_in_the_ignored_directory` 并一条 `tmp` 的文本面断言，不开新支。反向对照不新开：`tests/test_validation_matrix.py:763` 那一支早就用合成 roots 钉过「tmp 不在根面时示例路径不算指针」。
- 三臂电池（靶 `scripts/validate_release.py` 与 `.gitignore`，套件 36 点）：B1 摘掉 git-ignored 过滤 ⇒ 1 红；B2 从 `.gitignore` 删掉 `tmp/` 那两行 ⇒ 2 红（文本面与行为面各有读者）；B3 把根面收成空集 ⇒ 3 红（既有那支的分母自证一起抓到，说明「收成空」不会被读成「全绿」）。恢复后复跑 0 红。
- 未证实／限度：过滤器只认 `git check-ignore`，非 git 检出时整层不生效（今日行为，不是缺陷）；根面按**顶层目录名**判，`sub/dir` 形式的忽略子目录仍由 `_repo_file_index()` 按根收录。

### STOPPED 那一处终态写入自己带齐终结（N-119，闭 N-117）
- 缺陷：`_finalize_stop`（`app/services/orchestrator.py:582`）是全仓 `app/` 唯一一处「写出 `WorkspaceStatus` 终态，而同一条执行路径上此前没有任何终结」的位点。它的配对证据只在调用链上（`_stop_cleanup:448` → `:472`、reconcile 的 STOPPING 档 `:837` → `:846`），而 `stop():421-427` 的「已 STOPPED 就只补做结算与释放」那一档把「STOPPED ⇒ 会话已关」当成结构保证。
- 这个保证为什么不成立（逐行核实）：`_stop_cleanup` 的 `try`（`:446-452`）把 `terminate_for_workspace` 与 `provider.stop` 放进同一段，`except`（`:453-456`）不 `db.rollback()`；而释放准入在命令失败那一档只认 provider 亲口说的 MISSING（`_release_admitted:509`）。于是「终结自己抛错、runtime 确实没了」这一形状会把 STOPPED 与仍为 `connected` 的会话一起提交，此后每一次 `stop()` 重试都走那条幂等档，永远补不回来——两表互相打脸（与 N-82／N-116 同族）。
- 改法：终结收进 `_finalize_stop` 本体（`:596`），排在写 STOPPED（`:597`）之前——写终态的那个函数自己带齐配对，而不是指望调用链里恰好有人做过。
- 判据 7 支（`tests/test_finalize_stop_closes_streaming.py`）。行为面四支：A1 给成崩溃现场（STOPPED ＋卡已回池＋会话仍 `connected`＋两侧端口占着，用 ORM 造，理由写在用例里），`stop()` 必须修得回来且不凭空补一段账；A2 用真代码证前提（`FaultOnFirstClose` 桩的是协作者不是被测代码，provider 自述 MISSING），一次 stop 之后就该 `failed`；A3 不许开火的对照，「恰好关一次」在库里 1×／2× 同形，唯一可分辨的面是 `stream_failure_total` 增量。结构面三支：把 N-116 那把只看 `_reconcile_one` 的尺子宽成全 `app/` 逐 def 扫（枚举名取 AST 根节点，于是 `DeploymentStatus.FAILED` 与 `w.state` 都不入分母），期望写成**应然空集**并同条留`per_file` 分母自证（orchestrator 4 处／warmpool 1 处）；两支合成对照钉极性（同层在前／上层在前算成对；忘了、排在写之后、`finally` 里、跨 `def` 边界都算漏）。
- 两处极性是本轮从草稿翻上来的：B 组草稿钉的是 as-is（`{app/services/orchestrator.py:590}`），A2 的中段钉的是「会话仍 connected」——那都是在描述洞而不是防回归，与修法同批改回应然，否则尺子永远不认自己的修法。
- 四臂电池（靶 `app/services/orchestrator.py` 与尺子本体，11 个套件基线全绿）：F1 摘掉新行 ⇒ 3 红（A1／A2／尺子）；F2 只改顺序、终结排在写之后（行为同）⇒ 1 红（只有尺子点名）；F3 摘掉 `_stop_cleanup` 那处**既有**终结 ⇒ 1 红（A2）——这条读数更正了登记项里的猜测：两处并存不是冗余，早期那处抛错最多损失端口回收，晚期那处抛错会把整段收尾退回非终态，后果面不同；F4 摘掉尺子的枚举名判别 ⇒ 2 红（越界对照＋分母），证明分母不是按属性名蒙出来的。全部 expect 由第一遍读数补齐；恢复后复跑 0 红。
- 顺带把上一轮账面里被这次插行挪动的位点指回实物（`:611→:618`、`:789→:796`、`:798→:805`、`:804→:811`、`:807→:814`、`:830→:837`、`:839→:846`、`:590→:597`），逐条打印读到原文核对；同一抽样暴露出历史账面里三条指针已指向无关行（`:800`/`:833`/`:615`），本轮只登记不修，见 N-120。
- 未证实：真实 Docker／K8s 下 `terminate_for_workspace` 会不会抛（桩替的是协作者，A2 证的是「抛了之后终态写入不被阻断、且会话必须已关」这一半）；尺子不看终结调用是否被包在会吞异常的 `try` 里，也不看它是否真写完（`_stop_cleanup:448`、`destroy:618` 都包着）；值经局部变量的间接写不在射程内。

### 账面里 114 处指针从来没有存在性读者（N-121）
- 缺陷（账面自身的可读性，不在产品代码里）：`doc_references` 的划界是「只核以既有仓内根目录开头的路径」。一手普查读数：`CHANGELOG.md`／`docs/CURRENT_STATE.md`／`docs/ARCHITECTURE.md`／`docs/ACCEPTANCE_GATES.md`／`docs/SECURITY.md`／两份 review 里共 **114 处**指针写成少前缀形式（`scheduler.py:NNN`、`providers/k8s.py:NNN`、`billing.py:NNN` 这一类，NNN 是行号）——它们不进分母，所以**从来没有存在性读者**：那些文件改名或删掉，账面照样全绿。歧义 0 处，外部写法 2 处（`moby` 的引擎 API 文件、site-packages 里的 SDK）。
- 为什么这不是「文档风格问题」：本轮 N-116／N-119 的修法全靠 `file:行` 自证（每条判词都能被别人 `grep -n` 复算）。缩写形式把这条复算路径掐掉了一半——复算的人得先猜那是哪个文件，而「猜」正是这几轮一直在消的东西。
- 改法两步：① 按反引号形状机械改 102 处（basename 唯一命中才改、代码围栏内不动）；② 剩下 12 处是**不带反引号**写的，第一轮正则根本看不见 ⇒ 改成调判据自己的读数逐条改。这里踩到并当场拦下一次：拿「`app/routers/usage.py` 的缩写名＋冒号＋行号前缀」当裸子串去数，会把同一文件里行号更长的另一处（365 行）算成同一处，改成带边界断言的正则（前不接 `[\w./-]`、后不接数字）才让「命中数 = 判据列出的条数」这道闸门成立。改后读数：已限定 200／缩写 0／歧义 0／外部 2。
- 新判据 `unqualified_pointer_readings()`（`scripts/validate_release.py`）是四态机器，关键在**不把看不见折成合规**：已限定只计数（交给既有三类指针判据）；缩写＝违规；basename 有 ≥2 候选＝违规并在理由里列候选（按名字猜会指到别的文件）；外部形状（`moby`／含 `...`／`nvcr.io`／`site-packages`，或该 basename 全仓没有）走豁免但**单独计数**进门禁 note。`dangling_doc_reference_offenders()` 把它的 offenders 并进总清单。
- 常驻判据 3 支（`tests/test_validation_matrix.py`，本轮 collected 976→979）：`test_pointer_spellings_are_fully_qualified_today` 钉真语料棘轮（缩写 0、歧义 0、已限定 ≥150 防分母被收坏、外部 ≥1 证明豁免面有东西在豁免）；`test_the_spelling_clause_fires_on_each_pointer_shape` 用合成语料＋合成索引走四极，外加「`app/routers/usage.py:36` 与 usage.py：365 并存」的边界自证（这里用全角冒号，是为了不把这段说明写成新判据要抓的那种形状）；`test_the_gate_forwards_the_spelling_clause` 钉接线。
- 五臂电池：C1 摘掉门禁对新判据的接线 ⇒ **第一遍存活**——既有两支各自直接调读数函数，所以「报告面不再核」这件事在测试面完全看不见（判据成死码的形状）。这一条就是本轮补第三支判据的直接证据，补上之后 C1 ⇒ 1 红。C2 取消外部豁免 ⇒ 4 红；C3 把歧义阈值从 2 抬到 3 ⇒ 1 红；C4 往 `docs/SECURITY.md` 尾上插一条真缩写指针 ⇒ 3 红（牙齿在真 shipped 文件上证，不只在自己写的夹具里证）；C5 插一条全路径指针 ⇒ 按设计不红。恢复后复跑 0 红。
- 这条棘轮上线第一次就抓到写它的人：本轮刚落盘的同一份账面自己带出 4 处缩写指针（都是我解释「裸 count 会误算同一处」时举的那个文件例子，两个面各写两遍），判据逐条点名之后我把例子里的冒号改成全角——**没有放宽判据，也没有把例子删掉**。
- 连带更正登记项 N-120：它有两半，本轮闭了「缩写形式没有存在性读者」那一半，**指错行**那一半已由 N-131 闭上（窄口径，见上面 N-131 一节）（判据只看边界与存在，不看那句话说的符号在不在这一行所在的作用域里）。
- 未证实：普查按「反引号包住」与「裸写」两种形状各扫一遍，仍有第三种形状（表格单元里、行尾无空格粘连）理论上能躲过 `DOC_POINTER_ANY_RE`；本轮没有对第三种做穷举，只保证现存 200 处已限定。外部那 2 处的豁免靠形状而不是白名单文件清单，未来若上游文档改名会落到「basename 不在索引里」那一支，仍不判违规。




### 本轮新增的待收口项
- ~~`N-64`：`accumulated_seconds` 的累加在账本的幂等保护之外（扣一次、展示与配额算两次）~~ —— **已由 N-74 闭合**：这一列改由账本投影（`app/services/ledger.py:97-109` 新读数口径、`app/services/orchestrator.py:405-439` 结算后 SET 而非 `+=`，返回值同步改成账本认下的秒数）。改前两臂复算都是 `FFF.F.F.`（8 支里 5 开火），一手读数 `列=60、账本=30`。判据 `tests/test_settled_projection.py` 8 支。量出来的两格残留另登记 N-75（指标计数器重放加两次）／N-76（destroy 失败窗口 live 重复计）。
- ~~`N-65`：**`available_credits` 把个人与组织余额直接相加，而行同时带两个归属**（充值翻倍／跨成员拿钱）**—— 已由 N-71 闭合**：读侧分池（组织池只数 `user_id IS NULL` 的行）＋抽一份 `gross_credits` 把三遍相加合一，判据 `tests/test_credit_purse_split.py` 8 支；实测读数从 `available(a1)=2000 / available(b1)=1000` 变成 `1000 / 0`。写侧单一归属（CHECK 或 `account_id` 列）另轮处理，理由是本仓账本 append-only 不回填。
- ~~`N-66`：hold 幂等键按 workspace 全局唯一，二次启动永远拿不到 pending hold~~ —— **已由 N-73 闭合**：键改按轮次发（`app/services/billing.py:272-291`），兜底只按 `workspace_id + status=PENDING` 收敛；实测从 `('hold:ws-A','captured',300)`＋pending 0 变成二启拿到新 pending 且 available 减 300。判据 `tests/test_hold_round_key.py` 7 支。

- ~~`N-67`：provision 失败那一路是「释放先于确认」的第二实例~~ —— **已由 N-81 闭合**：补偿域不再吞信息（`cleanup_attempted`/`cleanup_error` 记下后原样 re-raise，ADR 0002 不破），`_fail` 新增必填 `command_succeeded`，release 与清 GPU 列整体搬进 `if self._release_admitted(...)`（`app/services/orchestrator.py:367`）；不放行档不放卡、不清列、状态写 STOPPING（PROVISIONING 会被 `reconcile_all:675-681` adopt 成 RUNNING 且幂等预筛会把重试记成 SUCCEEDED，两个候选都量过），`error_message` 带原始失败＋provider 回话＋destroy 错误串并自己 commit，`release_hold` 留在门外。判据 `tests/test_provision_release_admission.py` 17 支（＋我补的 2 支「provider 自己抛错 = 问不到」档，guard 落在判据本体里），改前换面 11 开火 `FFFF...F.FFFF..FF`，一手读数「卡却被放回池子里了（一卡双跑）」。登记项原文「`_fail` 的三个调用点」是错的：实测只有一个调用点，两档是同一调用点上的 `terminal=`。
- ~~`N-68`：reconcile 的节点不一致分支把卡放了，却没停那个 pod（同类第三实例）~~ —— **已由 N-77 闭合**：该分支改走 `_stop_cleanup`（`app/services/orchestrator.py:438-475`），认账了才置 FAILED；provider 仍自述 ALIVE 时保持 RUNNING 不写终态、卡不回池，下一轮接着停。判据 `tests/test_k8s_node_truth.py` 的夹具改成有状态（`stop` 计数＋`reconcile` 随停没停改口），诚实档读 `stop_calls == 1`／撒谎档读 `failed == 0`＋`Gpu` 仍 ALLOCATED＋第二趟 `stop_calls == 2`；接线棘轮 `reconcile_consumers` 1→2 并把反向对照改成 2/1/0 三档。改前就地换面复算 `...FF.........FF...`（19 支里 4 开火），一手读数 `节点不一致没有去停那个 pod（stop_calls=0）`。
- ~~`N-69`：warm pool 认领失败那一路把销毁失败只记日志，然后照样放卡并写成终态（第四实例，且三重不可见）~~ —— **已由 N-80 闭合**：撤销分支的放卡改由 `orchestrator._release_admitted` 认账（报错档只认 MISSING、成功档不再是 ALIVE；与 `_stop_cleanup` 的 `:366`/`:370` 同形），不认账时不放卡、不清 `container_name`、不写终态，`_cleanup_failed` 选择集扩成 `in_([FAILED, DRAINING])`（`app/services/warmpool.py:236-268`）使 DRAINING 那一格有了重试驱动者，`billing.release_hold` 保持无条件（计费轴与资源轴分家）。判据 `tests/test_warmpool_claim_admission.py`（11 def／收集 12 例）＋重判的 `test_warmpool_claim.py::test_warm_pool_rotation_failure_only_when_admitted`；我主线换面复算 `FF...FFF.FFF`（8 开火），一手读数「destroy 抛错、provider 说的是 alive，卡却被放回池子里了」（`alloc/gpu_free` 三键全反）。原任务书里「清列等于永远停不掉」的机制已被 N-78 降级，两处文字按现状改写。
- ~~`N-70`：`DockerProvider.reconcile` 用 DB 列推断「容器不存在」，而同一个 provider 的 destroy 会按命名约定把名字推出来 ⇒ 假缺席~~ —— **已由 N-78 闭合**：容器名推导收成一处 `DockerProvider._name()`（start/stop/destroy/inspect/logs/wait_ready/exec/pull_artifact 同源），`inspect` 失败按 stderr 分 absent／unknown（本机实测两形状同为 rc=1：`error: no such object` vs `Cannot connect to the Docker daemon at …`），`reconcile` 把 unknown 读成 UNKNOWN ⇒ `_release_admitted(command_succeeded=False)` 在 daemon 不可达时拒绝放卡；`KubernetesProvider.start/stop` 的空列静默空转同批改掉。判据 `tests/test_provider_absence_evidence.py` 8 支（含真引擎档 `tests/test_docker_provider_integration.py::test_reconcile_asks_the_engine_when_the_name_column_is_empty`）；改前单元档 `F.FFFF..`（5 开火）、真引擎档红在「空列被当成缺席：引擎说这个容器在跑」。
- ~~`N-72`：可用额为 0 时仍允许开机（出厂默认档）~~ —— **已由 N-83 闭合**：启动预授权从「建好了但默认不走」改成出厂就走（`billing_enforce_preauthorization` 默认 True），并补上它缺的那一半：注册时向**个人**池发 `billing_signup_credits=300`（＝一次最低启动窗口，幂等键 `signup:<user_id>`）。定档依据本机读过 Vast.ai 计费文档（「requires pre-payment of credits for GPU rentals」＋「stopped automatically」＋允许「a short grace period where your balance may go negative」）。`check_launch_eligible` 的 `available < 0`（`app/services/billing.py:69`）保留为第二道地板；判据 `tests/test_launch_pricing_default_is_enforced.py` 9 支（恒等判据钉 §8、0 额度 402 的行为档、个人/组织归属结构判据带四种写法对照）；改前 4 开火（`assert False is True` 是数值判决，余下 3 支报字段不存在）。
- ~~`N-75`：`gpu_seconds_total` 在 stop 重放里加两次~~ —— **已由 N-88 闭合**：抬 counter 的那一步改吃**账本净增量**（`_settle_run_delta` 结算前后各读一次 `settled_gpu_seconds`，`delta=after-before`），重放时行已存在 ⇒ `delta==0`；`_settle_run` 的公开契约不动（仍返回账本认下的段值，`capture_hold` 照旧读它）。依据是本机重取的 Prometheus 官方两页（counter「monotonically increasing」、「Do not use a counter to expose a value that can decrease」、累计量以 `total` 为后缀）与本机 `prometheus_client` 0.26.0（外部包，源码不在本仓）的 `metrics.py` 第 337-341 行（负数 `inc` 抛 ValueError），判据 14 支（重放只加一次／第二次 finalize 加 0 而段值仍 30／reconcile 路同样只加一次／hold 读段值／counter 只在一点被抬／**类型必须是 Counter**——换 Gauge 那档实测两把既有门全绿，故补上）。我在主树换面复算 `FF..F........F`（4 开火，读数 `(60.0 - 30.0) == 0`），换回后 13 个邻面文件合跑 170 点 rc=0。顺手量出两条缺口：既有少计 N-89、reconcile 异常穿出整趟 N-90。

- ~~`N-89`：DESTROY 与 reconcile RUNNING→FAILED 两条结算路给 counter 加 0，账本却入了 30 秒~~ —— **已由 N-89 闭合**：抬 `gpu_seconds_total` 挪进 `_settle_run_delta` 的 return 之前（净增量口径本体），两条旁路一起不再少计；停机那一路行为逐位不变，N-88/N-64 的 22 支一字未改照绿。判据 `tests/test_gpu_seconds_booked_paths.py` 6 支，改前复算 4 failed / 2 passed，一手读数「destroy 之后 counter 只动了 0.0」。

- ~~`N-90`：`reconcile_all` 的逐格循环没有异常边界，一格抛错穿出整趟~~ —— **已由 N-90 闭合**：循环体搬进 `_reconcile_one`，外层每格一个 try 且**每格各自 commit**（只在循环末 commit 时，一格的 rollback 会把前面已收敛格子的判决退回——这条是判据第一轮红了才发现的，不是推理）。判据 `tests/test_reconcile_cell_isolation.py` 5 支，改前复算 4 failed / 1 passed，一手读数 RuntimeError 穿出整趟＋errors 计数缺席。

- ~~`N-76`：`destroy` 的 provider 失败窗口让配额门禁把同一段算两次（N-74 的另一半）~~ —— **已由 N-97 闭合**：一段运行的身份就是它的 USAGE 幂等键（`idempotency_key` 带 unique），「这段入账了吗」收成一次点查，「已用秒数」收成 `ledger.workspace_seconds_used` 一处口径，两个旧读者（展示端点与 QUOTA 门禁）都改为调它；第三个读者在浏览器里——`WorkspaceOut` 新增 `usage_segment_booked`，前端两处 `accumulated_seconds + live` 只在它为假时才加。登记原文提的「release 失败那一档不在此列」照旧成立（状态已是 STOPPING）。判据 `tests/test_live_usage_not_double_counted.py` 22 支，改前 14 开火，一手读数 `QUOTA 门禁读到 60，账本 SUM 只有 30`。
- ~~`N-98`：不放行的格没有周期驱动者，一张卡可能被永久钉住~~ —— **已由 N-99 闭合**：`reconcile_all` 加 `limit`/`older_than_seconds` 两个默认 None 的参数（默认档＝改前全量扫描，启动恢复不受影响），`deps._reconcile_stuck_cells` 以 30 tick 注册进既有周期表，只碰「队列没在做」且「比阈值老」的格、一趟最多 8 格；登记项里那条成本读数（256 格一趟 ≈24 s）正是「不做定向档就要付的价」。判据 `tests/test_periodic_reconcile_driver.py` 11 支，四臂单变量各只红它守的那一支。多副本抖动未做，量级与理由写在 N-99 一节的未证实里。

- ~~`N-61`：要不要把构建后端从 setuptools 换成 hatchling~~ —— **已由 N-62 结案：不换**。本机在 `git worktree` 副本上真跑过：hatchling 1.32.4 两建 wheel 同为 `629d6ff7e24f`（它自己就钉 tar 成员 mtime/uid/gid 与 gzip mtime，读安装到本机 venv 的源文件核对过）；与 setuptools 的 wheel 差异只有三处——成员 55 对 56（少 `dist-info/top_level.txt`，全仓 grep 零读者）、`Requires-Dist` 只差 PEP 508 的引号风格（22 条语义同集）、`WHEEL` 的 Generator 行。净收益只是删掉 `scripts/sdist_normalize.py`（约 100 行，6 支判据与两处消费位都已落门禁），代价是 `uv.lock` 重解析、`dev` extra 对齐、wheel 侧 `recomputable` 基线重钉与所有引用产物 sha 的文档面重扫⇒ 不抵。再议的触发条件：自研归一哪天失效，或后端侧出现**别的**产品收益。
- ~~`N-111`：**一台彻底不再同步的节点，它名下那些仍标 AVAILABLE 的卡没人接走**。`expire_stale_hosts` 只改 host 的结论列，不碰 `gpus.status`（`allocate` 的权威是后者），所以节点整机消失后新工作区仍会被派到一张不存在的卡上。不顺手 drain 的理由是**归位那一半还没定**：`DRAINING`／`UNHEALTHY` 今天没有任何回到 AVAILABLE 的路径（只有 admin 路由 `app/routers/gpus.py` 的两个 POST 会写它们，`sync_host` 对仍在上报清单里的卡只更新 model/memory/index 不改 status，`release` 与 `recover_stuck_gpu_allocations` 只碰带 `workspace_id` 的行），于是“节点暂时看不见”一旦变成 DRAINING 就是一次不可逆的容量注销。要么先给一次成功重报定义归位语义（并回答“管理员手工 drain 的卡该不该被自动抬回来”），要么给缺席降级另设一档比 host 判死更长的阈值。~~ —— **已由 N-112 闭合**：分配候选与等锁计数一起吃 `host_is_visible()` 这条 EXISTS，失联节点上的卡不再被派给新工作区；不改写 `gpus.status`，所以节点回来即恢复可分配，管理员的 DRAINING/UNHEALTHY 也不被周期任务顶掉。判据 6 支＋四臂电池，读数见上面 N-112 一节。

- `N-114`：**两张没被 N-112 关掉的账，都记在“失联节点的卡”名下**。~~(a) 它虽然不再被分配，但仍以 `AVAILABLE` 出现在 `GET /api/gpus` 与容量报表里——“能派”与“算空闲”从今天起不是同一个问题，而读侧只认后者~~ —— **(a) 已由 N-122 闭合**：warm pool 的补位闸门改吃同一条 `host_is_visible()`，失联节点上的卡不再被算进余量（判据 `tests/test_warmpool_capacity_visibility.py`，读数见上面 N-122 一节）；(b) ~~`DRAINING`／`UNHEALTHY` 到今天仍然**没有回到 AVAILABLE 的路径**~~（只有 admin 路由的两个 POST 会写它们，`sync_host` 对仍在上报清单里的卡只更新 model/memory/index，`release`／`recover_stuck_gpu_allocations` 只碰带 `workspace_id` 的行）。**(b) 已由 N-123 闭合**：管理员判决有了单独的值 `DRAINED`，`DRAINING` 从此只表示“这次上报里没有它”，一次成功重报即归位；自动路径也不再抹掉人工判决（判据 `tests/test_gpu_drain_provenance.py`，读数见上面 N-123 一节）。剩下的开口不是“回不来”，而是“占用中的卡挂不上判决”，另记 N-124。

- `N-124`：**占用中的卡挂不上人工判决——「占用」与「判决」被挤在同一列**。`mark_unhealthy`（`app/services/scheduler.py:401-418`）没有状态前置，可以直接把一张 allocated 的卡判成 unhealthy，而 `gpus.status == allocated` 正是本仓的占用权威（`app/main.py:36` 的 stuck 保护与一批常驻断言读它）；N-82 那一族「两张表互相打脸」在这张表内部还有一份。N-123 因此把 `/drain` 限定为只作用于池子里的卡（占用中回 409）。—— **N-125 已闭本项**：意图单独立列 `gpus.drain_requested_at`（alembic 第 16 节），`/drain` 占用中也判得动、`release` 时兑现成 drained，`/undrain` 是唯一解除路径；本项剩下的那一半另记 N-126。没有把同样的覆盖动作带给 drained。要同时表达“在用”与“已被摘走”，得把意图单独立一列（K8s 的 `spec.unschedulable` 与 `.status` 分工）：新迁移＋`GpuOut`＋`docs/openapi.json`＋模型↔迁移对账门都会跟着动。先要拍的是“谁有权把一张在用的卡从池子里摘走，以及摘走之后那笔 GPU 秒还计不计费”。

- `N-126`：**`/unhealthy` 仍然直接覆盖占用事实**——N-124 那味药没吃完的一半。`mark_unhealthy`（`app/services/scheduler.py:401-418`）没有状态前置，一张正在被用的卡被判定不健康之后，`gpus.status` 就不再表示「这张卡在用」（对外的 `gpu_allocated` 指标少一张，`app/main.py:34-37`），而那一格的 `workspaces.gpu_id` 仍指着它；N-125 给 drain 立了意图列，健康判决却还是走覆盖这条路。~~要拍的是：unhealthy 应该同样变成一列（`health` 与 `occupancy` 与 `admin intent` 三分），还是像 drain 那样要求先结束占用。两种都要动 `GpuOut` 与前端表格。~~ —— **本项已由 N-126 闭合**：选「`health` 独立成列」那一支（`alembic/versions/391540c0fcb4_gpu_health_column.py` 第 17 节），`mark_unhealthy` 从此只写 `gpus.health`，并配 `POST /api/gpus/{gpu_id}/healthy` 作解除路径；读数见上面 N-126 一节。**形状已定（ADR 0010，`docs/adr/0010-gpu-status-is-three-dimensions.md`，2026-09-28）**：选「`health` 独立成列」那一支，`status` 从此只答调度可用性，并配一条 admin 的 `POST /api/gpus/{gpu_id}/healthy` 解除路径；实现与判据按该 ADR §3／§4 做（含迁移第 17 节）。顺带一条本行的自证：这一项 20:13 落账时写的 `app/services/scheduler.py:366` 在我自己改完注释后已漂到 375——正是 N-120 未闭的那一半，指针的「行还在、说的不是它」在这里当场复现一次。2026-09-29 同一处又漂了一次（`mark_unhealthy` 今天在 401-418）——这一类从此有机检读者，门禁 G0.116。

- `N-127`：**`gpus.health` 只有人在写，没有任何观察来源**。ADR 0010 把判决放对了格子，但「谁发现一张卡坏了」这条链路仍然不存在：mock provider 永远报同样的 8 张卡，`sync_host` 只带 model/memory/index，edge agent 的遥测里也没有温度／ECC 一类健康信号。于是今天的运维现实是：卡坏了没人知道，只有人点了 `/unhealthy` 才算数——而 `draining` 那一档已经证明「有观察、有归位」是可以做的（`health_is_usable()` 与它同层）。要收口得先回答两件事：观察从哪来（provider 上报？设备遥测新增字段？），以及一次坏是否要像缺席那样自动恢复（若自动恢复，就必须再分一档「人工确认过坏了」）。

- `N-115`：**跑完却再也报不上来的部署，今天没有人负责**。N-113 的收口是事件驱动的：设备 POST 一条 `edge-run` 才改判。于是三种形状都会一直停在 `running`——设备跑完即被掐（N-109 的 `reported=false` 档）、设备掉了而部署没重下、以及人工在库外把部署推到 running。要么给 `running` 配一个时效判决，要么在写入侧要求设备在 run 前重新登记；两条都要先回答“谁有权把一条运行判成超时失败”。另外 `DeploymentRecord.updated_at` 带 `onupdate`，它是“最后一次被改动”而不是“最后一次被看见”，不能直接当证据列用——这一格与 N-110 的差别正在这里，故登记不收口。

- ~~`N-117`：**`_finalize_stop` 是全仓唯一一处「本函数不终结会话却写终态」的位置，它的配对证据在调用链上**。`app/services/orchestrator.py:597` 写 `STOPPED` 而函数体里没有 `terminate_for_workspace`；两个正常入口都先在同一趟里终结过（`_stop_cleanup:448` → `:472`；reconcile 的 STOPPING 档 `:837` → `:846`），所以今天的读数不出错。但 `stop():421-427` 那一档是「已 STOPPED 就只补做结算与释放」，它假定「STOPPED 蕴含会话已闭」，而这句既没有判据守着、也不是结构性质：`_stop_cleanup` 的 `try` 把 `terminate_for_workspace` 与 `provider.stop` 放在同一段（`:446-452`），except 不 `db.rollback()`（`:453-456`），于是「终结那一半抛错、provider 又说 runtime 没了（`admitted=True`）」这一形状会把 STOPPED 与仍为 `connected` 的会话一起提交，此后每次重试 stop 都只走那条幂等档，永远补不回来。修法候选：把终结收进 `_finalize_stop` 本体（它是 STOPPED 的唯一写点、函数幂等），并把结构尺子从 `_reconcile_one` 扩到全部 5 处终态写入；动手前要先补一支能开火的判据把上面那个形状做出来——现无任何用例走「终结抛错＋admitted」这一档，所以本轮只登记不修。~~ —— **已由 N-119 闭合**：终结收进 `_finalize_stop` 本体（`app/services/orchestrator.py:596`，写在 `:597`），尺子从 `_reconcile_one` 宽成全 `app/` 逐 def 扫、读数 `set()`；判据 `tests/test_finalize_stop_closes_streaming.py` 7 支＋四臂（F1 3 红／F2 1 红／F3 1 红／F4 2 红）。F3 更正了上面那句猜测：两处并存不是冗余——早期抛错只损失端口回收，晚期抛错会把整段收尾退回非终态。

- `N-120`：**账面里的 `file:行号` 指针会随每轮插行漂移，而没有任何读者**。`doc_references` 门只判「文件在不在、行有没有越界」（`scripts/validate_release.py:637` 那条比较），所以指错了行照样全绿。本轮一手抽样三条全错：`CHANGELOG.md:1406`（N-106 轮）说 `_reconcile_one` 里的 `scheduler.release` 在 `:800`、`_finalize_stop` 在 `:833`，今天读到的是日志格式串与一句注释（真位在 `:812`／`:846`）；`CHANGELOG.md:1295` 说 destroy 的结算在 `:615`，今天是 `return  # 已 tombstone，幂等`。本轮只把自己写下的那批指针修回实物（改前/改后八个位点逐条打印核对），历史账面不回填——回填会把「当时读到什么」这条证据抹掉。可做的修法：给文档门的判据从「行不越界」加严成「指针所在的函数名与句子里点名的符号一致」（句子得带符号名，这需要先在写法上立规矩），或改成引用 `def` 名＋相对偏移这种不因插行漂移的锚形。本轮 N-121 闭了第一半（缩写指针 114 处全部改成全路径，并加棘轮：新增缩写即红），~~第二半——「行还在、说的是别的东西」——仍未闭，缺的是符号一致性判据，而它的语料需要先能区分「句子里点名的符号」与「指针所在作用域」，实测只能给下界（已限定的 200 处里，句子里带可点名符号的只有约一半能判）。~~ —— **第二半已由 N-131 闭合（窄口径）**：`symbol_pointer_offenders` 认「`符号`（`path.py:A-B`」与「`path.py:A-B` 的／处／里 `符号`」两种紧邻语法，要求符号定义区间覆盖所引行或符号就在其上；落盘前现读到 14 处配对里 9 处对不上，已全部改回实物（门禁 G0.116）。剩下那一类不在本判据射程：句子里不带紧邻符号名的裸指针，以及写在代码注释里的指针——另登 N-132。
- `N-132`：**裸指针与代码注释里的指针仍没有符号一致性读者**。N-131 的判据只认「符号与路径在同一句里相邻」两种形状；「见 path:行号」这种不带名字的指针、以及写在源码注释里的指针（现成例子：`app/services/warmpool.py:429` 那条注释引用 scheduler 的行号时省掉了仓内前缀，那种形状没人核）仍只有存在性与行边界两种读者。要收口得先定写法规矩：要么强制指针一律带符号名（可由 N-121 那把拼写尺加一条），要么把注释里的指针纳入语料面并按同一判据跑——后者会让 `app/` 里几百处注释进分母，先量代价再决定。


- `N-32`：CI 改按锁装之后，`docs/VALIDATION.json` 才第一次"可能"在 runner 与本机之间逐字节相等；
  这条主张**未在真 runner 上验证过**（不能推送），本机侧只用"同树两次跑 + 换环境"两档做了替代实验。
- ~~`N-129`：**管理台的 GPU 表没有周期驱动者**。`app/static/app.js` 的 `pollTick()` 只刷工作区与指标，且 `has_transient()` 一为空就整体停摆；`refreshGpus()` 只在进视图和点完一次动作之后被叫到。于是`draining`／`drained`／`last_synced_at` 这些**由后台同步改判**的事实，管理员不重新进一次 `#/gpus` 就看不见——`docs/OPERATIONS.md` 的排查表让运维「看到一批 `draining` 先查那个节点的同步」，而它说的读法在控制台上是静态快照。N-93／N-99／N-110 立的规矩是「有周期驱动的收敛才叫被验证过」，这一格同样缺驱动者。要收口先拍两件事：刷新的作用域（只在 gpus 视图可见时轮，还是常驻轮）与节奏（后台同步本身是分钟级，秒级轮询只是把控制台的读压放大），并补一条常驻浏览器判据：卡在被同步改判之后，不改哈希、不点任何按钮，表格必须自己跟上。~~ —— **已由 N-130 闭合**：`GPU_POLL_MS` 15s 的周期重读接在 `showView` 这个唯一导航入口上，起停按视图作用域；判据一把接线尺（driver／stops／scoped 三格＋三态反证）＋一支常驻浏览器用例（可见时确有 GET／后台改判自己翻极性／离开视图后一条都不许有），读数见上面 N-130 一节。
- ~~`N-33`：`tests/test_gpu_pool_guard.py` 那两支带哨兵（缺余量时算合法跳过）~~ —— 已由 **N-41 闭合**：
  夹具显式达成前置并断言，哨兵与闭集条目一并删除，条件跳过归零。

- ~~`N-79`：「存在但没在跑」被判成缺席（N-78 没动的那根轴）~~ —— **已由 N-96 闭合**，但登记原文的一半是假的，先更正再记账：`paused`/`restarting` 两档引擎自述 `Running:true`，改前就已经是 ALIVE，不是缺陷；承重的是 ① K8s Pending（只读 `available_replicas`，docstring 承诺的「0 副本 → MISSING」代码从没做）与 ② docker 兜底把 `created`/`removing` 落到 MISSING。改法与四处依据（moby `state.go` 七常量与 `container.go:76` 原文、pod-lifecycle.md 第 114 行、SDK 31.0.0 属性表）见上面 N-96 一节；判据 20 支，改前 9 开火。代价另立 N-98。

- ~~`N-82`：回收器强制放卡时不清 `workspace.gpu_id` ⇒ 两张表互相打脸~~ —— **已由 N-84 闭合**：`recover_stuck_gpu_allocations` 现在在同一事务、`commit` 之前，把自己判定为孤儿并放掉的那几格的 `gpu_id/gpu_index/gpu_name` 一起清空；受保护／占用的格一列都不动（`orphan_ids` 在 `db.delete` 之前记名，两个放卡分支各自先读 `Gpu.workspace_id`）。判据 `tests/test_recover_gpu_column_drift.py` 24 支（行为档逐状态核成对＋结构档按 AST 数 `unpaired_release_paths`＋六支变异控制各自只翻一格）；我在主树换面复算改前 `16 failed, 8 passed`、改后 `24 passed`，邻居 111 点全绿。范围只到『回收器自己放掉的格』：另外两处放卡点（`_stop_cleanup` step 4、warm pool 撤销档）仍不清列，且它们产出的漂移回收器看不见——登记为 N-86。

- ~~`N-86`：放卡的另外两处不清列，回收器看不见它们产出的漂移~~ —— **已由 N-86 闭合**：成对清列收进 `Scheduler.release` 本体（ORM 取回 holder 后同事务清 `gpu_id/gpu_index/gpu_name`），`_stop_cleanup` 放行档与 warm pool 撤销档不再各写一遍。判据 `tests/test_release_pairs_binding.py` 7 支，改前复算 4 failed / 3 passed，一手读数「release 之后同一会话仍读到绑定」。详见上面那一节。

- ~~`N-104`：`billing.release_hold` 在 provision 失败那一档抛错时只留一条 warning（`app/services/orchestrator.py:365-366`「释放失败不得掩盖状态置位」），残留的 pending hold 由 TTL 清扫（`billing.py` 的 `release_expired_holds`，已注册进周期表）兜底。没有任何常驻用例从「release_hold 抛错」驱动到「TTL 把这一笔收掉」——`tests/test_credit_holds.py` 只驱动 TTL 到期那一档，`tests/test_periodic_loop_and_boot_wiring.py` 只断注册在位。判据形状与 N-101 同构（残留 + 补做者 + 幂等）。~~ —— **已由 N-104 闭合**：三档判据 + 五臂电池（时间谓词与 RUNNING 保护各有独立牙）。见上面 N-104 一节。
- ~~`N-105`：`destroy()` 的结算抛错档写下「留下可审计、可补偿的痕迹」却没命名补做者（`app/services/orchestrator.py:616-621`）；`tests/test_streaming_lifecycle.py:242-266` 注入了这一档的抛错，但只断到痕迹与「释放照走」，没断补偿到底来不来。要拍的首先是应然：这一段的账是等 TTL、等 reconcile、还是根本没人补——命名了补做者才谈得上补判据。~~ —— **已由 N-105 结案：不补，改口**。判决是「这一段不入账、也不许按 utcnow()−started_at 补」（补即多计）。见上面 N-105 一节。

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
  上游那份《getting-started 安装》文档第 12–16 行**列了三个官方注册表**（不是本仓路径），其中
  `public.ecr.aws/aquasecurity/trivy`（同一文件第 16 行）当场可拉，且第 22 行明文支持
  "挂容器引擎 socket 扫镜像"这种接法。六维逐项出处：License 两份都从
  `raw.githubusercontent.com/.../LICENSE` 读到 Apache-2.0 正文；活跃度取
  `api.github.com/repos/{anchore/syft|aquasecurity/trivy}` 与 `releases/latest`
  （syft v1.52.0 发布 2026-09-17／9,613★，trivy v0.74.0 发布 2026-08-14／38,083★，两者仓库
  pushed 都是 2026-09-25）；签名形态是 31 vs 47 个 release 资产（cosign 签名 checksums vs
  逐件 sigstore）。第三个候选 `docker build --sbom/--attest` 本机直接不可用
  （`docker buildx version` → `docker: unknown command: docker buildx`）。
  **一处自我更正**：先前把"trivy 运行时要下载漏洞库"记成它的安全风险——真跑之后 trivy 自己打印
  「`--format cyclonedx` disables security scanning」（与它 SBOM 页第 203 行一致），那条主张撤回。
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

### amd64 复算的验收从"扫目录名"换成"读 ELF 头"，并让它带上退出码

- `make amd64-probe` 第 5 步原来打印的那行架构读数是
  `ls site-packages | grep -oE "x86_64|aarch64|arm64" | sort | uniq -c` → 实测打出来是 **`1`**。
  这个数字**几乎不证明任何事**：wheel 装完之后 `.dist-info` 目录名被归一化掉了平台标签，
  所以扫目录名既数不清原生扩展、也不看真正的二进制。改判为逐文件读 ELF 头的 `e_machine`
  （偏移 18 的小端 16 位；62＝x86-64，183＝AArch64），并加了三条判决：
  **分母为 0 直接 FAIL**（一个 `.so` 都没扫到＝这条判据与恒真同形，不能读成"干净"）、
  **混进非 x86-64 FAIL**、**只有 x86-64 且非空才 PASS**。
- 一手读数（真 amd64 运行时、qemu 模拟，跑了两遍，第二遍是为了确认复现）：
  `.so 文件数= 22   按 ELF e_machine 分布= {'x86-64': 22}`、`machine= x86_64`、
  `sqlalchemy= 2.1.0 psycopg= 3.3.6`、`alembic 1.20.0`、`sync_rc=0`、`VERDICT=PASS`。
  也就是说：旧那行报"1"，实际场上有 **22** 个原生二进制，全部 x86-64——旧判据把 22 件东西看成了 1 件。

### 往登记表插行的两次连带发现（都只有"读回磁盘"看得见）

- **我自己又犯了那一条**：给登记表加 N-25/N-26 时，`old_string` 取的是 N-24 行的**行首前缀**——
  替换之后 N-24 的行尾被接到我的 N-26 之后（一行被劈成两行、N-24 的编号前缀当场消失），
  而且两行还插在了 N-24 **前面**。修法是按**行索引**做三行轮换，并写盘后读回磁盘复算：
  编号序列单调（26 行、末行 N-26）、每行格数等于 3（先剥掉转义 `\|` 再 split）。
  顺序倒置这件事 `docs_row_order` 那条门理论上兜得住（它核"带编号的登记表行按号递增"）——
  这一句是**推断**：我没有为了证明门有牙而把错位重新引入一次，实际兜住它的是提交前的那次读回复算。
- **顺手做的格数普查量出一条 HEAD 里就存在的旧伤**：N-21 那行写的是 `` `docker run|pull` ``，
  代码块里的裸管道在 GFM 表格中**照样分格**，所以该行一直是 4 格而表头是 3 格（处置列被劈开）。
  同批新加的两行因为提前把管道写成 `\|` 而是 3 格——两种写法渲染出来都"看着对"。
  已按 GFM 规矩就地补转义。**没有任何常驻机器读者原先看着这一格**：`docs_row_order` 只认编号不认列数，
  列数这次是我插行脚本的后置断言，不是常驻门——要不要下沉为常驻判据留下一轮判断。
- **判据现在进退出码**：原来脚本无论打印什么都退 0，"打印了 FAIL"与"这一步没跑"在 `make`/CI 那一层
  完全同形。第 6 步改为汇总 `SYNC_RC` 与 `ARCH_VERDICT` 两半，任一不成立 ⇒ `exit 1` 并点名是哪一半。
  这条管道本身按四档验过：`(0,PASS)→0`、`(0,FAIL)→1`、`(1,"")→1 且两半都点名`、`(127,MISSING)→1`；
  重跑整支脚本 `make_rc=0`。（顺手记一条自己的捕错：第一次取 `make` 退码时我在 zsh 里写了
  `${PIPESTATUS[0]}`，zsh 的数组是 1 基且名字是小写 `pipestatus`，于是打出空串——
  读数作废，改用 `bash -c '…; echo rc=$?'` 才拿到真值。）

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

### 补上本轮自己撞见的一条越权面：warm pool 的两个观测端点普通用户可写

- 起因不是评审也不是巡检——是给"P50<15s／P95<30s"那句 SLA 找读数时，为了跑 `iterations=20` 的
  基准而先读了一遍代码，发现 `app/routers/streaming.py` 这两个端点只挂 `CurrentUser`。
  **三条一手读数**（RED 档，改之前跑出来的）：普通用户 `GET …/warmpool/benchmark?iterations=1`
  回 **200** 并真的建了一个 workspace；普通用户 `GET …/warmpool/metrics` 回 **200**，body 里是
  **全部 5 个模板**的池水位；一次 `iterations=2` 的调用之后库里留着 **4 个活体**无主 workspace
  （`user_id=None`、不计费、还占着 GPU）。而 `iterations` 在服务侧只有下限（`max(1, iterations)`）。
- **改的三件事**：两端加 admin 检查（沿用仓内既有惯例 `app/routers/gpus.py:13`，不另造一套依赖）；
  上限 `BENCHMARK_MAX_ITERATIONS = 20` **只定义一份**，路由拿它算 `Query(ge=1, le=…)`、服务拿它夹紧
  （两个面各写一个数迟早漂移）；每轮测完在 `finally` 里 destroy，回收失败只记日志不吃读数，
  返回体加 `iterations`——小样本下 p95 就是 `max()`，读数不自带样本数就会被当成百分位。
- **顺手改掉一条把缺陷钉成基线的旧断言**：`tests/test_warmpool.py:322` 原来断言
  `len(statuses) == 2` 且状态属于 {RUNNING, FAILED}，等于把"跑完留下两个活体"写成期望。
  现在钉的是更强的形状：两行仍在（tombstone 语义留给审计），但状态必须是 `DELETED` 且
  `deleted_at` 非空。**这条是本轮最该记的一笔**：常驻用例不只没拦住这个泄漏，它替泄漏作了证。
- **claim 的墙钟从此可观测**：新增 `warm_pool_claim_seconds{template_id}`（buckets 含 15/30，
  与 SLA 目标同值），只给成功交付的 claim 记时——池空／竞争失败／轮换失败都返回 None，
  把它们计进来会让"池越差、P95 越好看"。两头各一支控制（成功必须 +1、None 必须不 +1）。
- **端到端读数**（真 HTTP 路径，不是单测里的替身）：补池到 8 个 READY → 普通用户
  `POST /api/workspaces` → **201**、交付行 `name=warm-cartpole status=running`（证明确实走 claim
  而不是新建），客户端 **15.3 ms**、服务端 `count=1.0 / sum=0.00719`（7.2 ms），池 8 → 7；
  admin `iterations=20` → p50 **3.79 ms**／p95 **4.48 ms**／max 4.99 ms，并核到 p95 取的是最近秩那一格（下标 18）；
  `iterations=21` → **422**；非 admin 两端 → **403／403**。
- **这些数不等于 SLA 达标**（写清楚免得被复用）：mock provider 的 `wait_ready` 直接 `return True`、
  `provision` 只 `mkdir`，所以量到的是**控制面自身那一段**。绝对值那一格仍在
  PHYSICAL_VALIDATION_PENDING 里等 G1–G4 的真机，本轮关掉的是"这一格今天能不能测"。
- **顺带量出一条开放项（N-29）**：`maintain()` 的补位是**按模板逐个**算
  `missing = warm_pool_size − (ready + prewarming + legacy)`，整池需求 = `size × enabled 模板数`，
  没有一处跟舰队卡数对账。取证时 `WARM_POOL_SIZE=2` × 5 模板 = 10 个占位 > mock 的 8 张卡，
  日志当场出现两条 `No GPU available with >= 16 GB VRAM` 并进 1s backoff。生产默认 `enabled=False`
  且 `size=1`，今天没有活体受害者；要不要设舰队级封顶是产品裁决（全局显存预算？模板优先级抢占？
  k8s 与 docker 的容量口径不同），所以只开行留读数，不顺手改语义。
- 门禁目录里此前没有这一格，新开 **G0.39**；`docs/SECURITY.md` §5 补一条规则
  （有副作用的观测端点一律 admin、参数必须有上限、跑完必须自己收）。
- **我自己写的第一版反证有两处毛病，都是全套跑出来的**：
  ① 那条"不留占卡"的断言最初写成**全库** `ALLOCATED == 0`——隔离跑绿、进全套立刻红
  （`基准留下了 1 张仍被占用的卡`），因为别的模块合法地留着自己的 running workspace。
  判据的作用面搞错不是"多测了一点"，是**替别人记账**：别人一留东西就轮到我红。改成只数
  "仍绑在 `bench-%` 那些行上的卡"。② 补上第二条反证之后才发现第一条反证打不到它：
  把清理整体跳过，红的是"留下 2 个活体"那一支，GPU 那一支**从来没被证过**——
  于是加了 `test_the_allocated_gpu_clause_can_actually_fire`：只把 `scheduler.release`
  换成空操作（tombstone 照做），断言此时**必须**读得到卡被占着，读不到就说明探针恒真；
  夹具在 `finally` 里把释放补做回去，不留残骸。
- 计数：新增常驻用例 6 支（HTTP 面 5 ＋ claim 延迟 1），全套 545 → 551（passed 550 / skipped 1 /
  failed 0，两份计数面由 `make validate` 的 `docs_test_counts` 现算核对）。
- **一条我自己写的"独立重算"是假对账，被自己的复跑当场戳穿**：取证脚本最初这样核对分位数——
  实现里算 `p50 = statistics.median(sorted(samples))`，我在旁边又写了一遍
  `abs(statistics.median(s) - data["p50_s"]) < 1e-9` 并称"两把尺一致"。那是**同一个函数调用两次**，
  永远为真，什么都不证明（与"判据读不到事实却报一致"同族）。改判成核**分位索引**：
  n=20 时最近秩 p95 取下标 18，既不是最大也不是最小——这一条能抓住实现里的 off-by-one。
  第二次跑（`make warm-sla` 复跑）同时暴露了另一件事：mock 下这些绝对值本身会抖
  （p50 3.79 → 2.44 ms、claim 服务端 7.2 → 3.7 ms），所以文档面从"钉一个数"改成"记范围 + 给复跑入口"。
- **取证脚本进了仓库**：原先那次数值来自 `/Volumes/Extra/qoder-scratch` 里一个不入库的脚本，
  下一个读者照名字根本重跑不出来。现在它是 `scripts/warm_pool_sla_lab.py` + `make warm-sla`
  （与 `make policy-bench` 同一形状），带退出码：三条反向对照任一不符或分位索引对不上就 rc=1。
  本轮实跑 `rc=0`，读数为 `[warm-sla] overall=PASS（绝对 SLA 判定仍需 G1–G4 真机）`。

### 池子把舰队吃干：`size=2` 时 5 个模板的交互启动 0/5，现在有了闸门与预留开关

- 这条不是评审交回来的，是**给上一条 SLA 取证时顺手量出来的**：`make warm-capacity` 在 8 张异构卡 +
  真 5 份模板的舰队上跑 `size ∈ {1,2,4}`，改前读数 `size=1` → 占 5 张、剩 3 张、交互 **5/5**；
  `size=2`（需求 10 位）→ 开出 **10 格**、READY 8、剩 0 张、交互 **0/5**；`size=4` → 16 格、交互 0/5。
  即池子的补位公式（`size − ready − prewarming − legacy`，**逐模板**）里没有任何一项与舰队容量有关。
- **选型（四条候选，全部开过一手文档；比较改变了默认值与形状）**：
  1. *Kubernetes 的 capacity reservation*（`kubernetes.io/docs/tasks/administer-cluster/reserve-compute-resources/`）：
     `Allocatable = Capacity − Kube-Reserved − System-Reserved − Hard-Eviction-Threshold`，调度器不许超卖 Allocatable；
     预留是**逐资源的绝对量**（`kubeReserved: {cpu: 100m}`），只有 eviction 阈值才允许百分比。
     → 借它的语义：本次实现取"绝对张数"而不是我最初想的比例，且 `allocatable = capacity − reserve` 同构。
     另外它是 **node 级**的，正对应我们"每张卡各自算账"的形状。
  2. *YARN Capacity Scheduler*（`hadoop.apache.org/docs/stable/hadoop-yarn/hadoop-yarn-site/CapacityScheduler.html`）
     `capacity`（floor，各队列之和必须 = 100）＋ `maximum-capacity`（ceiling，"limits the elasticity"）。
     → 结构上更像"多租户配额"，我们要的是"一个池对一份容量"，floor/ceiling 那对语义在这里没有第二个队列可用；
     借的是"池不许吃掉全部弹性"这一点。
  3. *Kueue*（`kueue.sigs.k8s.io/docs/concepts/cluster_queue/`）`nominalQuota` / `borrowingLimit` +
     `reclaimWithinCohort` 抢占。→ 需要跨队列借用与抢占，本仓只有一个池、没有租户间借用，引入它是过度设计。
  4. *Slurm* `OverSubscribe`（`slurm.schedmd.com/slurm.conf.html`）默认 NO＝独占。→ 语义相近但那是作业排程器，
     我们没有 partition 概念，借不到实现。
  未找到一手出处的两条也记在这里：**K8s 上游没有任何"预留 N 张 GPU"的旋钮**（extended resources 不许超卖，
  kubelet 的预留减法只覆盖 cpu/memory/ephemeral-storage/pid/hugepages，device-plugin 资源是**覆盖写**进
  allocatable 的），Slurm 的 `ReservedNodes` 在现版 `slurm.conf` 手册里 0 命中。也就是说"给 GPU 留 headroom"
  这件事没有可直接引的成熟旋钮，能借的只有 K8s 的**公式与绝对量形状**。
- **择一决定**：自研两道闸门，但**形状与措辞借 K8s**（逐资源绝对预留 + `allocatable = capacity − reserve`，
  而且照它"预留不许超过容量"的方向补了可见信号，见下条）；不引依赖（Kueue/ClusterQueue 需要一个不存在的
  第二队列，Slurm/YARN 是另一类调度器）。`warm_pool_reserve_slots` 默认 **0** 的理由是实测而不是习惯：
  `size=1` 在 8 卡上占 5、交互 5/5，没有现实受害者；默认改成 1 会让单卡机器上池子永远空着。
- **过程中把自己写坏一次**（这条最能说明为什么反证要成对）：第一版闸门按**模板**各查一次"有没有够用的卡"，
  两个模板看见同样两张卡 ⇒ 同一张卡被许诺两次，`size=2` 仍开 10 格、交互 0/5，
  而我先写的 4 支用例（不开装不下的格／卡够了必须开／reserve=1 留一张／reserve=0 填满）**全绿**。
  补的那支是"2 张卡 × 2 个模板 × size=2 只能开 2 格"的共享预算用例——它一红，闸门改成跨模板的多重集预扣，
  实测 `size=2 + reserve=2` → 6 格、剩 2 张、**交互 5/5。
- **照 K8s 的另一半语义补了"预留大于容量"的可见信号**：kubelet 那条公式的方向是"预留不许超过容量"
  （它的校验器遇到 `reservation > capacity` 是报错，不是悄悄归零）。这里不能抛异常（抛了会打死 worker 循环），
  所以采取能落地的同语义版本：`reserve ≥ 当前空闲卡数` 且本轮本来要补位时，打一条
  `warm pool reserve=N leaves no room in M free card(s)` 的 WARNING，并把跳过的格数计进
  `stats["skipped_no_capacity"]`。用例两头都在：`reserve=3`／2 张卡 ⇒ 池子 0 格 **且必须有 WARNING**
  （没有这条，配置写错的人只会看见"池子怎么老是空的"）；`reserve=0` 同形状照旧填满 2 格。
  先写测试时它确实红了（`assert []`）——三条前置断言全过，唯独日志为空，说明缺的就是信号本身。
- 计数：本条切片新增常驻用例 6 支（容量段 5 支：不开装不下的格／卡够了必须开／共享预算／reserve 留一张／reserve=0 照旧填满；外加 over-reservation 可见信号 1 支），全套 551 → 557（passed 556 / skipped 1 / failed 0；两份计数面由 `docs_test_counts` 现算核对）。`make warm-capacity` 一并进 Makefile（与 `make warm-sla`／`make policy-bench` 同一形状：人工/CI 档，不进每轮 validate）。
### 一条 P0 安全规则此前只有 import 期的一句裸 if 在守（§7：不能轮换凭据 ⇒ 不许走 warm pool）

- 触发点是上一条切片留下的问题：warm pool 只在"能换掉 runtime 密码"的 provider 上才安全。
  规则写在 `app/deps.py` 装配段（import 期 `if ... : settings.warm_pool_enabled = False`），
  `WarmPoolManager.claim()` 自己**只看** `warm_pool_enabled`；而 `tests/conftest.py` 把 provider
  钉成 mock ⇒ **没有任何常驻用例能证那句在生效**。全仓 grep `supports_credential_rotation`
  在生产侧只有 provider 定义与 deps 那一句。
- 改前的真实后果不是泄露凭据，而是**白烧**：误开的 claim 会占掉一格 READY →
  `rotate_credentials()` 返回 False → 补偿拆 runtime → 账圈了又退 → 才返回 None。
  第一版用例就红在这里：断言"返回 None"改前也通过，加上"那一格的五项状态必须原样不动"才暴露出
  `state/status/user_id/container_name/password` 全被改掉（判据要挑能区分的那一条，不是挑能过的那一条）。
- **两处补齐**：装配期约束抽成 `apply_provider_constraints(settings, provider)`（行为逐字不变，
  只是变成有常驻读者的函数，三档断言含"已关→保持关"防它退化成"永远写 False"）；
  `claim()` 入口加第二道闸，不依赖装配期那句一定在场。测试用真 `DockerProvider`（它的
  `rotate_credentials` 是纯 `return False`，不碰守护进程）而不是假 provider。
- `docs/SECURITY.md` §5 补了这条规则——此前它只出现在 `docs/ARCHITECTURE.md` 的 provider 接口注释里，
  一份"谁都不核"的安全约束。
- 计数：本条新增 2 支常驻用例（装配期约束三档 ＋ 真 DockerProvider 的入口拒绝），557 → 559。这一跑的 skip 从 1 变 2 是**通道抖动**而不是代码红：`test_pinned_base_ref…is_fetchable` 按设计在三态分流里选了 PENDING（daemon 传输答 not found、第二条传输逐字节确认摘要在）——上一轮为这条形状写的分支今天第一次在真实抖动下生效，读 skip 要按用例名读，只比总数会把环境事件当成覆盖率变化。
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
- scripts/validate_release.py 自动生成 docs/VALIDATION.json 与 docs/VALIDATION.md（CI freshness 门禁）。
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

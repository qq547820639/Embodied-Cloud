# Changelog

## 0.7.0 — 2026-09-26（Sim2Real 从"控制面替设备走状态机"变成真设备通路）

`docs/VALIDATION.json`（`make validate` 生成）：collected 723 / failed 0。这份提交面现在**只放换机器重跑逐字节相同**的门禁；"本次跑跳过哪几支、各集成档是 PASS 还是 PENDING"属环境读数，改落 `dist/VALIDATION_RUN.{json,md}`（gitignored）——理由与判据见下方"计数面按可复现性分档"一节。
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
  目标写成 FAILED，正落进上面那条回收器的盲区；改成 STOPPING 是读了 `scheduler.py:257-267`
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
  （`providers/k8s.py:328-329`）会把一张没人用的卡永久钉住 ⇒ 补上"命令失败但 provider 亲口
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
- 计数面 665→678→686→693→701→702→711→723，门禁 G0.72／G0.73／G0.74／G0.75／G0.76／G0.77／G0.78。

### 两个钱包终于不相交：成员行不再被算进组织池（N-71，闭合登记项 N-65）
- 缺陷（本轮先量后改，/tmp 探针跑真对象）：`billing.py:191-198` 把
  `ledger.balance(user)` 与 `ledger.organization_balance(org)` 直接相加，而账本行常态是
  **两个归属一起写**（usage 见 `ledger.py:100-111`，充值/调整见 `routers/usage.py:68-77`），
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
  它只约束写入，不回头改历史行。本仓 `models.py:400` 明写“账本 append-only，不回填改写”，
  所以历史双主行只能靠读侧谓词归池 ⇒ 择一：借 Odoo 的“恰好一个归属”语义先做读侧分池，
  写侧 CHECK／`account_id` 列留作另轮（需要迁移与归池决策，不能顺手加）。
- 改法：`ledger.py:76-92` 的 `organization_balance` 加 `user_id IS NULL`；
  `billing.py:178-189` 新增 `gross_credits`，把重复三遍的相加合一
  （`available_credits` :198、`reserve_launch` :230、monitor 投影余额 :668 都改读它）。
- 判据 `tests/test_credit_purse_split.py`（8 支）：成员行只进个人池（a1 可用 1000、
  组织池 0）；同组织无钱者可用 0；**无主组织行对两个成员都可见**（合规侧，5000/5100 与
  既有用例同数）；别人的消耗不扣我；无组织用户不受影响；相加表达式在 app/ 里按 AST 数
  恰好一处（`def gross_credits` 也恰好一份）＋合成源码反向对照（再加一份就读到 2）；
  三个消费位各自核 `gross_credits` 调用数。
- 一处自我更正（判据写错，不是代码错）：我第一版把“可用额为 0 就该被启动门禁拒”写成应然，
  实测 `check_launch_eligible` 只挡负数（`billing.py:~80` 的 `if available < 0`），而
  `config.py:56` 出厂默认 `billing_enforce_preauthorization=False` ⇒ 0 余额确实能开机。
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
  对照档全新 ws-B 仍拿到 pending 300。调用方 `orchestrator.py:190` 把返回值丢掉 ⇒ 全程无报错。
- 改法：`_hold_key`（`billing.py:272-291`）按该 workspace 已有 hold 行数发轮次号
  （`hold:{ws}:{n}`，n=0,1,2…），hold 表不删行 ⇒ 新键必然空闲；兜底查询（:254-268）改成
  只按 `workspace_id + status=PENDING` 收敛，找不到 pending 就照原样抛——
  宁可 provision 失败重试，也不能把一笔已花掉的额度当成新授权。
- 跳过外部调研的理由（如实记）：改动只落在一个方法的键推导与一条查询谓词上，约束全部来自
  仓内既成事实（全局唯一列 `models.py:447`、部分唯一索引 `models.py:459-465`、hold 表
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
  读数口径；`_settle_run`（`app/services/orchestrator.py:405-439`）结算后 **SET** 这一列，并把
  返回值从"本次重算的 elapsed"改成"账本实际认下的秒数"（`booked`），下游 `capture_hold` 与
  `record_gpu_seconds`（:441-456）据此行动；投影是 SET 而非 patch，所以历史上被 `+=` 吹起来的值
  会被纠正（C3）。
- 为什么"从账本派生"是补口径而不是新发明：`settle_workspace_run` 是 USAGE 行的唯一写入方——
  我自己重扫（不采信子代理读数）：`LedgerType.USAGE` 在 app/ 只出现在 `ledger.py:106`（本轮新增的
  读数谓词）与 `ledger.py:129`（写入本身），枚举定义在 `app/models.py:50`；`record()` 的另两处
  生产调用是 `app/routers/usage.py:68`（RECHARGE）与 :101（ADJUSTMENT）；`settle_workspace_run(`
  的生产调用点只有 `orchestrator.py:423`，其余命中全在 tests/。
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
  ② `destroy` 的 `provider.destroy` 抛错那一路（`orchestrator.py:464-502`，故意上抛让 DESTROY 重试）
  会留下"RUNNING 且这一段已结算过、`started_at` 未清"的窗口，`course_usage_seconds` 的 live 项
  （`billing.py:396` 的状态门）于是把同一段再算一遍——实测
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
  停没停改口；`stop` 的返回形状照抄 `providers/k8s.py:317` 的 None）。两支各钉一极——
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
  并翻掉两支常驻绿用例。现按 `_stop_cleanup` 既有的两个消费点同形接线（`orchestrator.py:366` 成功档
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
  被抹掉、重试只能靠推断"。已把 `warmpool.py:413-416` 与测试 docstring 两处按现状改写，
  并把行号指针重解到 `docker.py:305-307`（stop）与 `:60-68`（`_name`）。
- 邻面（我自己跑）：新模块＋`test_warmpool_claim`／`test_warmpool`／`test_streaming_lifecycle`／
  `test_durable_ops`／`test_worker`／`test_provision_rollback`／`test_stop_release_admission`
  合跑 98 支 rc=0；`ruff check app tests` All checks passed；`mypy app` Success（41 files）。
- 未证实：真 docker/k8s 守护进程下的两极没跑（本轮禁容器；常驻用例用的是自述 runtime 事实的替身，
  mock 只会说 UNKNOWN —— 属机制限制）；`recover_stuck_gpu_allocations` 与本次 commit 在同一事务里
  的真实交错窗口没有常驻并发用例（那一半仍在 `tests/test_postgres_concurrency.py` 的档位里）；
  `error_message` 拼接后的长度不截断是按 `app/models.py` 的 `Text` 列判定的，非实测。
  本轮**没有**新开登记项（并发窗口属既有 PG 档，不重复立项）。
- 计数面 711→723，门禁 G0.78。

### 本轮新增的待收口项
- ~~`N-64`：`accumulated_seconds` 的累加在账本的幂等保护之外（扣一次、展示与配额算两次）~~ —— **已由 N-74 闭合**：这一列改由账本投影（`app/services/ledger.py:97-109` 新读数口径、`app/services/orchestrator.py:405-439` 结算后 SET 而非 `+=`，返回值同步改成账本认下的秒数）。改前两臂复算都是 `FFF.F.F.`（8 支里 5 开火），一手读数 `列=60、账本=30`。判据 `tests/test_settled_projection.py` 8 支。量出来的两格残留另登记 N-75（指标计数器重放加两次）／N-76（destroy 失败窗口 live 重复计）。
- ~~`N-65`：**`available_credits` 把个人与组织余额直接相加，而行同时带两个归属**（充值翻倍／跨成员拿钱）**—— 已由 N-71 闭合**：读侧分池（组织池只数 `user_id IS NULL` 的行）＋抽一份 `gross_credits` 把三遍相加合一，判据 `tests/test_credit_purse_split.py` 8 支；实测读数从 `available(a1)=2000 / available(b1)=1000` 变成 `1000 / 0`。写侧单一归属（CHECK 或 `account_id` 列）另轮处理，理由是本仓账本 append-only 不回填。
- ~~`N-66`：hold 幂等键按 workspace 全局唯一，二次启动永远拿不到 pending hold~~ —— **已由 N-73 闭合**：键改按轮次发（`billing.py:272-291`），兜底只按 `workspace_id + status=PENDING` 收敛；实测从 `('hold:ws-A','captured',300)`＋pending 0 变成二启拿到新 pending 且 available 减 300。判据 `tests/test_hold_round_key.py` 7 支。

- `N-67`：**provision 失败那一路是“释放先于确认”的第二实例**（N-63 只修了 stop/reconcile）。
  `_execute_provision` 在 :249 用 `contextlib.suppress(Exception)` 吞掉补偿
  `provider.destroy(workspace)` 的异常并继续 raise，随后 `_fail` 在 :304 无条件
  `scheduler.release` 并在 :315-317 清掉 GPU 字段 ⇒ 容器若没被销毁成功，卡照样回池。
  与 N-63 同一条判据可复用（`_release_admitted` 的 `command_succeeded` 参数就是为此留出），
  但要先量清“destroy 失败该由谁认账、`_fail` 的三个调用点各自的 runtime 形状”，
  不当顺手改。注意它会把 `tests/test_stop_release_admission.py` 里 `admitted_uses == 2`
  那一格顶到 3——接同一条判据时必须同步改判那条常驻断言，不能让它悄悄变宽。
- ~~`N-68`：reconcile 的节点不一致分支把卡放了，却没停那个 pod（同类第三实例）~~ —— **已由 N-77 闭合**：该分支改走 `_stop_cleanup`（`app/services/orchestrator.py:556-575`），认账了才置 FAILED；provider 仍自述 ALIVE 时保持 RUNNING 不写终态、卡不回池，下一轮接着停。判据 `tests/test_k8s_node_truth.py` 的夹具改成有状态（`stop` 计数＋`reconcile` 随停没停改口），诚实档读 `stop_calls == 1`／撒谎档读 `failed == 0`＋`Gpu` 仍 ALLOCATED＋第二趟 `stop_calls == 2`；接线棘轮 `reconcile_consumers` 1→2 并把反向对照改成 2/1/0 三档。改前就地换面复算 `...FF.........FF...`（19 支里 4 开火），一手读数 `节点不一致没有去停那个 pod（stop_calls=0）`。
- ~~`N-69`：warm pool 认领失败那一路把销毁失败只记日志，然后照样放卡并写成终态（第四实例，且三重不可见）~~ —— **已由 N-80 闭合**：撤销分支的放卡改由 `orchestrator._release_admitted` 认账（报错档只认 MISSING、成功档不再是 ALIVE；与 `_stop_cleanup` 的 `:366`/`:370` 同形），不认账时不放卡、不清 `container_name`、不写终态，`_cleanup_failed` 选择集扩成 `in_([FAILED, DRAINING])`（`app/services/warmpool.py:236-268`）使 DRAINING 那一格有了重试驱动者，`billing.release_hold` 保持无条件（计费轴与资源轴分家）。判据 `tests/test_warmpool_claim_admission.py`（11 def／收集 12 例）＋重判的 `test_warmpool_claim.py::test_warm_pool_rotation_failure_only_when_admitted`；我主线换面复算 `FF...FFF.FFF`（8 开火），一手读数「destroy 抛错、provider 说的是 alive，卡却被放回池子里了」（`alloc/gpu_free` 三键全反）。原任务书里「清列等于永远停不掉」的机制已被 N-78 降级，两处文字按现状改写。
- ~~`N-70`：`DockerProvider.reconcile` 用 DB 列推断「容器不存在」，而同一个 provider 的 destroy 会按命名约定把名字推出来 ⇒ 假缺席~~ —— **已由 N-78 闭合**：容器名推导收成一处 `DockerProvider._name()`（start/stop/destroy/inspect/logs/wait_ready/exec/pull_artifact 同源），`inspect` 失败按 stderr 分 absent／unknown（本机实测两形状同为 rc=1：`error: no such object` vs `Cannot connect to the Docker daemon at …`），`reconcile` 把 unknown 读成 UNKNOWN ⇒ `_release_admitted(command_succeeded=False)` 在 daemon 不可达时拒绝放卡；`KubernetesProvider.start/stop` 的空列静默空转同批改掉。判据 `tests/test_provider_absence_evidence.py` 8 支（含真引擎档 `tests/test_docker_provider_integration.py::test_reconcile_asks_the_engine_when_the_name_column_is_empty`）；改前单元档 `F.FFFF..`（5 开火）、真引擎档红在「空列被当成缺席：引擎说这个容器在跑」。
- `N-72`：**可用额为 0 时仍允许开机**（出厂默认档）。`billing.check_launch_eligible` 只挡  `available < 0`（`app/services/billing.py:~80`），而 `app/config.py:56` 出厂默认  `billing_enforce_preauthorization=False` ⇒ 零余额成员可以启动，钱在第一次结算时变成负数、  靠配额 monitor 兜。这是应然问题（要不要把 0 也挡掉／出厂是否该开预授权），不是实现 bug，  N-71 的判据已把两档现状钉住：预授权开启时 0 余额必须被拒。
- `N-75`：**`gpu_seconds_total` 在 stop 重放里加两次**（N-74 量出来的一半）。`_finalize_stop`
  每次进入都 `record_gpu_seconds(booked)`，而重放时 `booked` 仍是那一条 USAGE 行的秒数（账本幂等
  ⇒ 同一个数再加一次）⇒ 计数器 60、账本 30。实测 `start=0.0 after_first=+30.0 after_retry=+30.0`。
  两条修法都要先定口径：要么让 `settle_workspace_run` 告诉调用方"这次是命中已有行还是新入账"，
  要么把指标从 counter 改成账本 SUM 的投影。本轮没自行改设计（属主可决）。
- `N-76`：**`destroy` 的 provider 失败窗口让配额门禁把同一段算两次**（N-74 的另一半）。
  `_settle_running_segment`（`app/services/orchestrator.py:458-462`）在 status==RUNNING 时结算且
  **不清** `started_at`，而 `destroy` 是"先结算、后 `provider.destroy`（抛错故意上抛让 DESTROY 重试）"
  ⇒ 库里留下一行"RUNNING＋已结算＋started_at 未清"，`course_usage_seconds` 的 live 项
  （`app/services/billing.py:396` 只对 RUNNING 相加）把同一段再算一遍。实测
  `status=running accumulated=30 ledger_sum=30 配额读到 60`。release 失败那一档不在此列（状态已是
  STOPPING，live 分支不进）。修法候选：把"已结算到的时刻"随结算一起推进、live 从那儿起算；或让
  live 读"账本 SUM 之外的差额"。都涉及口径，需与 N-64 的读者面一起定。

- `N-61`：~~要不要把构建后端从 setuptools 换成 hatchling`**【N-62 结案：不换】** 本机在 `git worktree` 副本上真跑过：hatchling 1.32.4 两建 wheel 同为 `629d6ff7e24f`（它自己就钉 tar 成员 mtime/uid/gid 与 gzip mtime，读安装到本机 venv 的源文件核对过）；与 setuptools 的 wheel 差异只有三处——成员 55 对 56（少 `dist-info/top_level.txt`，全仓 grep 零读者）、`Requires-Dist` 只差 PEP 508 的引号风格（22 条语义同集）、`WHEEL` 的 Generator 行。净收益只是删掉 `scripts/sdist_normalize.py`（约 100 行，6 支判据与两处消费位都已落门禁），代价是 `uv.lock` 重解析、`dev` extra 对齐、wheel 侧 `recomputable` 基线重钉与所有引用产物 sha 的文档面重扫⇒ 不抵。再议的触发条件：自研归一哪天失效，或后端侧出现**别的**产品收益。
~~`N-34`：sdist 的 sha 随打包时刻变（setuptools 84 不把 sdist 的目录条目与 `PKG-INFO` 的 mtime 夹到
  `SOURCE_DATE_EPOCH`，逐字节定位见上一节）。wheel 已可复算；sdist 那一半要么给 `dist/checksums.txt`
  加"这是构建记录、不是复算承诺"的口径说明，要么换 `uv build`/后处理再验一次。（**已由 N-60 走"后处理"这一支闭合**：`scripts/sdist_normalize.py`，实测两建同 sha；口径改为"那行 sha 是归一后产物的 sha"）~~
- `N-32`：CI 改按锁装之后，`docs/VALIDATION.json` 才第一次"可能"在 runner 与本机之间逐字节相等；
  这条主张**未在真 runner 上验证过**（不能推送），本机侧只用"同树两次跑 + 换环境"两档做了替代实验。
- ~~`N-33`：`tests/test_gpu_pool_guard.py` 那两支带哨兵（缺余量时算合法跳过）~~ —— 已由 **N-41 闭合**：
  夹具显式达成前置并断言，哨兵与闭集条目一并删除，条件跳过归零。

- `N-79`：**「存在但没在跑」被判成缺席**（N-78 没动的那根轴）。`DockerProvider.reconcile`
  在 inspect 拿到非 running 的 state 时落到 `RuntimeState.MISSING` —— 这涵盖了
  `created`/`restarting`/`paused`/`dead` 这些「容器还在、只是暂时没进程」的态；`KubernetesProvider.reconcile`
  （`providers/k8s.py:535-549`）同样把 `available_replicas` 读不到 ≥1 一律判 MISSING，含 pod 还在
  Pending／节点不可调度的那一段时间。释放准入认 MISSING 就放卡 ⇒ 一个正在 restart 的容器或一个
  迟早就绪的 pod 会与新人共用同一张卡。要不要把这些态改成 UNKNOWN／ALIVE 是口径决定（改严会让
  永久卡住的 pod 把 GPU 钉死，需要配超时），本轮只登记不改：N-78 已经改了「问不到」那一档的判法，
  这一档必须连超时策略一起定。

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
- **改的三件事**：两端加 admin 检查（沿用仓内既有惯例 `gpus.py:13`，不另造一套依赖）；
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

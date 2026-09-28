# OPERATIONS — EmbodiedCloud

> 版本：0.7.0（2026-09-26）。环境拓扑、监控、容量与运维约定。

## 1. 部署拓扑

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│ Control Plane (1+ replicas) │        │ GPU Host（单机模式）          │
│  FastAPI + SQLAlchemy       │───────▶│  Docker + NVIDIA runtime      │
│  /metrics, /api/*           │        │  每 Workspace 独占 GPU        │
└──────────┬──────────────────┘        └──────────────────────────────┘
           │                          ┌──────────────────────────────┐
           │                          │ K8s Cluster（生产多机）        │
           ├─────────────────────────▶│  NVIDIA device plugin         │
           │                          │  PVC + fsGroup                │
           ▼                          │  Ingress / TLS                │
  PostgreSQL（生产）                  └──────────────────────────────┘
  Redis（未来 streaming/queue）
```

## 2. 监控

- Prometheus 采集 `/metrics`（详见 Observability 章节）。
- 关键指标告警建议（未来接入 Alertmanager）：

| 指标 | 告警建议 |
|---|---|
| workspace_launch_failed_total 增量 | >0 持续 10min（**只含用户按下的启动**：池内补位的失败记在 `warm_pool_prewarm_failed_total`，见 N-39） |
| warm_pool_prewarm_failed_total 增量 | 持续 >0 说明池在反复开"注定失败"的格（容量不够或 `warm_pool_size` 与舰队不匹配），不是用户故障 |
| workspace_running / gpu_allocated 失衡 | allocated > running + 5 |
| gpu UNHEALTHY 数量 | >0 |
| API 5xx 率 | >1% 持续 10min |

## 3. 容量模型

- 单 GPU = 1 Workspace（第一阶段）。
- GPU 秒计费 = RUNNING 起止差（stop/delete 结算）。
- Warm Pool 目标：P50 < 15s，P95 < 30s（需真实硬件实测调优）。

## 4. 数据生命周期

- 数据库：Alembic 迁移，SQLite 本地 / PostgreSQL 生产（**生产仅支持 PostgreSQL**：
  SQLite 方言丢弃 `FOR UPDATE`，行锁类并发保证拿不到；见 ADR 0005）。
- 预授权（credit_holds）：**出厂默认开启**（`billing_enforce_preauthorization=True`，N-83）。余额必须 ≥ `billing_minimum_launch_minutes × 60` 才能启动，并真的圈住这笔额度；演示/离线部署可显式设 `false` 回到旧行为。注册时向个人池发 `billing_signup_credits`（默认 300 ＝ 一次最低启动窗口）—— 生产没有别的充值入口，不发这笔就等于「谁开机都是白嫖」。运行中的透支仍由配额 monitor 兜（一个扫描间隔的宽限）。
- 预授权超时回收：`billing_hold_ttl_minutes`（默认 60）界定崩溃残留的泄漏窗口，
  worker 每 60s 扫一次超时 pending 并退回；RUNNING 的 workspace 不回收，等结算转正。
- 迁移新增列/约束忘了写：`tests/test_migrations.py` 的模型↔迁移对账（`compare_metadata`）
  会在默认档直接红，不需要等生产库暴露。
- Workspace 数据：`/workspace/project|datasets|outputs|checkpoints` 持久化，重建不丢。
- checkpoint → artifact（checksum）→ deployment record → edge 下载验证。

## 5. 运维例行

- 每周：alembic current、备份验证、审计 ledger 抽样对账。
- 每次发布：G0 全部 Gate + 迁移 up/down + smoke（见 ACCEPTANCE_GATES.md）。
- 每季度：威胁模型复核、依赖扫描、镜像重扫。
- **`POST /deployments/{id}/verify` 返回 503 时不要动数据**：那表示对象存储不可用
  （鉴权失败 / 桶不存在 / 网络故障），部署记录仍停在 `downloading`，恢复后直接重试
  即可。只有 200 + `status=failed` 才是"关于产物的判决"（对象不存在或 checksum 不符），
  而那是终态、不会自动重验（ADR 0008）。

## 6. 环境变量（.env）

见 `.env.example`；新增配置必须在 `app/config.py` 有默认值且文档化 —— 该约定由
`tests/test_config_docs.py` 双向核（字段未进文档、或文档留着 Settings 已删的键，都红）。

两条运维含义容易踩：

- **对象存储**：`EMBODIEDCLOUD_ARTIFACT_BACKEND=s3` 的 SDK 必须在**构建时**装进镜像：
  给镜像的 `uv sync --frozen` 那一步加 `--extra s3`（见 `runtime/Dockerfile.control-plane`
  与 SUPPLY_CHAIN §4）。事后在运行中的容器里 `pip install "…"` 已经没用——`python` 解析到
  `/app/.venv/bin/python`，而 `pip` 仍是基础镜像的 `/usr/local/bin/pip`，那份 venv 又不暴露
  基础镜像的 site-packages，装进去的东西应用 import 不到。
  凭据或 endpoint 不全时**启动即失败**
  （`BLOCKED_EXTERNAL_DEPENDENCY`），不会退回本地目录 —— 退回会让产物落在控制面
  文件系统，而 `Artifact.store_name` 仍记 `s3`，校验时找不到对象。
- **`EMBODIEDCLOUD_K8S_KUBECONFIG`**：留空时走 SDK 默认解析，但 kubernetes Python SDK
  在**模块 import 时**就把 `KUBECONFIG` 固化为常量（实测：进程启动后再 export 该变量，
  加载直接报 `Invalid kube-config file`）。要在进程生命周期内切换集群/挂载专用
  kubeconfig，请用本项而不是改环境变量。

## 7. 验证档位与前置条件

> 记账脚本的落盘纪律（N-87 踩过）：往 Markdown 表格插行时，**每支新行必须自带行尾换行**。
> 把「带换行的上一行」换成「带换行的上一行 + 新行」而新行不带换行，会让新行与它下面那行
> 粘成一行；`## ` 标题一旦被吞进行尾，表的后续行就跑到下一节去了。这类粘连骗得过所有
> 「按行首前缀取数」的门禁与自检读数（`docs_row_order`／`docs_state_rows` 只会少看一支，
> 不会判红），所以写后必须用 `tests/test_docs_table_rows.py` 里那两把尺复算：`row_seams`
> （行内第二个行起点或被吞的标题）与 `unterminated_rows`（`| G` 开头却不以 `|` 收尾）。
> 结构性重排还要证内容守恒：去掉全部换行后逐字节相等，或逐行多重集不变。

`make test` 一次跑全部档位；单档可点名单跑。缺前置条件的档位是**整档 skip +
在 `dist/VALIDATION_RUN.json` 记 PENDING(原因)**（提交面 `docs/VALIDATION.json` 按设计
不含档位读数，见 §7.1），CI 对需要 docker 的档位要求 PASS。

### 7.1 两份报告：可复现面 vs 本次跑读数

| 文件 | 进版本库 | 内容 | 谁读它 |
|---|---|---|---|
| `docs/VALIDATION.json` / `.md` | 是 | 只含**换机器重跑逐字节相同**的门禁：collected / failed / lint / typecheck / migration / build / 文档面一致性 | CI `git diff --exit-code`（新鲜度）、CURRENT_STATE 的 `Test` 行对账 |
| `dist/VALIDATION_RUN.json` / `.md` | 否（gitignored） | 本次跑读数：各集成档状态与 note、passed/skipped、按用例名列出的 skip、skip 闭集判决 | CI 的 "Docker-backed tiers really ran"、`scripts/release.sh` 的档位表、人工排查 |

**skip 闭集**（为什么"跳了几支"不再需要改文档）：一支用例可以按条件跳，但必须满足
"所属模块在闭集里 + skip 文案含该模块的哨兵"。闭集由 `conditional_skip_universe()`
从用例源码现取（`_const_str` 走 AST，不手抄）：六个集成档模块。闭集的第二来源 `EXTRA_SKIP_UNIVERSE` 现在是空的 —— 原先唯一的住户 `test_gpu_pool_guard` 已在 N-41 改成「夹具显式达成前置 + 断言」，不再有合法的条件跳过。
名单外的 skip → `unexpected_skips=FAIL` → 挡住发布。这样"通道今天通不通"只在读数里出现，
而"某支新用例开始静默跳过"照样当场翻红。

**这条分档是量出来的**：同一份树跑两次 `make validate`，第二次只把守护进程通道打断
（`DOCKER_HOST=tcp://127.0.0.1:1`）。提交面两份文件的 sha256 **逐字节相同**，而本次跑读数从
`passed=566 / skipped=2` 变成 `passed=498 / skipped=70`（依赖守护进程的四个档整档干净跳过），
两次的闭集判决都是 PASS。分档之前，同样这两跑会改动提交面 6 个以上字段（`test_run.passed/skipped`
＋四个 `integration_*` 的状态与 note），也就是 CI 那条 `git diff --exit-code` 会随机红。
遮罩自身也带判据：`report_split` 要求声明的每个环境字段真在报告里、且遮完至少剩 4 项门禁。

### 7.2 `make verify-artifacts`：checksum 里哪一行可复算，由探针说了算

`dist/checksums.txt` 记 wheel / sdist / sbom 的 SHA-256。第三方拿到那行 sha 能不能自己重建出同样的字节？
`make verify-artifacts` 每类产物各建两次（每次一份新目录，避免读到上一次的残留）比 sha，全等才写
`recomputable=yes` 进 `dist/checksums.manifest`；发布链里它排在 checksums **之前**，主张与实测不一致
（说反了、清单缺项、有项没测）退出码非 0。2026-09-27 实测：`[artifacts] sdist=yes wheel=yes`
（`SOURCE_DATE_EPOCH=1790533457`，两建 sdist 同为 `35e21305d9…`、wheel 同为 `374ea4b59f…`），
`dist/checksums.manifest` 里 `embodiedcloud-0.7.0.tar.gz` 那行已从 `recomputable=no` 写成 `yes`。
把 sdist 钉住的不是上游：本机装的 setuptools 是 84.0.0，PyPI 上 `setuptools` 的最新也是 84.0.0
（2026-08-08 上传），"等一个把 sdist 时间夹住的版本"今天不存在。是一条自己加的工序——`make build`
末尾用 `scripts/sdist_normalize.py`（只用标准库 `tarfile` + `gzip`）把 tar 的**头部**归一：成员按名字排序、
mtime 钉到 `SOURCE_DATE_EPOCH`、mode &= 0o755、uid/gid 归零、uname/gname 清空、不落 pax 记录
（格式钉成 GNU_FORMAT），gzip 写 `filename=""` 且 mtime 同样钉到 epoch；**内容一个字节都不动**。
探针在自己的构建步里调同一份实现，所以它量到的是发出去的那个形状。归一之前，同一棵树立两次是漂的
（`6b6d115a25…` 对 `ad66f49101…`，epoch 相同；那两次的 wheel 都是 `374ea4b59f…`）——这句是这道工序的来处。

口径要说窄：sdist 那行 sha 是**归一后**产物的 sha。第三方复算走的是同一条 `make build`（含这一步）
加同一个 `SOURCE_DATE_EPOCH`，**而且是同一个 Python/zlib**：gzip 那一层的字节随 zlib 的压缩参数与版本变，
tar 的 GNU 头部布局随标准库实现变——换解释器或小版本号都可能改 sha 而没有任何东西坏掉（这一条是本轮
查 `setuptools-reproducible` 时顺带挖出来的：那包 0.1 版、MIT、2024-05-15 唯一一次发布，只 patch
`tarfile` 的 TarInfo、从不给 `GzipFile` 传 mtime，标准库在没传时取 `time.time()`（本机亲读 `gzip.py`），
于是它的"确定性"要求两次构建落在同一秒内——读码即知，未实测）。这一步公开、确定性、只依赖标准库，
所以"可复算"仍然是一句可被推翻的话，而不是"setuptools 自己的输出稳定"。判据常驻在 `tests/test_artifact_reproducibility.py`：两支不同的构建
归一到同一个形状、成员清单与每个成员的内容 sha 逐个不变（不许靠改内容换确定性）、gzip 头部两头各一臂
（我们的输出里 FNAME 位必须为 0；用有名字的文件句柄且不传 `filename` 时这一位必须能复现出来）、
`SOURCE_DATE_EPOCH` 取不到就失败而不许退回打包那一刻、以及这一步只有两个消费方（`Makefile` 与探针）。

**探针打印回 `sdist=no` 时怎么办**：`no` 本身不红——清单由探针现写、写完再读回来对账，实测 `no` 与清单
`no` 一致就照退 0（手改那一行既不成立也没用），所以这一行得由人在收尾时读，别等门禁敲门。按顺序翻：
① `make build` 里那一行归一还在不在、`scripts/sdist_normalize.py` 会不会因为 `SOURCE_DATE_EPOCH` 取不到而
非零退出（按 make 的语义那一行会把 `make build` 停住——这条是语义推得，本轮没实测）；② 漂的是头部还是
内容：多出一个没被钉住的头部字段就把它补进这一步（判据那句"归一后仍不同：还有没被钉住的头部字段"就是
为这一格写的），成员内容自己漂了则不是这道工序的事，那是构建不确定性的新缺陷，要另立案；③ 在实测翻回
`yes` 之前，`dist/checksums.txt` 里那一行只是本次构建的记录，对外别当可复算主张发出去。

一处诚实的坑：`GzipFile` 不传 `filename` 时会从 `fileobj.name` 推断 gzip 的 FNAME 字段（单变量实测：
有名字的文件句柄不传参 ⇒ FLG=0x08；传 `filename=""` ⇒ FLG=0x00），本机第一版就栽在这里——137 个成员
逐字节相同、两份 `.tar.gz` 的 sha 仍不同，差的只有那一个字段。但本实现写进 `BytesIO`（没有 `.name`），
变异台上"删掉 `filename=""`"那一臂**没开火**（四臂开了三支：不排序、改内容、不钉 mtime），所以它是
钉给将来改写方式的保证，不是今天承重的墙。

历史读数留着当证据、不当现状：上一轮（`SOURCE_DATE_EPOCH=1790482819`）记的是 `wheel=yes`
（两建同为 `4259babd17…`）、`sdist=no`（`41dda386f2…` vs `174db2b484…`）；跨环境那次（HEAD `c9de532`，
干净 worktree + 独立锁造 venv）里跑 `make verify-artifacts` 退 0，两处构建出的 wheel 逐字节相同
（`40f27dde80…`），sdist 不同（`7f27c86553…` vs `68f5170dff…`）——当时 `yes`/`no` 两行各自被独立环境
证实了一次。今天的 `yes` 不覆盖那两笔，那两笔也不否定今天这行；差异点正好是上面那道归一。

| 档位 | 命令 | 前置（本机实测） |
|---|---|---|
| PostgreSQL 真并发 | `make test-pg` | docker daemon + `postgres:16-alpine` 缓存 + `.[postgres]` |
| Docker provider 真容器 | `make test-docker` | docker daemon + 与 daemon 同架构的已缓存测试镜像 |
| 浏览器真 DOM | `make test-browser` | Playwright + 系统 Chrome/Chromium |
| 对象存储真后端 | `make test-s3` | docker daemon + `versity/versitygw:v1.8.0` 缓存 + `.[s3]` |
| K8s 控制面真集群 | `make test-k8s-control-plane` | docker daemon + `kindest/node:v1.37.0` 缓存 + kind 二进制（`EMBODIEDCLOUD_KIND_BIN` 或 PATH）；**不需要 GPU** |
| K8s GPU 全流程 | `EMBODIEDCLOUD_K8S_TEST=1 pytest -m k8s_integration` | 真实集群 + NVIDIA Device Plugin（物理待验） |

测试自建的外部资源一律自持有、用完即删：PG/S3 容器带 `--rm` 与 atexit 兜底，
kind 集群写进临时 KUBECONFIG（不合并 `~/.kube/config`）并在退出时 `kind delete cluster`。
档位内部**只起一个 kind 集群**：本机曾出现两个集群并跑时 API server 连接被掐
（`SSLEOFError`，一次观测，未做对照复现），删掉多余集群后同一条用例链稳定通过。

两条读法上的坑（本轮踩过）：

- **PENDING ≠ 跑过**。kind 二进制不在 PATH 且没设 `EMBODIEDCLOUD_KIND_BIN` 时，
  G0.26 整档 skip，报告写 `integration_k8s_control_plane: PENDING(缺 kind 二进制)`，
  其余档位照跑、总用例数照常报——只看 `overall` 会漏掉这一档根本没执行。
  这条坑本轮**又踩了一次**（写这条的同一轮）：`make validate` 与它前面的 `export` 若不在同一条
  命令行里，那一档就降为 PENDING。本轮那次的实际读法是：报告里
  `integration_k8s_control_plane: PENDING(缺 kind 二进制)`，而 `overall` 之所以还是 FAIL，
  是**计数对账先响**（skipped 由 1 变 8，与文档里那一串四元组不符）——两道防线都在，
  看不见档位的人由计数那条兜住；反过来也提醒一句：文档计数串一旦写死，档位漏跑就会以
  "计数不符"的形式暴露，别把它当成计数器的错。
- **共享库 + 固定大小的 mock GPU 池**：全套用例共用一个 SQLite 库，mock 只 seed 8 张卡，
  而 `POST /api/workspaces` 会占住一张、用例结束不还。新加"批量建 workspace"的文件
  能把后面的 GPU 用例饿死（报错是 `No GPU available with >= 8 GB VRAM`，不是断言失败）。
  需要空闲卡的用例请显式达成前置条件：`tests/gpu_pool.py:ensure_free_gpus`。
  **净额口径（N-85）**：光数 AVAILABLE 行会撒谎——队列里那些「已入队、还没被执行」的 provision op 每一个
  都会吃掉一张卡，所以 `ensure_free_gpus` 判的是 `空闲数 − 未执行 provision op 数 ≥ need`，失败消息把两个数
  都打出来。返回值仍是原始 AVAILABLE 数（调用点语义不变）。`tests/workspace_progress.py` 那种自己 tick 队列的写法，需要卡的用例请按「本文件会排掉几支 op」来算 need。
  **还有一条引信**：app 每次启动跑 `reconcile_all()`，其中"QUEUED/CREATED 且无 active operation
  ⇒ 重新入队 PROVISION"——于是任何模块留下的 CREATED 行，都会由**下一个**起 TestClient 的模块
  的 worker 去执行、去抢卡。留下行的模块必须自己收尾（删行 + 还卡），否则它看起来"已经绿过"
  却把红留给后面不相干的用例。
- **第三句要分清的话：`failed` 不一定是死了**。共享卡池被借走的那 1s 里 provision 的第 1 次尝试
  会报 `No GPU available`，而 worker 还打算再试两次——旧实现在那一刻就把 workspace 写成 FAILED，
  几秒后重试成功又把它改回 RUNNING（读者看到的是一次假死，而且没人替它翻案：
  `workspace_operations` 在 API 层零读者）。现在只有 `OperationWorker.will_retry(op)` 为假
  （最后一次尝试）才写 FAILED，非终态停在 QUEUED 且保留 `error_message`。
  运维读法：`status=queued` + 有 `error_message` ＝ 正在重试，看 `last_error` 与 attempts；
  `status=failed` ＝ 三次尝试都用尽，这才是终态。
- **两句"分配失败"不是一回事**：`GpuPoolContendedError` 说的是"N 张满足容量的空闲卡
  正被并发事务锁住，0.5s 预算内没等到"——该重试、该看谁在抢；
  `No GPU available with >= X GB VRAM` 才是容量结论——该扩卡或下调模板需求。
  旧实现两者共用后一句，本轮在真 PG 行锁上把它拆开（读法见 ARCHITECTURE §3）。
- **前提要等，判据不许等**。同一轮里两条红都是这个形状（一次真集群档、一次 docker 档），
  且都在"已经通过判据之后"的那一行：判据是"未就绪的容器/Pod 不该被读成就绪"，
  而"容器已退出""Pod 已被控制器建出来"是**别人异步做的事**，宿主忙时就晚到。
  等前提是允许的（`tests/k8s_server.py` 的 `await_pod`/`await_condition_reason`、
  docker 档的 `wait_exited`），但等不到必须报**"前提未达成 + 最后一次读数"**；
  判据自己的超时预算（`wait_ready` 的 6s、`wait_running` 的 30s）是被测主张的一部分，
  红了也不能调大——那等于把主张改小。两类都各有开火对照。
  同一族的第三处是 4 份 `for _ in range(40): sleep(0.05)` 的 workspace 轮询：**2s 不是判据预算，
  是 worker 的重试节奏**（backoff 1s + 2s ⇒ 第 3 次尝试最早落在 3s 之后），现已统一到
  `tests/settle.py:await_workspace_settled`（终态才返回，等不到就报前提未达成 + 池内空闲卡数）。
- **容器侧的两条宿主前提，本轮各咬过一次，读法写在前面**。①这台机器的 `/tmp` **不是共享进 colima 虚拟机的挂载点**：`-v /tmp/x:/out` 之后容器内写 `/out/f` 成功、宿主 `/tmp/x` 依然是空的，而把挂载点换成工程目录下的子目录同一分钟就可见——所以「产物没落盘」这种读数先怀疑夹具，别急着记在被测程序头上（`-v /tmp/probe.py:/probe.py` 会以 "can't find `__main__` module" 的形式露出同一只空挂载）。要往容器里送脚本就用 stdin：`docker run -i … python - < script.py`。②守护进程所在的 VM 会**整台停掉**：本轮 17:14 还能 pull，17:21 就报 `dial unix ~/.colima/default/docker.sock: no such file or directory`，`colima list` 里 default 变 Stopped；`colima start default`（约 20s）恢复，镜像缓存不丢。docker 档整档突然干净跳过时先读这条，别读成「这档今天没需求」。 **今晚同一前提又咬了一次**：22:07 的一次复算里 postgres／docker／k8s 三档同时 PENDING、S3 档退成 5/20，
  而浏览器档照绿（它不依赖守护进程）；20 分钟后同一棵树上的 `make validate` 跑出 542/1/0。判"环境的红"有两个一手
  tell：`colima status` 与 `~/.colima/default/docker.sock` 的 **mtime**（套接字是 VM 起来那一刻重建的，mtime 落在
  红的那段时间里就说明当时 VM 不在），以及"同一轮里不依赖 docker 的档照绿"这个形状——真的代码坏不会只挑
  依赖 docker 的那几档红。
- **`kind` 不在 PATH 上，而在仓库同级的 `.toolcache/` 里**：`tests/k8s_server.py:kind_binary()` 现在按「显式覆盖 → PATH → 固定候选位」三档找。此前整档能跑靠的是某个 shell 导出过 `EMBODIEDCLOUD_KIND_BIN`，换个 shell 就静默跳 7 例——**跳过长得像「今天没跑到」，不像「前提没了」**，所以这一格修在发现逻辑里，而不是写进文档要求人工导出（2026-09-26 实测 `/Volumes/Extra/CodeProj/.toolcache/kind` = kind version 0.33.0，正是 ADR 0009 钉的版本；补上发现档后真集群 7/7 恢复，用时 2:08）。
- **漏洞库不是镜像：它要每天新鲜，所以"取不到库"是通道问题、"钉住库"是判断问题**。`make image-cve` 的默认库通道（trivy 0.74.0 的 `--help` 原文：`mirror.gcr.io/aquasec/trivy-db:2` → `ghcr.io/aquasecurity/trivy-db:2`）本机实测分别 `connect: connection refused`（重试 77s 后 FATAL）与拨号 i/o timeout；改用 `--db-repository public.ecr.aws/aquasecurity/trivy-db:2` 跑通。两条读法的区别要留住：热缓存后整步 2.5s、首次含库下载约 15 分钟（缓存落 `.trivy-cache/`，已 gitignore）；**别**为了让 CI 快就把库钉死或 `--skip-db-update` 常驻——那会把扫描变成一份过期结论。
- **外网通道会分道失效：构建／取工具这类步骤先判通道，再谈代码**。本轮同一晚三种形状各自独立——
  守护进程经它配置里的镜像站取 docker.io 时 TLS 握手超时（上一轮它是这台机器**唯一通**的那条），
  `ghcr.io` 拨号 i/o timeout，容器内对 `files.pythonhosted.org` 的 TLS 握手超时。
  把"控制面镜像建不出来"定成通道问题而不是配方问题，靠的是同一时刻的并排对照：
  容器内取 `pypi.org/simple/setuptools/` 是 200／535 KB／1.3s，宿主 `curl` 同一条 CDN URL 拿得到 302，
  而同一份配方上一轮 160s 构建成功过（读数分别见 SUPPLY_CHAIN §2 与 §8 第 3 项）。
  **不要**用加大构建超时、换基础镜像或"先出一个空产物"去洗它——那等于把外网波动记成代码变更，
  还会让下一个读报告的人以为配方动过。`make image-sbom` 在这种情况下的正确行为是退 2 并说清缺哪件前提。
- **想在本机做跨架构构建复算之前先读这条（本轮量出来的）**：`docker build --platform linux/amd64 -f
  runtime/Dockerfile.control-plane` 在这台机器上**不成立**，而且失败方式很误导。一手读数：日志走的是
  legacy 格式（`Step 9/16`），中途三次打印「The requested image's platform (linux/amd64) does not match
  the detected host platform (linux/arm64/v8) **and no specific platform was requested**」，最后报
  `image with reference sha256:2d26323feb05… was found but does not provide the specified platform
  (linux/amd64)`；把那个中间镜像 `docker image inspect` 出来是 **arm64**——也就是说 builder 阶段其实
  按宿主架构跑完了，`--platform` 没被传进中间容器。本机 `docker buildx` 也不存在（`docker: unknown
  command: docker buildx`）。**为什么没去查上游文档定版**：今晚 `docs.docker.com` 两个页面（build/buildkit
  与 reference/cli/docker/build）都 fetch failed，取不到原文，所以这里只写本机观测到的行为，不写成版本结论。
  可行的替代做法（本轮用的就是这个）：绕开构建器，用 `docker run --platform linux/amd64 <同一个基础镜像
  digest>` 起一个模拟的 amd64 容器，把 `pyproject.toml`/`uv.lock`/uv 二进制送进去，跑与 Dockerfile 里
  **同一行** `uv sync --frozen …`，再看装出来的轮子标签与能否 import——这验证的是"锁在生产架构上选得到
  那一组字节"这件事本身，而不是本机的构建器 plumbing。
  **这一条在配方改造后仍然成立**（只是形状变了）：依赖层改由 `uv sync --frozen` 从 `uv.lock` 装之后，构建不再需要
  现解析、也不用装构建隔离的 setuptools，但它**照样要从 `files.pythonhosted.org` 把每个 wheel 的字节拉一遍**，
  所以同一条通道坏了照样红构建。区别在于现在失败的含义变了：旧配方失败可能是"解析到了锁外的版本"，
  现在失败只会是"字节取不到"——版本集合由 `make image-sbom` 末尾那条 `--lock` 对账判据常驻钉住

- **真集群档在"引导阶段"整档红，先看宿主负载再看代码**。本轮实测到一次
  `kind create cluster` 失败在 `Preparing nodes ✗`，原因行是
  `could not find a log line that matches "Reached target .*Multi-User System.*|detected cgroup v1"`
  ——**7 例全部 failed on setup**，一条断言都没跑到。当时同一台机器上另有一个会话在跑它自己的
  pytest + Chrome，`vm.loadavg` 的 1 分钟值 43.8；等负载衰减到 9 以下复跑同一棵树，7/7 恢复绿，
  代码零改动。区分办法就写在报告里：这类红的形状是 `failed_names` 全落在同一个文件的 setup、
  且明细是引导日志缺行，而不是某个断言不成立（`test_run.failed_names` 正是为这个区分而加的）。
  **不要**用重试或加大超时去掩盖它：CI 上一台忙碌的 runner 会把"引导不起来"洗成通过，
  而那恰恰是这一档存在的理由。若需要判断"是不是卡住了"，比较进程的 CPU TIME 与 ELAPSED，
  再看 `sample <pid> 1` 的叶子帧（本轮看到的是 `psycopg … poll`，即 PG 档在跑，只是慢）。

- **每轮收尾的"干净树复算"（此前只活在会话记忆里，本轮第一次写下来）**：共享的 `.venv` 是
  **可编辑安装**，`import app` 会指回主树，所以在主树里跑绿并不等于"这份发布物自己绿"。
  复算要**同时换树和换环境**：`git worktree add <别处> HEAD` → 在那个目录里
  `uv sync --frozen --extra dev --extra postgres --extra s3`（由 `uv.lock` 造一份新 venv，别复用主树的）
  → `EMBODIEDCLOUD_KIND_BIN=<kind 路径> make validate` → `git diff --exit-code docs/VALIDATION.json docs/VALIDATION.md`
  （绿了但报告被改动＝提交里的读数不是这棵树算出来的）→ 收尾 `git worktree remove`。
  自 §7.1 的分档起，这一步多了一层用途：**它就是"提交面换环境逐字节相同"的第二次实测**（另一台机器、
  另一个 venv、另一套绝对路径）。本轮读数（HEAD `3917f5b`，574 例）：`uvsync_rc=0`、`validate_rc=0`、
  `freshness_rc=0`，工作树侧 `app_from=/private/tmp/attest-head/app`、`prefix=` 是该树自己的 `.venv`，
  两处的 `docs/VALIDATION.json` sha256 相同（`24a39df270…`）、`.md` 相同（`ceffc12b16…`），
  而该树的本次跑读数是 `integration_docker PASS 26/26`（主树本轮另外两跑分别是 `PARTIAL 执行 25/26`
  ——registry 通道抖，与四个依赖守护进程的档全 PENDING——`DOCKER_HOST` 被隔断）——
  档位不同、提交面相同，正是 §7.1 想表达的那件事。kind 二进制在 `/Volumes/Extra/CodeProj/.toolcache/kind`
  （不在 PATH 上；`tests/k8s_server.py` 的第二/三档发现逻辑会兜住它，但复算时显式给更稳）。
  本轮实测：`app_from=` 落在 worktree 内、`prefix=` 是 worktree 自己的 `.venv`、`make validate` 退 0、
  `544 passed / 1 skipped / 0 failed`、新鲜度那条 `git diff --exit-code` 无输出。
- **`make amd64-probe` 现在带退出码**：第 6 步汇总 `sync_rc` 与 `arch_verdict`，任一不成立 `exit 1` 并点名是哪一半
  （此前无论打印什么都退 0，"打印了 FAIL"与"这一步没跑"在 `make`/CI 那一层同形）。架构判据也从
  "扫 site-packages 目录名"换成"逐个 `.so` 读 ELF 头的 `e_machine`"（62＝x86-64、183＝AArch64，
  并且**一个 `.so` 都没扫到直接判 FAIL**，分母为 0 不算干净）。本轮读数：`.so 文件数= 22`、
  分布 `{'x86-64': 22}`、`machine= x86_64`、`sqlalchemy= 2.1.0 psycopg= 3.3.6`、`alembic 1.20.0`、
  `make_rc=0`（连跑两遍复现）。旧那条按目录名扫的读数打出来是 `1`——不是"只有 1 个原生扩展"，
  而是装完之后 `.dist-info` 目录名被归一化掉了平台标签，那条扫法数不清场上有 22 个原生二进制。

### 7.3 `make hang-probe`：外部依赖卡住时，代价与文案都被量过

两种造法都是真子进程、真超时（不是 monkeypatch）：`blackhole` 把 `DOCKER_HOST` 指向
RFC 5737 的 TEST-NET-1 地址（丢包≠拒连）；`hang-later` 在 PATH 前面放一个假 `docker`——
`version`/`info` 正常答、其余子命令 `sleep`，于是每一层探测都被**自己的超时**掐掉。
每档跑在独立子进程里，台子自带判据：任何一档给不出原因、或原因不可行动（N-48 那套判据），退出码非 0。

2026-09-27 三种剧本**同一轮**实测（`--timeout 20`、`--mode all`，`rc=0`，三张偏离表全空）：

| 档 | blackhole（版本探测就挂） | hang-later（版本通、后续全挂） | half-hang（只 `image ls` 挂） | half-hang-b（只 `image inspect` 挂） |
|---|---|---|---|---|
| docker | 20.22s | 80.60s | 0.66s | **80.51s** |
| postgres | 20.10s | 20.18s | 0.13s | 20.16s |
| object-store | 20.03s | 20.04s | 0.04s | 20.03s |
| k8s 控制面 | 20.03s | 40.04s | 20.04s | 20.04s |

读法：黑洞下每档只付**一次**探测超时；两个半挂列合起来才说明「谁在等」——掐 `image ls` 时只有 k8s 控制面等，掐 `image inspect` 时 docker 档最贵（它逐个探测候选镜像）、postgres 与对象存储档各付一次超时。结论由对偶剧本的角色互换支撑，而不是单一剧本的观察。

- 传输卡住与「取不到」是两张脸（N-91）：`docker pull` 卡在 registry 上时，**前置**（`gate_reason`）与**判据本体**都必须把这次调用交给 `tests/docker_probe.py` 那份异常吸收，再让 `_pull_failure_action` 按两条通道定档——通道慢 ⇒ `DOCKER_VALIDATION_PENDING` ＋两条读数，第二通道逐字节说没有 ⇒ 钉错的摘要必须红。运维读法：见到这一档 PENDING 就换一条出网通道再跑 `make control-image` 定案，别把它读成「配方没问题」，也别把一次 300s 超时读成代码失败。
关键是这两条都不会再出现 N-46 修前的形状（整档 26 条 `failed on setup`）——最坏就是上面这一列秒数，
然后干净跳过并留下一句可行动的原因。常驻判据用同一份实现的 `TIMEOUT=2` 快档
（`pytest tests/test_hang_probe.py`），人手动跑 `make hang-probe` 用真实超时。

## 8. 边缘设备（edge agent）

设备侧是独立包 `edge_agent/`（只依赖标准库，不 import `app`），装在机器人上：

```bash
# 1) 入网（在人值守的机器上做，用用户 access token；返回的 token 只显示一次）
embodiedcloud-edge-agent register --server https://cloud.example \
    --owner-token "$USER_TOKEN" --name arm-01
# → {"agent_id": "...", "token": "..."}

# 2) 常驻（凭据走环境变量：argv 上的 token 会被同机任何用户从 ps 里读到）
export EMBODIEDCLOUD_EDGE_SERVER=https://cloud.example
export EMBODIEDCLOUD_EDGE_AGENT_ID=<上一步>
export EMBODIEDCLOUD_EDGE_TOKEN=<上一步>
export EMBODIEDCLOUD_EDGE_WORKDIR=/var/lib/edge-agent
embodiedcloud-edge-agent run                    # 或 --json --iterations 1 做冒烟
```

每轮做的事：心跳 → 发现绑定给自己的部署 → `begin` → 流式取件（边写边算 sha256，
超体积上限即熔断并删除半成品）→ 与部署记录的期望摘要核对 → `report-checksum` →
mock 驱动装载并跑一次 → 以 `kind=edge-run` 回报遥测。任一步失败都会在同一格里说清它
走到了哪一步：发现响应不是清单 ⇒ 整轮硬失败（不会读成"今天没活"）；机器人已经上机而
遥测上报失败 ⇒ 那一格仍是 `ran`、另带 `reported: false`，退出码非零。运维看结果：
`GET /api/edge/agents/{id}/telemetry`。

排查顺序：

| 现象 | 先看 |
|---|---|
| 退出码 1、`无法与控制面通信` | token 是否被撤销/重装（服务端只存哈希，丢了只能重新 register） |
| 一轮下来 `assigned` 为空 | 部署有没有在 `POST /deployments` 时带 `edge_agent_id`（指派是控制面动作） |
| `ArtifactIntegrityError` | 产物被换过或链路损坏：文件不会被留下，也不会被喂给驱动；重下即可，若持续红查上游 artifact 登记 |
| 第二轮不跑驱动 | 有意为之：只在"本轮亲手推到 verified"的那一次上机（无运行游标，见 ADR 0007 后果段） |
| 退出码 1、`not a JSON array` | 发现端点回来的不是清单（中间层改了响应体）。设备不会把它读成"没活干"：查 base URL 是否被指到了网关/登录页 |
| stderr `已运行，但结果未能上报`、退出码 1 | 机器人**已经动过**，只是那条 `edge-run` 遥测没进控制面：`deployments/{id}` 会停在 verified 而无人知晓运行结果。别在没核对的情况下手工重跑同一模型 |

真机驱动接入点是 `edge_agent/drivers.py:build_driver`，目前只有 `mock`。


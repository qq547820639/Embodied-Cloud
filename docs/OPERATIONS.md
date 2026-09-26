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
| workspace_launch_failed_total 增量 | >0 持续 10min |
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
- 预授权（credit_holds）：`billing_hold_ttl_minutes`（默认 60）界定崩溃残留的泄漏窗口，
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

- **对象存储**：`EMBODIEDCLOUD_ARTIFACT_BACKEND=s3` 必须先装 SDK
  （`pip install ".[s3]"`）。凭据或 endpoint 不全时**启动即失败**
  （`BLOCKED_EXTERNAL_DEPENDENCY`），不会退回本地目录 —— 退回会让产物落在控制面
  文件系统，而 `Artifact.store_name` 仍记 `s3`，校验时找不到对象。
- **`EMBODIEDCLOUD_K8S_KUBECONFIG`**：留空时走 SDK 默认解析，但 kubernetes Python SDK
  在**模块 import 时**就把 `KUBECONFIG` 固化为常量（实测：进程启动后再 export 该变量，
  加载直接报 `Invalid kube-config file`）。要在进程生命周期内切换集群/挂载专用
  kubeconfig，请用本项而不是改环境变量。

## 7. 验证档位与前置条件

`make test` 一次跑全部档位；单档可点名单跑。缺前置条件的档位是**整档 skip +
在 `docs/VALIDATION.json` 记 PENDING(原因)**，CI 对需要 docker 的档位要求 PASS。

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
mock 驱动装载并跑一次 → 以 `kind=edge-run` 回报遥测。运维看结果：
`GET /api/edge/agents/{id}/telemetry`。

排查顺序：

| 现象 | 先看 |
|---|---|
| 退出码 1、`无法与控制面通信` | token 是否被撤销/重装（服务端只存哈希，丢了只能重新 register） |
| 一轮下来 `assigned` 为空 | 部署有没有在 `POST /deployments` 时带 `edge_agent_id`（指派是控制面动作） |
| `ArtifactIntegrityError` | 产物被换过或链路损坏：文件不会被留下，也不会被喂给驱动；重下即可，若持续红查上游 artifact 登记 |
| 第二轮不跑驱动 | 有意为之：只在"本轮亲手推到 verified"的那一次上机（无运行游标，见 ADR 0007 后果段） |

真机驱动接入点是 `edge_agent/drivers.py:build_driver`，目前只有 `mock`。


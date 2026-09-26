# ACCEPTANCE_GATES — EmbodiedCloud

> 版本：0.7.0（2026-09-26，v0.2.1–v0.4.0 软件面 + v0.6.0 验证纵深 + v0.7.0 边缘设备通路）。验收分级严格区分：**VERIFIED PASS** / **IMPLEMENTED BUT NOT PHYSICALLY VERIFIED** / **FAILED** / **BLOCKED_EXTERNAL_DEPENDENCY**。

## G0 软件验收（本机/CI 自动执行）

| Gate | 命令 | 期望 | 状态 |
|---|---|---|---|
| G0.1 Lint | `make lint` | ruff 0 错误 + shell syntax OK | VERIFIED PASS |
| G0.2 Type | `make typecheck` | mypy no issues | VERIFIED PASS |
| G0.3 Unit | `make test` | pytest 全绿（精确计数见 `docs/VALIDATION.json`，`make validate` 自动生成） | VERIFIED PASS |
| G0.4 Build | `make build` | wheel 产出 | VERIFIED PASS |
| G0.5 Smoke | `make smoke` | API 生命周期冒烟 | VERIFIED PASS |
| G0.6 Migrations | `alembic upgrade head` / `downgrade base` | empty→head 14 文件迁移链 + 关键表落地 + downgrade 循环 + **模型↔迁移对账**（`compare_metadata` 非空即红） | VERIFIED PASS |
| G0.7 OpenAPI freshness | `make api-docs && git diff --exit-code` | 重新生成无 diff | VERIFIED PASS |
| G0.8 GPU single authority | `pytest tests/test_gpu_single_authority.py` | DB↔docker --gpus 一致 | VERIFIED PASS |
| G0.9 Provision rollback | `pytest tests/test_provision_rollback.py` | 失败无孤儿资源 | VERIFIED PASS |
| G0.10 Durable ops/reconcile | `pytest tests/test_worker.py` | lease/串行/幂等 | VERIFIED PASS |
| G0.11 Immutable versions | `pytest tests/test_template_versions.py` | released 不可覆盖 | VERIFIED PASS |
| G0.12 Billing policy | `pytest tests/test_billing_policy.py` | 负余额/配额拒绝 | VERIFIED PASS |
| G0.13 Credential at rest | `pytest tests/test_workspace_credential.py` | DB 无明文密码 | VERIFIED PASS |
| G0.14 Auth session | `pytest tests/test_auth.py` | login/logout/me + verify_password 边界 + token 仅存哈希 | VERIFIED PASS |
| G0.15 GPU admin & release | `pytest tests/test_gpu_admin.py` | admin inventory + 分配→释放真实断言 | VERIFIED PASS |
| G0.16 Frontend 安全/课程 onboarding | `pytest tests/test_demo_workspace.py tests/test_course_onboarding.py tests/test_demo_checkpoint.py` | demo 页转义/鉴权、slug 加入、member 可见、mock checkpoint | VERIFIED PASS |
| G0.17 K8s integration | `pytest -m k8s_integration` | 无集群 → 整档 PENDING 并在 VALIDATION 登记原因（哨兵，不用 skip 冒充） | K8S_PHYSICAL_VALIDATION_PENDING（不假装 PASS） |
| G0.18 PostgreSQL 真并发 | `make test-pg` | 自建一次性 PG 容器；`FOR UPDATE`/`SKIP LOCKED`/CAS/部分唯一索引在真行锁下成对验证；**锁等待与容量不足分别报**（`GpuPoolContendedError` vs `No GPU available`），重试预算与真实持锁时长都量过读数 | VERIFIED PASS（21/21） |
| G0.19 Docker provider 真容器 | `make test-docker` | 真守护进程跑 health/start/logs/wait_ready/reconcile/流式占用；会话级容器泄漏守卫；`--gpus device=N` 由**守护进程自己的 HostConfig 记账**验收（DeviceIDs/Capabilities）、生产 argv 原样交给 daemon 且不残留孤儿容器 | VERIFIED PASS（21/21） |
| G0.20 浏览器真 DOM | `make test-browser` | Playwright + 系统 Chrome：XSS 载荷不执行、轮询真的停、控制台零错误 | VERIFIED PASS（11/11） |
| G0.21 K8s 线格式合规 | `pytest tests/test_k8s_model_conformance.py` | provider 生成的对象过真实 SDK 的 `sanitize_for_serialization` | VERIFIED PASS（6/6） |
| G0.22 预授权语义 | `pytest tests/test_credit_holds.py` | 圈住/转正/退回/过期回收 + 3 条走 worker 真实执行链 | VERIFIED PASS（14/14） |
| G0.23 供应链可复现 | `make verify-lock && make sbom && make audit` | universal uv.lock 一致、SBOM 生成、审计无阻断项；CI 要求 docker 档必须真 PASS | VERIFIED PASS |
| G0.24 约定入门禁 | `pytest tests/test_config_docs.py` + `test_migrations.py` 对账用例 | Settings↔.env.example 双向、模型声明↔迁移产物（`compare_metadata`） | VERIFIED PASS |
| G0.25 对象存储真后端 | `make test-s3` | 自建一次性 S3 兼容服务端（VersityGW v1.8.0）+ 真实 boto3：读写删/Content-Type/4 MiB 字节保真；「桶不存在」不得读成「对象不存在」；403 与网络故障上抛；SDK 异常不外泄；与 Local 档同判据并排 | VERIFIED PASS（20/20，另用 MinIO 交叉核对读数一致，见 ADR 0008） |
| G0.26 K8s 控制面真集群 | `make test-k8s-control-plane` | kind 一次性真集群（真 kubelet/调度器/endpoints）：provision 对象图与标签、§14 Pod 标记形态、`wait_ready` 三段判据的**正/负两档**（无 device plugin 时 Unschedulable ⇒ False；改规格后 ⇒ True 且独立复核 Endpoints 有地址）、凭据轮换真的滚出新 Pod 且无 Pod 残留旧口令、空口令不上报成功、stop/start 缩放与 destroy 无残留 | VERIFIED PASS（7/7，变异对照读数见 ADR 0009） |
| G0.27 镜像配方下载钉死 | `pytest tests/test_supply_chain.py` | `runtime/Dockerfile*` 内每个下载步骤必须同块 `sha256sum -c`、每个 `git clone --branch` 必须比对 HEAD commit；并断言判据作用域非空（防恒真） | VERIFIED PASS（改钉之前对两处开火，读数见 CHANGELOG 0.6.0） |
| G0.28 边缘设备真进程通路 | `pytest tests/test_edge_agent_api.py tests/test_edge_agent_client.py tests/test_edge_agent_e2e.py` | 设备侧三个新端点（发现 / begin / 取件）+ 遥测回读；e2e 是**真 uvicorn 子进程 + 真 agent 子进程 + mock 驱动**：一轮跑到 VERIFIED、落盘字节 sha256 与登记一致、`.part` 无残留、第二轮不重复上机；三道防线各有拆掉即红的读数（M1 前缀复核、M2 取件状态前提、M3 部署期绑定）；取件不得下发 `Content-Disposition`，错误文本不得含 token | VERIFIED PASS（变异读数见 ADR 0007） |
| G0.29 发布报告自证 | `pytest tests/test_version_consistency.py` | `failed ≥ 1` 的报告必须带出**是哪条用例**（名字取自 JUnit，不取日志文本）；全绿报告不得凭空造名字；`make lint/typecheck` 与 release 门禁的 ruff/mypy 目标集必须同源相等（钉住 `edge_agent` 在册）；`wait_status` 的正/反两档在"后台 worker 线程被掐住"这一确定前提下反向 | VERIFIED PASS（本轮新增，起因见 CHANGELOG 0.7.0） |
| G0.30 基础镜像钉 digest 且全仓同源 | `pytest tests/test_supply_chain.py` | 非 `embodiedcloud/` 命名空间的 `FROM` 必须带 `@sha256:`；未钉者必须与例外登记表**双向**对账（多登记＝死免检、漏登记＝新裸 tag，都红），例外须带固定词表的证据等级 + ≥40 字理由并与 `docs/SUPPLY_CHAIN.md` 逐字互核；消费侧（三脚本 + `docs/GPU_HOST.md`）引用同一镜像时必须与 Dockerfile 钉死的那份**逐字相等**（归属键剥掉 tag，否则 tag 漂移看不见），并按"必须覆盖到哪几个文件"做子集断言；三条判据作用域均须非空，且真实树现在必须**零个**未钉的外部基础镜像 | VERIFIED PASS（真实内容变异电池 SC1–SC6 六支全开火、干净副本 control 不开火；`python:3.12-slim` 本轮从"已登记例外"升级为钉死（权威 digest 取自 docker.io 的 pull 读数，并与上一轮第三方镜像站同值），例外登记表已空；空表下两个方向的开火夹具改由常驻注入用例 `test_exception_reconciliation_fires_in_both_directions` 承担（漏登记、死登记各开一次火，两侧皆空/两侧相等都不开火），构建侧实跑读数见 SUPPLY_CHAIN §2） |
| G0.31 惰性开关不说谎 | `pytest tests/test_config_docs.py` | 按 AST 数 `app/`（排除声明文件）里每个 `Settings` 字段的读取位置（属性访问与字符串形式两态都算）；**零读取字段集合必须恰好等于 `INERT_SETTINGS`**（漏登记＝运维按"设了就生效"设值，死登记＝文档宣称不生效而代码已在读）；登记项在其 `.env.example` 条目紧邻上方注释块必须带"未启用"标记（"预留"二字不算澄清） | VERIFIED PASS（34 字段中恰好 1 个零读取；CFG1 接上读取者→点名死登记、CFG2 抹掉标记→点名该字段、CFG3 用已知有读取者的 `ide_port_start` 证明探针不是恒真） |
| G0.32 workspace 镜像 digest 闭环 | `pytest tests/test_template_versions.py tests/test_cli.py` | `TemplateVersion.image_digest` 必须有写入入口（CLI `record-image-digest`：形制校验、幂等、released 版本已钉别处则拒改）与消费点（`image_ref.pinned_ref` 在 workspace 快照时拼 `image@sha256:…`，坏形制/摘要冲突/超列宽一律拒绝，不静默退回可变 tag）；整条链在同一份真库上连跑到 docker argv，并断言回填不改历史工作区快照 | VERIFIED PASS（PIN1–PIN4 四支变异各自翻红；本机 `docker image inspect` 的 .Id 与 .RepoDigests[0] 同值，两例独立实测） |
| G0.33 分配策略是量过的 | `make policy-bench` + `pytest tests/test_scheduler_policy.py` | GPU 候选排序（=分配策略）抽成 `candidate_order()`；同一个 `allocate()` 跑同一份工作负载换四种排序成表：现产 best-fit 必须接满（8/8、两个 48 GiB 都留得住、浪费率 1.00），其余三档（worst_fit／arrival／pack_host）必须都接不满；生产排序**按表达式直比**，不经认档函数；实测表逐格钉值；跑完必须复原生产排序 | VERIFIED PASS（POL1/POL2/POL3/POL4/POL6 五支变异各自翻红；POL5 单支不改今天判决，如实记为未变） |
| G0.34 终态不由第一次失败决定 | `pytest tests/test_worker.py tests/test_gpu_pool_guard.py` | `OperationWorker.will_retry(op)` 是"还有没有下一次尝试"的**唯一**判据，`finish_failure`（写 RETRYING/FAILED）与 `orchestrator._fail(terminal=...)`（写 workspace 状态）同读它；非终态失败停 QUEUED 且保留 `error_message`、归还卡，终态才 FAILED。两支极性常驻对照都在场：①注入"只失败一次"→ 第 1 轮后 workspace 必须不是 failed、op 是 retrying、卡回到 AVAILABLE，第 2 轮成功且 `error_message` 清空；②"每次都失败"→ 前 `MAX_ATTEMPTS-1` 轮逐轮不得出现 failed，最后一轮 workspace 与 op 同时 failed 且原因就是最后一次的。判据被短路成"永远终态"（变异 CONT2）时这两支必须翻红而其余 10 支照旧绿。另钉夹具侧：`tests/settle.py:await_workspace_settled` 的终态集合 `{running, failed}` 现在确实是终态，等不到要报"前提未达成 + 最后一次读数 + 池内空闲卡数"（正反两支：永远 queued 必红；`running`/`failed` 都不红且首读数未收敛 ⇒ 真在轮询） | VERIFIED PASS（本轮新增，起因与两份现场读数见 CHANGELOG 0.7.0 / CURRENT_STATE N-17；全套两连绿 528 passed / 1 skipped @ 宿主 load 15.7、28.4） |

## G1 物理 GPU 主机预检（BLOCKED_EXTERNAL_DEPENDENCY：本机无 NVIDIA 设备/容器运行时；docker daemon 本身可用，见 G0.19）

| Gate | 脚本 | 期望 PASS 条件 |
|---|---|---|
| G1.1 预检 | `scripts/preflight_gpu_host.sh` | docker + nvidia-smi + NVENC 存在；端口可绑定 |
| G1.2 兼容检查 | `scripts/gpu_acceptance.sh` | isaac-sim compatibility_check 退出码 0 |

## G2 Isaac Sim GPU 渲染 / headless（BLOCKED）

| Gate | 脚本 | 期望 PASS 条件 |
|---|---|---|
| G2.1 | `scripts/isaac_sim_smoke.sh` | 容器内 headless 渲染 10s 无异常 |

## G3 Isaac Lab Cartpole（BLOCKED）

| Gate | 脚本 | 期望 PASS 条件 |
|---|---|---|
| G3.1 | `scripts/isaac_lab_cartpole_smoke.sh` | 5 iteration 训练完成，loss/reward 非 NaN（模板 `cartpole`） |

## G4 Franka manipulation / WebRTC（BLOCKED）

| Gate | 脚本 | 期望 PASS 条件 |
|---|---|---|
| G4.1 | `scripts/franka_smoke.sh` | Franka Lift 训练或推理 smoke 完成（模板 `franka-lift`） |
| G4.2 | WebRTC 流 | 模板 `franka-pick-place`：49100/TCP + 47998/UDP 监听，客户端可见仿真画面 |

## G5 真机 Sim2Real（BLOCKED：无真实机器人）

| Gate | 脚本 | 期望 PASS 条件 |
|---|---|---|
| G5.1 | edge agent + RobotDriver（Mock） | 真进程回环：`tests/test_edge_agent_e2e.py` 起真控制面 + 真 `python -m edge_agent` 子进程，走完 发现→begin→取件→核对→上报→驱动→遥测（控制台「部署/边缘设备」页仍是人工入口） |
| G5.2 | 物理真机 | 需要真实机器人；本环境不可执行 |

## 当前总状态

- VERIFIED PASS：G0 全部（精确计数见 `docs/VALIDATION.json`，`make validate` 自动生成）
- IMPLEMENTED BUT NOT PHYSICALLY VERIFIED：Streaming 媒体面、G1–G4 脚本、
  K8s 上 `nvidia.com/gpu` 的真实分配（需要 device plugin；控制面路径本身已由 G0.26 覆盖）
- 已由真后端覆盖（不再属于上一条）：Docker provider 非 GPU 路径与 `--gpus` 参数的守护进程
  侧记账（G0.19）、PostgreSQL 并发语义（G0.18）、前端真实 DOM（G0.20）、
  对象存储 S3 协议（G0.25）、K8s `wait_ready`/凭据轮换（G0.26）、
  边缘设备取件通路与真进程 Sim2Real 回环（G0.28）
- FAILED：无
- BLOCKED_EXTERNAL_DEPENDENCY：G1.1–G4（Docker/NVIDIA/NGC）、G5.2（真机）

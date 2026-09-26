# ADR 0007: edge agent 的分派/取件鉴权面

状态：Accepted（2026-09-26 记录裁决与前置问题；同日实施完毕，实施记录见文末）

## 背景

`docs/CURRENT_STATE.md` 把"edge agent 独立包（§25）"记为一条待办。实测它的性质：

- 仓库内**没有 agent 客户端**：`runtime/` 只有两个 Dockerfile 和 entrypoint；
  `X-Agent-Token` / heartbeat 相关代码只出现在 `app/`（服务端）与迁移里。
- 服务端也没有 agent 侧的**工作发现**与**取件**通路：`app/routers/edge.py` 只有
  register/heartbeat/telemetry/查询，`app/routers/deployments.py` 里 agent token
  只被允许 `report-checksum`，`download/verify/run/complete` 四个迁移全部要求用户
  bearer token。也就是说当时的 Sim2Real 链路是"控制面替 agent 走状态机"。

## 候选方案对比（六维；检索结果只列本轮实际读到的）

| 维度 | A 复用 RAUC 类 OTA 客户端 | B MQTT 设备 SDK（uLink 系） | C 自建 agent + 新增 agent 端点（选定） |
|---|---|---|---|
| 功能匹配度 | 低：RAUC 定位是"控制嵌入式 Linux 的升级过程"，产物是整机安装 artifact（A/B 槽位）；我们要下发的是单个模型 checkpoint + 调驱动 | 中：遥测/命令通道现成，但我们的控制面是 REST 且 deployment 状态机已在服务端 | 高：逐条对上 6 个既有端点，只补"发现分派""开门""取件"三处 |
| License 兼容性 | 未核实（Debian 包页未显示 license 字段，故不下结论） | 未检索到一手资料（当时的搜索只返回无关页面）→ 不据此判断 | 无新增第三方依赖（标准库 HTTP 客户端即可） |
| 维护活跃度 | 不引入即无维护面；引入则绑上游发布节奏 | 同左，且额外要维护 broker | 我方 ~450 行（含 CLI），随控制面同栈演进 |
| 安全风险 | 高：需要 bootloader/slot 前提，且引入外部二进制执行链 | 高：新增 broker 攻击面 + 第二套凭据体系 | 中：**新增按 agent token 鉴权的取件端点**，必须处理租户绑定、路径穿越、越权 404（SECURITY.md T1） |
| 代码质量 | 外部黑盒，难被本仓常驻用例覆盖 | 外部黑盒 | 可被 e2e 覆盖：起真 uvicorn + 跑真 agent 子进程 + mock 驱动 |
| 适配成本 | 高（整机镜像模型与我们的 artifact 模型不同） | 中高（协议改造 + 基础设施） | 低（复用既有端点与 mock 驱动） |

## 两个前置问题的裁决（2026-09-26，一手资料）

实施前查了 AWS IoT Jobs 的任务生命周期文档（`docs.aws.amazon.com/iot/latest/developerguide/iot-jobs-lifecycle.html`，
本轮实际读取）与 Jobs 总览（`iot-jobs.html`）。据这两页的事实定：

1. **发现方式：独立端点，不塞进 heartbeat。**
   AWS 把"通知"（MQTT `$notify-next` 主题）与"取任务"（`StartNextPendingJobExecution`
   API）分成两件事，通知只是可选优化。我们的控制面是 REST，没有常驻消息通道，
   于是取"AWS 的那条主路径"：`GET /edge/agents/{id}/deployments/assigned` 轮询。
   heartbeat 保持"我还活着"的单向登记——若让它顺带返回任务，心跳就变成写路径，
   每次心跳都可能改变分派，重试语义立刻说不清。
2. **状态迁移由设备发起。**
   同一页的表列明 `QUEUED → IN_PROGRESS`、`→ SUCCEEDED/FAILED` 是"Initiated by device"，
   而 `QUEUED` 本身由服务端 rollout。对应到我们这边：控制面在 `POST /deployments`
   时就把部署 rollout 给设备（`edge_agent_id` 绑定，等价于 QUEUED），
   设备用 `POST .../begin` 说"我开始取了"（等价于 IN_PROGRESS）。
   这一步不是装饰：`report_checksum` 只接受 `downloading`（§23 防绕过），
   没有设备侧的开门动作，就得让控制面替设备写 `downloading`——那又回到"控制面替 agent 走状态机"。
3. **取件授权：agent token 直取，不用预签名 URL。**
   预签名那条需要 S3/预签名基础设施，而 `ArtifactStore` 协议只有
   `put/get/exists/delete`（v0.6.0 刚被真服务端验过），local 后端根本没有可签名对象。
   落一个新鉴权分支的代价，比给设备一个"按记录取字节"的只读端点更可控——
   前提是三道防线齐全：租户/绑定校验、状态前提、object_key 前缀复核（见下）。

## 实施记录（同日）

新增服务端点（全部 `X-Agent-Token`，除最后一条是用户侧读路径）：

| 端点 | 作用 |
|---|---|
| `GET  /api/edge/agents/{id}/deployments/assigned` | 工作发现：只看绑定给自己的部署 |
| `POST /api/edge/agents/{id}/deployments/{dep}/begin` | pending → downloading（条件 UPDATE，重复调用幂等） |
| `GET  /api/deployments/{dep}/artifact` | 取件：字节 + `X-Artifact-Sha256` + `X-Artifact-Size` + ETag |
| `GET  /api/edge/agents/{id}/telemetry` | 遥测回读（用户侧；此前 `report_telemetry` 只写不读） |

设备侧新增 `edge_agent/` 包（标准库，不 import `app`）：`client.py`（流式取件 +
边写边算摘要 + 体积熔断 + `.part` 原子改名）、`drivers.py`（mock 驱动）、
`agent.py`（一轮编排）、`__main__.py`（CLI，凭据走环境变量而不是 argv，避免 `ps` 泄漏）。

三道防线的开火读数（每条都是"把防线拆掉，看用例是否翻红"）：

| 变异 | 拆掉的东西 | 读数 |
|---|---|---|
| M1 | `read_artifact` 的 workspace 前缀复核 | `test_planted_object_key_pointing_at_another_workspace_is_not_served` 翻红，且攻击者拿到 **200 + victim 的字节**（`assert 200 == 404`）——这道防线是真的在挡事，不是装饰 |
| M2 | 取件的 `downloading` 状态前提 | `test_fetch_requires_begin_first` 翻红（未 begin 也能取到件，200 而非 409） |
| M3 | `POST /deployments` 的 `edge_agent=agent` 绑定 | 发现面变空（`[] == ['ran']` e2e 红 + API 档 `ids == [dep["id"]]` 红），证明整条链真依赖绑定关系 |

未被常驻用例覆盖的部分（如实登记，不假装）：`begin_agent_download` 的条件 UPDATE 里
`edge_agent_id == agent.id` 这一支**没有独立开火对照**——它和路由层的绑定校验语义重叠，
单线程用例观测不到差别（两种写法都返回同一个 `downloading`）。它的作用是防"路由层将来
被人改动后状态机被越权推进"，属冗余防线；要让它可观测，需要一条并发 begin 的 PG 档用例。

## 后果

- `docs/CURRENT_STATE.md` §25 与 TECH DEBT 措辞按事实更新：组件已存在并被常驻用例覆盖。
- Gate G5.1 从"控制台页走通"升级为**真进程 e2e**（`tests/test_edge_agent_e2e.py`：
  真 uvicorn 子进程 + `python -m edge_agent` 另一个子进程 + mock 驱动），
  并断言"第二轮轮询不得重复上机"。
- 运行结果只由设备写、由人读：`verified` 之后是否 `run/complete` 仍归控制面
  （要绑 GPU 工作区、要结算账本）。设备把结果写成 `kind=edge-run` 的遥测。
- 已知限制（有意为之，不是遗漏）：**没有运行游标**。设备只在"本轮亲手把它推到 verified"
  时跑一次驱动；若进程恰好崩在 verified 之后、驱动之前，这条部署不会被自动补跑，
  需要控制面重新下发。补它要先回答"如何证明上一次运行没真的跑过"，属下一轮设计题。
- 真机（G5.2）仍 BLOCKED：设备侧只有 mock 驱动，`build_driver()` 是唯一替换点。

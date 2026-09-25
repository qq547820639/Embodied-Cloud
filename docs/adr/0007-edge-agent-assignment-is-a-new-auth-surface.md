# ADR 0007: edge agent 需要一个新的分派/取件鉴权面，先不顺手实现

状态：Proposed（2026-09-26，记录裁决与前置设计问题；未实施）

## 背景

`docs/CURRENT_STATE.md` 把"edge agent 独立包（§25）"记为一条待办。本轮实测它的性质：

- 仓库内**没有 agent 客户端**：`runtime/` 只有两个 Dockerfile 和 entrypoint；
  `X-Agent-Token` / heartbeat 相关代码只出现在 `app/`（服务端）与迁移里。
- 服务端也没有 agent 侧的**工作发现**与**取件**通路：`app/routers/edge.py` 只有
  register/heartbeat/telemetry/查询，`app/routers/deployments.py` 里 agent token
  只被允许 `report-checksum`，`download/verify/run/complete` 四个迁移全部要求用户
  bearer token。也就是说今天的 Sim2Real 链路是"控制面替 agent 走状态机"。

## 候选方案对比（六维；检索结果只列本轮实际读到的）

| 维度 | A 复用 RAUC 类 OTA 客户端 | B MQTT 设备 SDK（uLink 系） | C 自建 agent + 新增两个 agent 端点（建议） |
|---|---|---|---|
| 功能匹配度 | 低：RAUC 定位是"控制嵌入式 Linux 的升级过程"，产物是整机安装 artifact（A/B 槽位）；我们要下发的是单个模型 checkpoint + 调驱动 | 中：遥测/命令通道现成，但我们的控制面是 REST 且 deployment 状态机已在服务端 | 高：逐条对上 6 个既有端点，只补"发现分派"和"取件"两处 |
| License 兼容性 | 未核实（Debian 包页未显示 license 字段，故不下结论） | 未检索到一手资料（本轮搜索只返回无关页面）→ 不据此判断 | 无新增第三方依赖（标准库 HTTP 客户端即可） |
| 维护活跃度 | 不引入即无维护面；引入则绑上游发布节奏 | 同左，且额外要维护 broker | 我方 ~200 行，随控制面同栈演进 |
| 安全风险 | 高：需要 bootloader/slot 前提，且引入外部二进制执行链 | 高：新增 broker 攻击面 + 第二套凭据体系 | 中：**新增一个按 agent token 鉴权的文件下载端点**，必须处理租户绑定、路径穿越、越权 404 语义（SECURITY.md T1） |
| 代码质量 | 外部黑盒，难被本仓常驻用例覆盖 | 外部黑盒 | 可被 e2e 覆盖：起真 uvicorn + 跑真 agent 子进程 + mock 驱动 |
| 适配成本 | 高（整机镜像模型与我们的 artifact 模型不同） | 中高（协议改造 + 基础设施） | 低（复用既有端点与 mock 驱动），但**先要做鉴权设计** |

## 决策

1. 现在**不**实现 agent 客户端，也**不**新增鉴权面：在版本收口轮里塞一个按 agent token
   取文件的端点，属于未经独立评审的安全面扩张，风险高于收益。
2. 把该条从 TECH DEBT 改记为"组件缺失 + 前置设计问题"，并锁定两个必须先回答的问题：
   - 发现方式：agent 轮询 `GET /api/edge/agents/{id}/deployments/assigned`，
     还是复用 heartbeat 响应携带任务？（后者少一个端点、少一次往返，但把分派语义
     塞进心跳会让 heartbeat 变成写路径）
   - 取件授权：agent token 直取 `/deployments/{id}/artifact`（流式 + `sha256` 头），
     还是控制面下发一次性预签名 URL？（后者不落新鉴权分支，但需要 S3/预签名基础设施，
     而 S3 凭据本身仍 BLOCKED）
3. 实施那一轮必须自带：越权 404 用例（agent A 取 agent B 的件）、路径穿越用例、
   真起 server 的 e2e（浏览器档与 Docker 档已证明这条套路可行）。

## 后果

- `docs/CURRENT_STATE.md` 的 §25 措辞已按上述事实更正，不再暗示"只是没打包"。
- Gate G5（真机 Sim2Real）在此之前只能保持 mock 闭环，不能宣称有真 agent 通路。

# 实施计划与执行 Gate

> 这份计划不是“愿景路线图”，而是从当前仓库到可收费 Beta 的执行清单。每一阶段都有明确退出条件；前一 Gate 不过，不进入后一阶段。

## Iteration 0 — 产品收敛【已完成】

- 范围从“具身智能云 + 课程 + 硬件 + 国产引擎”收敛为 Isaac Lab Cloud Workspace。
- 产品单位从“云主机”改成“可运行 Golden Template”。
- 一工作区一 GPU，优先可复现与稳定性。
- Phase 2（硬件/Sim2Real）由用户付费和任务使用数据触发，不按日历触发。

**Gate：** 一个陌生开发者只看模板名就能理解“点开会得到什么”。

## Iteration 1 — 控制面 MVP【已完成并自动测试】

- FastAPI API
- SQLite 状态持久化
- Template Catalog
- Workspace 状态机
- Mock Provider
- 浏览器 Dashboard
- 运行中 + 已停止时长计量
- code-server access secret API
- API 自动测试

**Gate：** 无 GPU 可完整跑通 `create -> running -> IDE demo -> access -> stop -> delete`。

## Iteration 2 — 单机 GPU Provider【代码已完成；真实 GPU 验收待外部主机】

- Docker provider
- `nvidia-smi` GPU 发现
- 一 GPU 一 Workspace 分配
- 独立 IDE 端口
- Streaming 单槽位保护
- code-server 随机密码
- Isaac Sim 6.0.1 + Isaac Lab 3.0 Beta 2 workspace image recipe
- GPU host preflight/build/run 脚本
- WebRTC 端口/安全约束固化

**外部依赖 Gate：** 必须在真实 NVIDIA GPU 主机依次完成 G1/G2/G3，详见 `GPU_HOST.md`。当前执行环境没有 Docker daemon / NVIDIA GPU，所以不能伪造这一步的“通过”。

## Iteration 3 — 商业 Beta 基础设施【下一步 1–7 天】

### D1：GPU 真机验收 + 基准

动作：
1. 跑 `scripts/preflight_gpu_host.sh`；
2. 跑 NVIDIA compatibility checker；
3. build workspace image；
4. 跑 G1/G2/G3；
5. 记录 cold build、cold start、warm start、显存峰值、失败日志。

退出条件：3 个 Golden Template 在同一主机重复 3 次均成功。

### D2：冻结 Golden Image

动作：镜像 build 成功后用 digest 固定；模板表新增 image digest（Beta 迁移项），禁止 `latest`。

退出条件：旧实验 7 天后仍可用同一 digest 复现。

### D3：入口安全

动作：Nginx/Caddy TLS；8000 只由 gateway 暴露；IDE/WebRTC 仅受控网络访问；按源 IP 收紧防火墙。

退出条件：公网无法直接访问裸 IDE/WebRTC 端口。

### D4：身份与租户

动作：接 OIDC；Workspace 加 `owner_id`；API 所有 workspace 路由做 owner/role 校验。

退出条件：A 用户无法读取/停止 B 用户 workspace。

### D5：成本控制

动作：heartbeat + idle policy；训练长任务用显式 lease 防误杀；每个 workspace 强制 max-runtime。

退出条件：无人使用的 workspace 不会无限烧 GPU。

### D6：Credit Ledger

动作：只做 `credit_grant / usage_debit / adjustment` 三类账本事件；先后台充值，不先接复杂支付。

退出条件：每一 GPU 秒都能追溯到账本事件，余额不能静默变负。

### D7：首批 10 个开发者

只观察：
- 启动成功率；
- 从注册到第一个成功实验的时间；
- 第二次回来使用率；
- 是否愿意付费。

退出条件：至少 5/10 能不靠人工介入完成一个实验，否则不扩流量。

## Week 2–4 — 可收费 Beta

- PostgreSQL
- 账户/组织/课程班
- Credit ledger + usage event
- 模板版本与 image digest
- workspace warm pool：热门模板每种 1 个
- 失败诊断：driver / image / asset / WebRTC 分层错误码
- 5 个 Golden Template
- 高校课堂模式：教师建班 -> 学生复制相同模板

**Gate：** 30 个真实 Workspace，启动成功率 >90%；warm start P50 <30s（不把首次 shader/cache 计入承诺）。

## Week 5–8 — 多机化

- Kubernetes Provider（替换 Docker Provider，不重写 API/UI）
- NVIDIA GPU Operator
- PostgreSQL + queue
- PVC + Object Storage
- DCGM 指标
- 用户/组织 GPU quota
- IDE 动态 Ingress
- 独立 Streaming session gateway

**Gate：** 2+ GPU 节点；控制面重启不杀训练；每 Workspace GPU 秒可追踪。

## Week 9–12 — 高校试点与收费闭环

- 课程班/邀请码
- 教师 dashboard
- 课时预算
- 对公付款/支付后自动 credit
- 10 家实验室 Demo SOP

**商业 Gate：** 至少 2 个付费实验室，或 10 个持续付费开发者。否则暂停硬件套件研发。

## Phase 2 Gate — Sim2Real

只有同时满足以下条件才做官方硬件：

1. 5 个以上 Golden Template 持续有人用；
2. 至少一个机械臂任务有稳定 policy 导出；
3. 明确单一机器人型号；
4. 至少 3 个客户主动要求真机验证。

随后做本地 `embodied-agent`：`拉取签名 policy -> 校验 runtime/robot 版本 -> robot driver -> inference`。只承诺官方支持型号，不承诺任意机器人通用。

## 永不并行启动的事项

- 自研国产仿真引擎：只留长期研究愿景，不占当前研发资源。
- 通用资产商城：没有稳定活跃开发者前不做。
- 自研大硬件：没有 Sim2Real 需求证据前不做。

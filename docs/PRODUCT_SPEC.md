# PRODUCT_SPEC — EmbodiedCloud

> 版本：0.7.0（2026-09-26）。取代早期 `docs/PRODUCT_SCOPE.md` 的定位描述，保留其边界并固化为规范。

## 1. 产品使命

让机器人/具身智能开发者**无需自行配置 GPU、CUDA、Isaac Sim、Isaac Lab** 等复杂环境，
通过浏览器启动可运行的机器人实验，完成：

> 选择 Template → 创建 GPU Workspace → 浏览器编码 → 运行 Isaac Lab → 查看实时仿真
> → 训练 → 保存 checkpoint → Deploy → 真机验证

## 2. 产品层

1. **Cloud Workspace / SaaS**（当前核心）：浏览器起 GPU 工作区，Template 一键可跑。
2. **Template / Content / Experiment Hub**：模板即产品 SKU，version-locked、可验收、可计费。
3. **Sim2Real / Edge Agent / Hardware validation**：checkpoint → artifact → 真机验证。

## 3. 用户旅程（主路径）

1. 注册登录（User/Org）
2. 浏览 Template（5 个首批 SKU，version locked）
3. 创建 Workspace（排队 → 调度 GPU → provisioning → running）
4. 浏览器 code-server 编码
5. 运行 Isaac Lab 训练
6. 实时仿真（WebRTC streaming）
7. 保存 checkpoint
8. Deploy 到 Edge Agent / 真机

## 4. 核心对象

| 对象 | 说明 |
|---|---|
| User / Organization / Role | user / admin / org_admin / instructor / student（课程模块已实现并接线） |
| Template | slug/version/image/gpu_requirement/entrypoints/outputs/streaming/healthcheck/metadata |
| Workspace | 生命周期 CREATED→QUEUED→PROVISIONING→RUNNING→STOPPING→STOPPED/FAILED/DELETED |
| GpuHost / Gpu | inventory：AVAILABLE/ALLOCATED/DRAINING/DRAINED（后两个分别是机器缺席判决与人工下架判决，只有前者会自动归位；管理员的下架意图另有证据列 `drain_requested_at`，占用中也能提出，N-125）；健康另有 `gpus.health` 一列，不再是状态值（N-126） |
| CreditLedger | 不可变账本：RECHARGE/USAGE/PROMOTION/REFUND/ADJUSTMENT |
| Course / Lab / Assignment / Submission | 高校场景 |
| DeploymentRecord / Artifact | checkpoint→artifact→checksum→deploy→verify→run |
| StreamingSession | starting/ready/connected/disconnected/failed |

## 5. 非目标（当前阶段）

- 完整国产物理仿真引擎（保留 SimulationProvider 抽象，当前绑定 Isaac Sim/Lab）
- 复杂 LMS、复杂 MIG 切分、公网多租户 SaaS 的计费/支付网关
- 任何"真实硬件验证"在本机不可用时，一律标记 PHYSICAL_GPU_VALIDATION_PENDING，不标 PASS

## 6. 商业 Gate（预留指标）

- Gate1：10 real users；≥5 independently complete experiment
- Gate2：30 active developers；≥15 experiment completions
- Gate3：≥5 paying users 或 ≥2 paying labs
- 指标：registered_users / activated_users / workspace_users / experiment_completed / gpu_hours / paying_users / paying_labs

在商业 Gate 未验证前，优先改善 activation / reliability / onboarding / templates，而非无限增加基础设施复杂度。

## 7. 停止条件

A. 当前计划全部完成并验收；或
B. 剩余任务全部需要当前环境不存在的物理资源/凭据，且不依赖这些资源的代码、自动测试、文档、验收脚本已完成（输出 BLOCKED EXTERNAL ITEMS 逐项说明）。

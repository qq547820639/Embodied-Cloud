# Changelog

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

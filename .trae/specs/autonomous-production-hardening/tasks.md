# Tasks

- [x] Task 1: 治理文档与构建基线
  - [x] 1.1 创建 docs/PRODUCT_SPEC.md, ARCHITECTURE.md, MASTER_PLAN.md, ACCEPTANCE_GATES.md, SECURITY.md, RUNBOOK.md, OPERATIONS.md, RELEASE_PROCESS.md, docs/adr/ (0001-*.md)
  - [x] 1.2 pyproject 增加 ruff/mypy 配置（豁免有意 BLE001）、pytest 配置；Makefile 补全 install/dev/lint/typecheck/build/smoke/clean/gpu-preflight/gpu-test/compose-up/compose-down
  - [x] 1.3 CI 增加 lint/type/unit/integration/build/artifact 阶段
  - [x] 1.4 验证：make lint / make typecheck / make test / make build 全绿

- [x] Task 2: 数据模型 + 迁移体系
  - [x] 2.1 扩展 models：User/Organization/Role/UserSession/GpuHost/Gpu/GpuAllocation/CreditLedger/StreamingSession/Template 完整字段/Course/CourseMember/Lab/Assignment/Submission/DeploymentRecord/Artifact
  - [x] 2.2 引入 Alembic，SQLite 本地 + PostgreSQL 生产；保留幂等 seed
  - [x] 2.3 WorkspaceStatus 增加 CREATED/DELETED；crash recovery：启动时恢复悬置 PROVISIONING→FAILED，归还 GPU
  - [x] 2.4 验证：迁移 up/down 测试 + 全量测试通过

- [x] Task 3: Auth + 隔离
  - [x] 3.1 session 认证（OIDC-ready 接口），password hash (PBKDF2-SHA256 600k)
  - [x] 3.2 user/admin 角色；Workspace/Template/Usage 查询强制 owner/org 过滤
  - [x] 3.3 测试：User A 不能读/改/删 User B 的 workspace
  - [x] 3.4 验证：隔离测试全绿

- [x] Task 4: GPU Scheduler + Provider 完备
  - [x] 4.1 GpuHost/Gpu inventory + 原子分配（SELECT FOR UPDATE skip_locked）+ 状态机 AVAILABLE/ALLOCATED/UNHEALTHY/DRAINING
  - [x] 4.2 Provider 接口扩展：provision/stop/destroy/inspect/logs/health；Mock 增强
  - [x] 4.3 KubernetesWorkspaceProvider（create/start/stop/delete/inspect/logs/health）——k8s python client 懒加载，可离线单测（mock client）
  - [x] 4.4 crash recovery + 并发分配测试（防重复分配）
  - [x] 4.5 验证：scheduler 并发测试 + provider 单测全绿

- [x] Task 5: Credit Ledger + Usage
  - [x] 5.1 不可变 ledger（Transaction: RECHARGE/USAGE/PROMOTION/REFUND/ADJUSTMENT），幂等键唯一约束
  - [x] 5.2 workspace 运行计费：stop/delete 时结算 GPU 秒数；restart-safe 幂等重放
  - [x] 5.3 GET /api/usage 改 ledger-based
  - [x] 5.4 验证：幂等/审计/不重复扣款测试

- [x] Task 6: Template Registry + Streaming + Warm Pool
  - [x] 6.1 Template 模型完整化 + 5 个首批模板（cartpole/franka-lift/franka-pick-place/domain-randomization/rgbd-perception）version locked，禁 latest
  - [x] 6.2 Streaming 状态机 + 会话端点 + 模拟链路测试（start/connect/disconnect/reconnect/stop/port cleanup）
  - [x] 6.3 Warm Pool：pool manager + metrics + benchmark harness（P50/P95 记录）
  - [x] 6.4 验证：streaming 测试 + pool 单测全绿

- [x] Task 7: Observability
  - [x] 7.1 /metrics Prometheus（workspace_launch_total/failed/seconds/running、gpu_allocated/gpu_seconds、template_*、stream_*）
  - [x] 7.2 结构化日志（JSON）+ request_id 中间件 + 敏感字段脱敏
  - [x] 7.3 验证：metrics 端点测试 + 日志脱敏测试

- [x] Task 8: Course + Edge Agent
  - [x] 8.1 Course/CourseMember/Lab/Assignment/Submission + 教师/学生流程 API
  - [x] 8.2 embodied-edge-agent 模块（register/heartbeat/device_info/download/verify/start/stop/telemetry）+ RobotDriver interface + MockRobotDriver
  - [x] 8.3 Deployment 流程：checkpoint→artifact→checksum→deployment record→edge download→verify→run
  - [x] 8.4 验证：course 流程测试 + edge agent 单测

- [x] Task 9: GPU 验收脚本 G1–G4
  - [x] 9.1 scripts/isaac_sim_smoke.sh (G2), isaac_lab_cartpole_smoke.sh (G3), franka_smoke.sh (G4)
  - [x] 9.2 全部标记 PHYSICAL_GPU_VALIDATION_PENDING（本机无 GPU），无 GPU 时输出 BLOCKED_EXTERNAL_DEPENDENCY 退出码 2
  - [x] 9.3 验证：脚本 bash -n + dry-run 通过

- [x] Task 10: Release + 最终验证
  - [x] 10.1 release 脚本（semver、CHANGELOG、checksum、migration notes、分级验证矩阵 PASS/FAIL/BLOCKED）
  - [x] 10.2 更新 CURRENT_STATE/MASTER_PLAN/ACCEPTANCE_GATES；冒烟 API 端到端
  - [x] 10.3 checklist 全量验证；git commit + push origin/main

# Task Dependencies
- Task 2 depends on Task 1 (构建基线)
- Task 3/4/5/6 depend on Task 2 (数据模型)
- Task 7 depends on Task 2/4 (metrics 基于 workspace/gpu 状态)
- Task 8 depends on Task 3 (auth) / Task 2 (deployment 记录)
- Task 9 与 Task 1–8 并行可执行（脚本独立）
- Task 10 depends on 全部

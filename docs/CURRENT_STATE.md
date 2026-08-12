# CURRENT_STATE — EmbodiedCloud

> 版本：**0.3.0 → v0.3.1 Security & Integrity Hotfix 软件面完成**（2026-08-12）。
> 数据来源：`docs/VALIDATION.json`（`make validate` 自动生成，JUnit 稳定计数）。

## 1. 本次真实验证（实测，非复制旧文档）

| Gate | 结果 |
|---|---|
| Test | **PASS（226 passed + 1 skipped[k8s_integration]，JUnit 计数）** |
| Lint / Type / compileall | PASS（ruff 0 / mypy 39 files / compileall） |
| Migration | PASS（clean DB empty→head 10 级链 + downgrade 循环） |
| Build / Smoke | PASS（embodiedcloud-0.3.0 / SMOKE_OK） |
| OpenAPI / VALIDATION freshness | PASS（make api-docs / make validate） |
| Release | PASS（lint/type/test/build + archive 清洁） |

## 2. v0.3.1 本轮修复（全部有 regression test）

| § | 内容 | 测试 |
|---|---|---|
| §5 P0 | **Deployment checksum bypass 移除**：download 端点不再自动 VERIFIED（删除无校验 verify() 方法）；唯一 VERIFIED 路径 = Edge 下载 → 本地 SHA256 → report-checksum → server 比较 | test_deployment_bypass.py（7 用例） |
| §6 P0 | **EdgeAgent 租户所有权**：owner_user_id/organization_id + migration；register 必须认证（匿名 401）；list/get/部署派发全部 tenant scope；edge router 统一走 app.deps | test_edge_ownership.py（6 用例） |
| §7 P0 | **Warm pool rotation 失败资源泄漏修复**：完整补偿（streaming terminate → runtime destroy → GPU release → 清凭据/owner/端口 → DRAINING/FAILED）；providers 增加 supports_credential_rotation（Docker False → warm pool 自动禁用） | test_warmpool_claim.py（4 新用例） |
| §8 P0 | **BillingPolicy settings 真实接线**（deps 注入 minimum_launch_minutes/enforce，不再 constructor default） | test_billing_policy.py（3 新用例） |
| §9 P0 | **Runtime readiness contract**：provider.wait_ready（Mock/Docker[TCP+HTTP+healthcheck exec]/K8s[Deployment+Pod Ready+endpoints]）；未就绪 → 回滚 → FAILED；_fail 清 GPU 字段 | test_runtime_readiness.py（4 用例） |
| §12 P0 | **Durable STOP/DESTROY**：端点 enqueue operation + fast-path tick；worker 全覆盖 5 种 operation；重启不丢 cleanup | test_durable_ops.py（5 用例） |
| §13 P0 | **Active operation 数据库级唯一性**：部分唯一索引 uq_ops_active_per_workspace（SQLite/PostgreSQL where 均生效）；enqueue 依赖 DB 拒绝并发 | test_worker_fencing.py 并发用例 |
| §11 P0 | **Worker 外部副作用 fencing**：operation_id/fencing_token 注入 reservation → Docker labels；provision 幂等 adopt（容器存在复用）；lost lease 后旧 worker 不能写终态 | test_worker_fencing.py（3 新用例） |
| §10 P0 | **K8s reservation→node 真相**：ResourceReservation.node_name 明确字段；真实模式禁 reservation=None；nodeSelector 由 node_name 构造；reconcile 检测 pod nodeName ≠ reserved → FAILED | test_k8s_node_truth.py（5 用例） |
| §14 | **K8s manifest 修复**：Pod labels 增加 embodiedcloud.workspace:true（NetworkPolicy selector 真实命中，消除假安全） | test_pod_labels_hit_network_policy_selector |
| §15 | **版本单一来源**：pyproject 事实源；UI 版本动态（/api/health）；manifest 0.3.0 | test_version_consistency.py（6 用例） |
| §16 | **VALIDATION 可信化**：JUnit XML 稳定计数；物理 gate NOT_RUN（不永久 hardcode PENDING） | make validate |
| §19/§21 | **配额监控 + warm pool maintain 接入 worker 周期调度**（每 10/30 tick） | test_worker_runs_periodic_tasks |

## 3. 分项状态

### VERIFIED PASS
226 tests 全绿；lint/type/migration/build/smoke/release 全链路；migration 10 级链。

### PHYSICAL_VALIDATION_PENDING / NOT_RUN（不假装 PASS）
GPU（G1–G4 脚本就绪）· K8s（pytest -m k8s_integration 正确 skip）· Streaming · Robot · Warm pool SLA。

### BLOCKED_EXTERNAL_DEPENDENCY
NGC 凭据（镜像 digest 回填）· PostgreSQL 生产验证/容器测试（无 docker daemon）· S3 凭据 · 物理机器人 · 真实 K8s 集群。

### TECH DEBT
BillingAccount 重构（§17，当前 user/org 双 FK 聚合视角）· CreditHold 预授权（§18）· edge agent 独立包（§25）· SQLite FK 约束（§30，batch migration 待做）· lockfile/SBOM。

## 4. 结论

v0.3.1 Security & Integrity Hotfix 软件面完成：三个最高严重度问题
（checksum bypass / EdgeAgent 租户 / warm pool 资源泄漏）已修复并由
regression tests 证明；billing wiring、readiness、durable ops、worker
fencing、K8s node truth、版本/验证可信化、manifest 修复全部落地。
剩余工作依赖真实硬件/凭据（GPU/K8s/机器人/S3/PostgreSQL 容器）。

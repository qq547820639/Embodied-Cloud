# CURRENT_STATE — EmbodiedCloud

> 版本：**0.3.0**（Acceptance Hardening，2026-08-12）。
> 本文件只记录本次真实验证结果；验证数据来源：`docs/VALIDATION.json`（`make validate` 自动生成，CI 校验 freshness）。

## 1. 本次执行记录（实测）

| Gate | 命令 | 结果 |
|---|---|---|
| Test | `make test` | **PASS（182 passed + 1 skipped[k8s_integration]）** |
| Lint | `make lint` | PASS（ruff 0 errors） |
| Type | `make typecheck` | PASS（39 files） |
| Migration | `alembic upgrade head`（empty→head 8 级链） | PASS |
| Build | `make build` | PASS（embodiedcloud-0.3.0） |
| Release | `bash scripts/release.sh 0.3.0` | PASS（lint/type/test/build + **archive 清洁验证** + checksums + VALIDATION_STATUS） |
| Smoke | `make smoke` | PASS |
| OpenAPI | `make api-docs` + CI freshness | PASS（0.3.0） |
| 版本一致性 | pyproject/app/Makefile/CHANGELOG/OpenAPI | 统一 0.3.0 |

## 2. 本轮修复/新增（v0.3.0 Acceptance Hardening）

| # | 内容 | 证明 |
|---|---|---|
| §2 | K8s offline 隔离：model_factory 注入（tests/k8s_fakes.py），offline 测试零 SDK 依赖（blocked-import 证明测试） | test_k8s_provider.py |
| §3 | K8s inventory 真实路径：node nvidia.com/gpu capacity → GpuHost/Gpu；Workspace→Scheduler→Reservation→Provider 全链成立 | test_k8s_inventory.py（4 用例） |
| §4 | K8s integration harness 真实全流程（**无 NotImplementedError**；无集群 SKIP 标 PENDING） | test_k8s_integration.py |
| §5 | **Operation lease/fencing（P0）**：lease_owner/fencing_token/heartbeat_at；原子 claim（rowcount）；执行期心跳续期；finish 必须 fencing 验证（LeaseLostError）；SQL 层比较 | test_worker_fencing.py（4 用例） |
| §6 | Warm pool 真实 launch 路径：POST /api/workspaces → BillingPolicy → claim → 命中直接返回 | test_warmpool_claim.py |
| §7 | **Warm pool credential rotation**：provider.rotate_credentials（Mock True / Docker False 明确不支持 / K8s patch env 滚动重启）；rotation 失败不得交付（DRAINING + fallback） | test_warmpool_claim.py（rotation 失败/先于交付） |
| §8 | Warm pool 指标 COUNT(*) 按 template×state 真实计数 | test_pool_metrics_uses_real_counts |
| §9 | **ArtifactStore 集成**：DeploymentService 走 store 协议（create→put / verify→get）；migration object_key/content_type/store_name | test_deployment_service_uses_artifact_store |
| §10 | **Edge 上报 checksum**：POST report-checksum {actual_sha256}；server 比较 → VERIFIED/FAILED；防绕过（未下载拒绝）/防 replay（FAILED 不复活） | test_edge_checksum.py（7 用例） |
| §11 | **Template.current_version_id** 确定性指针（不用 created_at 猜 latest）；Artifact/Deployment 版本来自 Workspace.template_version_id | test_template_versions.py（3 新用例） |
| §12 | **Billing 预授权**（minimum_launch_minutes × 60 credits，enforce 开关）+ **active-runtime quota monitor**（投影余额透支 → 优雅停止，幂等） | test_billing_policy.py（4 新用例） |
| §13 | **凭据生产安全**：EMBODIEDCLOUD_WORKSPACE_CREDENTIAL_KEY；provider≠mock 未显式配置拒绝启动；enc: 密文解密失败 fail closed（不返回密文当密码） | test_workspace_credential.py（2 新用例） |
| §14 | scripts/validate_release.py 自动生成 docs/VALIDATION.json/.md；CI freshness 门禁 | make validate |
| §15 | 版本统一 0.3.0（pyproject/app/Makefile/CHANGELOG/OpenAPI） | — |
| §16 | release archive 清洁验证（无 __pycache__/pyc/cache/test-db/.env/.venv/.workbuddy） | scripts/release.sh 2.5 步骤 |

## 3. 分项状态

### VERIFIED PASS（自动测试证明）
182 tests 全绿；lint/type/migration/build/smoke/release 全链路通过；
migration 链：572b9ffa→a359de4e→eacad364→d19abc03→bf8d0efb→9a56191f→1cfd5543→93cc4735→head。

### PHYSICAL_VALIDATION_PENDING（软件完成，需真实硬件；不假装 PASS）
- GPU（G1–G4 脚本就绪）· K8s（pytest -m k8s_integration 正确 skip）· Streaming · Robot · Warm pool SLA

### BLOCKED_EXTERNAL_DEPENDENCY
NGC 凭据（镜像 digest 回填）· PostgreSQL 生产验证 · S3 凭据（ArtifactStore 生产后端）· lockfile/SBOM（P2）

## 4. 结论

v0.3.0 Acceptance Hardening 软件面全部完成：lease/fencing、K8s 真实路径、
warm pool 真实 launch + 凭据轮换、ArtifactStore 集成、edge 上报 checksum、
版本指针、billing 预授权 + 配额监控、凭据 fail-closed。全部由自动测试证明，
物理 gate 如实标记 PENDING。

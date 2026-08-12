# EmbodiedCloud v0.3.0 Acceptance Hardening — 交付总结

> 日期：2026-08-12 · 分支：main · 版本：0.3.0（软件面完成）
> 数据来源：docs/VALIDATION.json（`make validate` 自动生成，CI freshness 门禁）

## 交付概览

- **TL;DR**：v0.3.0 全部软件 acceptance gate 通过（182 passed + 1 skipped），
  发布产物 embodiedcloud-0.3.0 已生成并通过 archive 清洁验证。
- **验证**：`make check` 全绿（lint/type/test 182+1）· migration 8 级链 ·
  build · smoke（SMOKE_OK）· release 全流程 · OpenAPI/VALIDATION freshness
- **提交**：本轮 16 个 commit，工作树 CLEAN

## VERIFIED PASS（自动测试证明）

| § | 内容 |
|---|---|
| §2 | K8s offline 隔离：model_factory 注入，offline 测试零 Kubernetes SDK 依赖（blocked-import 证明） |
| §3 | K8s inventory 真实路径：node nvidia.com/gpu capacity → GpuHost/Gpu（capacity reservation，device 分配归 Device Plugin） |
| §4 | K8s integration harness 真实全流程（无 NotImplementedError；无集群 SKIP） |
| §5 | **Operation lease/fencing（P0）**：lease_owner/fencing_token/heartbeat_at；原子 claim；执行期心跳续期；finish 必须 fencing（LeaseLostError）；SQL 层比较 |
| §6 | Warm pool 真实 launch 路径（POST /api/workspaces → BillingPolicy → claim） |
| §7 | **Warm pool credential rotation**：三 provider 实现；rotation 失败不得交付（DRAINING + fallback） |
| §8 | Warm pool 指标 COUNT(*) 真实计数 |
| §9 | **ArtifactStore 集成**：DeploymentService 走 store 协议（object_key/content_type/store_name） |
| §10 | **Edge 上报 checksum**：report-checksum 协议，server 比较；防绕过/防 replay |
| §11 | **Template.current_version_id** 确定性指针；Artifact/Deployment 版本真相 |
| §12 | **Billing 预授权** + active-runtime quota monitor（透支优雅停止，幂等） |
| §13 | **凭据生产安全**：生产 provider 必须显式密钥；enc: 解密失败 fail closed |
| §14 | validate_release.py → VALIDATION.json/.md（CI freshness） |
| §15 | 版本统一 0.3.0（单一来源） |
| §16 | release archive 清洁验证（实测通过） |

## PHYSICAL_VALIDATION_PENDING（不假装 PASS）

GPU（G1–G4 脚本就绪）· K8s（pytest -m k8s_integration 正确 skip）· Streaming · Robot · Warm pool SLA

## BLOCKED_EXTERNAL_DEPENDENCY

NGC 凭据（镜像 digest 回填）· PostgreSQL 生产验证 · S3 凭据 · lockfile/SBOM（P2）

## NEXT PHYSICAL ACTIONS

1. GPU 主机：`make gpu-preflight && make gpu-test`
2. K8s 集群：`EMBODIEDCLOUD_K8S_TEST=1 pytest -m k8s_integration`
3. 生产：`EMBODIEDCLOUD_BILLING_ENFORCE_PREAUTHORIZATION=true` + `EMBODIEDCLOUD_WORKSPACE_CREDENTIAL_KEY=<强密钥>` + PostgreSQL

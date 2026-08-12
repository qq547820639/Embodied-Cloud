# EmbodiedCloud v0.3.1 Security & Integrity Hotfix — 交付总结

> 日期：2026-08-12 · 版本 0.3.0（软件面 v0.3.1 完成）· 19 commits 待推送
> 验证数据：docs/VALIDATION.json（make validate，JUnit 稳定计数）

## 交付概览
- **TL;DR**：三个最高严重度问题（Deployment checksum bypass / EdgeAgent 租户越权 / warm pool 凭据轮换失败资源泄漏）全部修复并由 regression tests 证明；P0 工程加固（readiness、durable stop/destroy、worker fencing、K8s node truth、DB 级 operation 唯一性）完成。
- **测试**：226 passed + 1 skipped（k8s_integration）· lint/mypy/compileall/migration(10 级链)/build/smoke/release 全 PASS
- **新增测试文件**：test_deployment_bypass / test_edge_ownership / test_runtime_readiness / test_durable_ops / test_k8s_node_truth / test_version_consistency

## 修复矩阵（VERIFIED PASS）
| 严重度 | 问题 | 修复 |
|---|---|---|
| P0 | checksum bypass（download 自动 VERIFIED） | 删除无校验 verify()；唯一路径 = edge 上报 → server 比较 |
| P0 | EdgeAgent 匿名注册 + 无租户隔离 | owner 绑定 + tenant scope + 部署派发校验 |
| P0 | warm pool rotation 失败泄漏 runtime/GPU | 完整补偿链 + provider 能力标志（Docker 禁用 warm pool） |
| P0 | billing settings 未接线 | deps 真实注入 |
| P0 | RUNNING 不含 readiness | wait_ready 契约（Docker TCP/HTTP/healthcheck exec；K8s Pod Ready/endpoints） |
| P0 | stop/destroy 非 durable | enqueue operation + fast-path tick |
| P0 | active operation 无 DB 级唯一 | 部分唯一索引 |
| P0 | worker 外部副作用无 fencing | reservation 注入 operation/fencing → labels；provision adopt 幂等 |
| P0 | K8s reservation 无 node 真相 | node_name 明确字段 + nodeSelector + reconcile mismatch → FAILED |

## 工程流程
- §14 Pod labels ↔ NetworkPolicy selector 真实命中（假安全配置修复）
- §15 版本单一来源（pyproject；UI 动态；manifest 0.3.0）
- §16 VALIDATION JUnit 稳定计数 + 物理 gate NOT_RUN
- §19/§21 quota monitor + warm pool maintain 接入 worker 周期调度

## PHYSICAL_VALIDATION_PENDING / NOT_RUN
GPU · K8s · Streaming · Robot（无真实硬件，不假装 PASS）

## BLOCKED_EXTERNAL_DEPENDENCY
- PostgreSQL 容器测试（需 docker daemon）：`docker run postgres:16` + pytest postgres 套件
- S3 凭据：S3CompatibleArtifactStore 生产验证
- NGC 凭据：镜像 digest 回填
- 物理机器人 / 真实 K8s GPU 集群

## NEXT
1. `git push`（19 commits）
2. GPU 主机：make gpu-preflight && make gpu-test
3. K8s：EMBODIEDCLOUD_K8S_TEST=1 pytest -m k8s_integration
4. PostgreSQL 容器测试套件（§34）

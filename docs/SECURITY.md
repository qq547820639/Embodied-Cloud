# SECURITY — EmbodiedCloud

> 版本：0.2.0（2026-08-12）。先读本文再改任何安全相关代码。

## 1. 安全原则

Reliability > Reproducibility > **Security** > Observability > DX > Performance > Features

## 2. 威胁模型

| # | 威胁 | 缓解 |
|---|---|---|
| T1 | 越权访问他人 Workspace | 所有 Workspace 查询强制 owner/org 校验；403/404 |
| T2 | 会话/凭据泄漏 | password 仅存哈希（PBKDF2-SHA256 600k iter）；session token 随机；不入日志 |
| T3 | Workspace 逃逸到 host | 容器无 docker.sock、无 host FS、非 root 用户、resource limits、no privileged |
| T4 | 计费作弊/重复扣款 | 不可变 ledger + idempotency_key 唯一约束 + 审计 |
| T5 | secret 泄漏（代码/日志/镜像） | CI secret 扫描；日志脱敏中间件；.env 不入 git |
| T6 | 供应链（latest 漂移） | 镜像/依赖全部 version pinned；CI 锁定依赖 |
| T7 | 调度资源抢占 | GPU 原子分配 + 唯一约束；UNHEALTHY 不调度 |
| T8 | DoS（端口耗尽） | 端口 allocator 校验、workspace 配额 |

## 3. 硬性禁令

- 禁止 hard-code password/token/key；禁止 commit secret。
- 禁止向普通 Workspace 暴露 Docker socket。
- 禁止让 Workspace 获得 host root / host filesystem / cluster-admin 凭据。
- 禁止把 token/password/access secret 写入日志（含结构化日志字段）。
- Workspace 镜像/依赖禁止引用 mutable `latest`。

## 4. 认证与会话

- `POST /api/auth/register`、`/api/auth/login`、`/api/auth/logout`。
- 密码哈希：`app/security.py` PBKDF2-SHA256（600,000 iter，per-user salt）。
- Bearer token 存 `user_sessions`（哈希存储），`Authorization: Bearer <token>`。
- OIDC-ready：auth 依赖抽象为 `AuthProvider`，未来接 OIDC 不改业务层。

## 5. 授权与隔离

- 角色：user / admin（预留 org_admin / instructor / student）。
- Workspace / Course / Deployment 查询必须校验 `owner_id == current_user.id` 或 admin。
- 隔离测试：`tests/test_isolation.py` 必须保持全绿。

## 6. 容器安全基线（Docker/K8s Provider）

- 非 root 用户运行；`--network host` 仅限单机可信模式（ADR 0003 记录）。
- resource limits（CPU/mem/pids）；K8s 模式 PVC + fsGroup 替代 0o777。
- GPU 只挂 `--gpus device=<index>`，不挂 `--gpus all` 之外的 host 设备。

## 7. 审计

- `credit_ledger` 即资金审计日志（谁、何时、什么类型、金额、幂等键）。
- 结构化日志保留 request_id / user_id / workspace_id / template_id / host_id / gpu_id。

## 8. Secret 管理

- `.env` 不入库（.gitignore）；示例为 `.env.example`。
- 生产 secret 建议外部 secret manager（K8s Secret/云 KMS）；本轮不内置 vault。

## 9. 发布安全

- release 前：lint + type + test + build + migration up/down + smoke。
- 镜像 tag 带版本（`0.2.0`），不推 latest。
- checksum 随 release artifact 发布。

# 安全边界

## v0.1 可接受范围

- Mock 模式：演示/本地产品验证。
- Docker GPU 模式：**可信内网、单团队**。
- code-server：每 Workspace 随机密码，Dashboard 通过 `/access` 取回。
- NVIDIA WebRTC：不裸露公网。
- 容器：不使用 `--privileged`；只挂用户项目目录与指定 GPU。

## 明确禁止

- 不把 v0.1 直接作为公网多租户 SaaS。
- 不把 `/var/run/docker.sock` 挂给公网控制面。
- 不对 49100/TCP、47998/UDP 做 `0.0.0.0/0` 开放。
- 不把数据库里的 workspace password 当长期身份凭据。

## 生产必须完成

- OIDC/企业 SSO；Workspace URL 使用短期签名 token。
- 所有 HTTP 走 TLS。
- 控制面不挂宿主 `/var/run/docker.sock`。
- Workspace 容器非 privileged；K8s 使用最小权限 ServiceAccount。
- 用户项目独立 PVC；对象存储按租户前缀和最小权限。
- Streaming gateway / firewall 限制到当前用户来源或认证代理。
- 审计：create/start/stop/delete、镜像、模板、GPU、账单事件。
- 密钥进入 secret manager，不进入镜像和 Git。

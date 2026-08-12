# ADR 0003: Docker 单机可信模式与 K8s 隔离策略

状态：Accepted（2026-08-12）

## 背景
单机 Docker GPU 模式需要共享宿主端口与 GPU；生产需要强隔离。

## 决策
1. **单机 Docker（v0.1–v0.2）**：`--network host` + 绑定卷 0o777 为**明确信任的单机模式**，
   仅限可信运维网络；文档与 SECURITY.md 明示限制。
2. 容器安全底线（所有模式）：非 root、无 docker.sock、无 host FS、resource limits、
   只挂 `--gpus device=<index>`。
3. **生产（K8s）**：以 PVC + fsGroup 替代 0o777，默认 network isolation（非 hostNetwork），
   Ingress/TLS 网关终结公网流量。

## 后果
- K8sWorkspaceProvider 不得复制 Docker 的 hostNetwork 假设；每个 provider 独立实现隔离策略。

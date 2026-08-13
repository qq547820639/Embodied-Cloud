# Kubernetes 生产迁移设计

当前仓库交付的是单机 Docker MVP；Kubernetes 是下一阶段替换 Provider，不重写 API/UI。

## 目标映射

- `Workspace` -> Pod + PVC + Service
- GPU -> `nvidia.com/gpu: 1`
- Template -> immutable image digest + command + resource profile
- IDE -> per-workspace Service + authenticated Ingress
- WebRTC -> 独立 streaming session gateway / host networking policy

## 集群基础

1. Kubernetes + containerd
2. NVIDIA GPU Operator
3. GPU 节点池标签/污点
4. PostgreSQL（替代 SQLite）
5. S3/OSS（实验结果）
6. Prometheus + DCGM Exporter
7. Ingress + OIDC

## 里程碑

- K1：Kubernetes Provider（Pod create/watch/delete）
- K2：PVC 与模板初始化
- K3：GPU 预约/配额
- K4：IDE Ingress 动态路由
- K5：Streaming session gateway
- K6：空闲/lease/max-runtime、计费、审计

## 重要说明

`deploy/kubernetes/control-plane.yaml` 只是**控制面骨架**，当前使用 1 replica + SQLite 便于验证镜像，不是生产数据库架构。生产前先切 PostgreSQL，再扩 replicas。

生产就绪清单见 `deploy/kubernetes/control-plane-production.yaml`：PostgreSQL（Secret 注入）+ `alembic upgrade head` 迁移 initContainer + `AUTO_CREATE_TABLES=false` + workspace_root PVC 持久卷。

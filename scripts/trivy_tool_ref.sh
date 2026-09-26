# 镜像清单／漏洞扫描共用的 trivy 工具引用——**只在这里写一份**。
# 两处各存一份默认值是这仓库已经吃过的那类缺陷（同一个事实两个来源，改一处忘另一处），
# 而这里的默认值还必须被 tests/test_supply_chain.py 的配方层判据逐字核到文档里。

# 为什么钉 public.ecr.aws：本机三条通道实测（2026-09-26，读数记在 SUPPLY_CHAIN §5/§8）——
# docker.io 走守护进程配置的镜像站时 TLS 握手超时、ghcr.io 拨号 i/o timeout，
# 只有 ECR Public 通。钉 digest 在这种前提下不是洁癖而是唯一把关：字节来自第三方镜像，
# 只有内容摘要能把"拿到的东西"和"想要的东西"对上。
# 方向也单独核过：重算 index body 的 sha256 得到同一个值，子清单 amd64/arm64 各在。
TRIVY_IMAGE="${TRIVY_IMAGE:-public.ecr.aws/aquasecurity/trivy:0.74.0@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969}"

# 漏洞库的通道。trivy 0.74.0 的默认优先级（`--help` 原文）是
#   mirror.gcr.io/aquasec/trivy-db:2 → ghcr.io/aquasecurity/trivy-db:2
# 这两条本机实测**全不通**（前者 connection refused、后者拨号 i/o timeout），所以 OS 层扫描
# 必须显式指定一条到得了的库通道；`public.ecr.aws/aquasecurity/trivy-db` 与工具镜像同在
# Aqua 的官方命名空间下（本机实测匿名 manifest GET 返回 200，manifest v2，751 B）。
# 注意它不在 trivy 自己的默认列表里：上游哪天挪走这里就要红，届时要重新核通道而不是改判据。
TRIVY_DB_REPOSITORY="${TRIVY_DB_REPOSITORY:-public.ecr.aws/aquasecurity/trivy-db:2}"

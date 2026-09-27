# VALIDATION — EmbodiedCloud

> 版本：0.7.0（自动生成，勿手改）

## 总览：**PASS_WITH_PHYSICAL_PENDING**

| Gate | 状态 | 明细 |
|---|---|---|
| Test collected | PASS | 559 |
| Test run | PASS | passed=557 skipped=2 failed=0 |
| lint | PASS | |
| typecheck | PASS | |
| migration | PASS | |
| build | PASS | |
| Integration browser | PASS | 11/11 用例在真实后端上执行 |
| Integration docker | PARTIAL | 执行 25/26，跳过 1（哨兵 DOCKER_VALIDATION_PENDING 命中 1） |
| Integration k8s | PENDING | 整档 1 用例未执行，原因：需要真实 Kubernetes 集群 + NVIDIA Device Plugin（export EMBODIEDCLOUD_K8S_TEST=1 且配置 kubeconfig 后运行）（哨兵 K8S_PHYSICAL_VALIDATION_PENDING） |
| Integration k8s_control_plane | PASS | 7/7 用例在真实后端上执行 |
| Integration object_store | PASS | 20/20 用例在真实后端上执行 |
| Integration postgres | PASS | 21/21 用例在真实后端上执行 |
| Physical gpu | NOT_RUN | 需要真实硬件/凭据（NVIDIA GPU、Isaac Sim 流媒体面、物理机器人），本机不可执行 |
| Physical streaming | NOT_RUN | 需要真实硬件/凭据（NVIDIA GPU、Isaac Sim 流媒体面、物理机器人），本机不可执行 |
| Physical robot | NOT_RUN | 需要真实硬件/凭据（NVIDIA GPU、Isaac Sim 流媒体面、物理机器人），本机不可执行 |

> 由 `python scripts/validate_release.py` 生成；CI 校验 freshness（重新生成无 diff）。
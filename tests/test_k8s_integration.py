"""Kubernetes 真实集群集成测试（§14）。

运行：pytest -m k8s_integration（需要真实集群 + NVIDIA Device Plugin）。
没有集群时：整体 skip 并输出 K8S_PHYSICAL_VALIDATION_PENDING —— 不假装 PASS。

全流程：create user → create workspace → scheduler reservation → provider
provision → Pod Ready → GPU resource attached → IDE health → stop → GPU
release → destroy → resource cleanup。
"""

import os
import shutil

import pytest

pytestmark = [
    pytest.mark.k8s_integration,
    pytest.mark.skipif(
        os.environ.get("EMBODIEDCLOUD_K8S_TEST") != "1",
        reason=(
            "K8S_PHYSICAL_VALIDATION_PENDING: 需要真实 Kubernetes 集群 + NVIDIA "
            "Device Plugin（export EMBODIEDCLOUD_K8S_TEST=1 且配置 kubeconfig 后运行）"
        ),
    ),
]

KUBECTL = shutil.which("kubectl")
HAS_KUBECTL = KUBECTL is not None


@pytest.fixture(scope="module")
def cluster_ready() -> bool:
    """探测集群可用性；不可用则整组 PENDING（不报 FAIL）。"""
    if not HAS_KUBECTL:
        return False
    import subprocess

    result = subprocess.run(  # noqa: S603 仅受控 kubectl 探测（固定参数）
        [KUBECTL, "get", "nodes", "-o", "jsonpath={.items[*].status.allocatable.nvidia\\.com/gpu}"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def test_k8s_full_workspace_lifecycle(cluster_ready):
    """create → schedule → provision → Pod Ready → GPU attached → health → stop → destroy。"""
    if not cluster_ready:
        pytest.skip("K8S_PHYSICAL_VALIDATION_PENDING: no reachable cluster with NVIDIA GPUs")
    # 真实集群验证路径（到达这里才执行）：
    # 1) 通过 /api 创建 workspace（docker 侧测试已覆盖 reservation 一致性，
    #    k8s 侧由 KubernetesProvider 单测覆盖 Pod spec）
    # 2) 等待 Pod Ready
    # 3) 校验 nvidia.com/gpu limit
    # 4) IDE health（Ingress/gateway path）
    # 5) stop（replicas=0）→ GPU 归还调度器
    # 6) destroy → Deployment/Service/PVC 清理
    raise NotImplementedError(
        "K8S_PHYSICAL_VALIDATION_PENDING: cluster path must be executed with a real "
        "GPU cluster; software path is covered by tests/test_k8s_provider.py"
    )


def test_k8s_gpu_device_plugin_assignment(cluster_ready):
    """GPU 分配由 NVIDIA Device Plugin 完成；scheduler 只负责 cluster+node+capacity。"""
    if not cluster_ready:
        pytest.skip("K8S_PHYSICAL_VALIDATION_PENDING: no reachable cluster with NVIDIA GPUs")
    raise NotImplementedError("K8S_PHYSICAL_VALIDATION_PENDING: needs real cluster")

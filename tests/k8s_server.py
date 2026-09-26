"""自持有的真 Kubernetes 控制面（kind 一次性集群，用完即删）。

为什么是 kind（2026-09-25 实测检索，六维对比见 docs/adr/0009）：

- **kind v0.33.0**（Apache-2.0，pushed 2026-09-24）：真 kubelet、真调度器、真
  readiness/endpoints 记账；单二进制 10 MB，发布物自带 `.sha256sum`。**选定**。
- **minikube**（Apache-2.0，pushed 2026-09-25）：能力相当，但默认多一层 VM driver。
- **envtest**（Apache-2.0）：只有 apiserver+etcd，**没有 kubelet** ⇒ Pod 永远到不了
  Running/Ready，要测试自己手写 status = 把结论当前提。

选 kind：本案要验的正是 `wait_ready` 的三段判断（Deployment available /
Pod Running+Ready / Endpoints 非空），这三段只有真 kubelet + 真 endpoints
控制器才能给出独立于我们代码的答案。envtest 需要测试自己把 pod 状态"写成"
Ready，那等于把结论当前提。

依赖：docker daemon、`kind` 二进制（`EMBODIEDCLOUD_KIND_BIN` 或 PATH）、
已缓存的 `kindest/node` 镜像（未缓存即整档跳过，避免测试期间拉起 GB 级下载）。
集群只写进临时 KUBECONFIG，**不合并进 ~/.kube/config**。
"""

import atexit
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid

DEFAULT_NODE_IMAGE = "kindest/node:v1.37.0"
GATE_SENTINEL = "K8S_CONTROL_PLANE_PENDING"
_READY_TIMEOUT_SECONDS = 300

_live: set[str] = set()


def node_image() -> str:
    return os.environ.get("EMBODIEDCLOUD_KIND_NODE_IMAGE", DEFAULT_NODE_IMAGE)


def kind_binary() -> str | None:
    return os.environ.get("EMBODIEDCLOUD_KIND_BIN") or shutil.which("kind")


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 受控常量参数，无用户输入
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def node_image_cached() -> bool:
    """节点镜像是否可用。

    `docker pull` 会建 tag，但 **kind 是按 digest 拉的**：实测本机拉完后 `docker images`
    只有 `kindest/node@sha256:a1ed56…`，没有 `:v1.37.0`，所以只查 tag 会把"已经缓存"
    读成"未缓存"（档位被无谓地跳过）。两种形态都认。
    """
    if _docker("image", "inspect", node_image(), timeout=30).returncode == 0:
        return True
    repo = node_image().split(":")[0]
    return bool(_docker("image", "ls", "-q", repo, timeout=30).stdout.strip())


def gate_reason() -> str | None:
    """可跑返回 None，否则返回原因（skip 文案与 release gate 分类共用）。"""
    if shutil.which("docker") is None:
        return "docker CLI 不可用"
    if _docker("version", timeout=20).returncode != 0:
        return "docker daemon 不可达"
    if kind_binary() is None:
        return "缺 kind 二进制（EMBODIEDCLOUD_KIND_BIN 或 PATH）"
    if not node_image_cached():
        return f"节点镜像 {node_image()} 未缓存（离线无法拉取）"
    try:
        import kubernetes  # noqa: F401
    except ImportError:
        return "缺 kubernetes SDK（项目主依赖）"
    return None


class KindCluster:
    """一个临时 kind 集群。KUBECONFIG 指向本次专用文件，退出即删集群。"""

    def __init__(self) -> None:
        self.name = f"ec-k8s-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.kubeconfig = os.path.join(tempfile.mkdtemp(prefix="ec-k8s-kube-"), "kubeconfig")

    def _kind(self, *args: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
        binary = kind_binary()
        assert binary is not None
        env = dict(os.environ, KUBECONFIG=self.kubeconfig)
        return subprocess.run(  # noqa: S603 受控 argv（集群名由 uuid 铸造）
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )

    def start(self) -> "KindCluster":
        proc = self._kind(
            "create", "cluster", "--name", self.name, "--image", node_image(), "--wait", "120s"
        )
        _live.add(self.name)
        # 先看 kind 自己的退出码：它失败时 kubeconfig 根本没写出来，若先去等就绪，
        # 报错只会是"Invalid kube-config file"，把真实原因（节点起不来/内存不足）吞掉。
        if proc.returncode != 0:
            self.stop()
            raise RuntimeError(f"kind create cluster 失败：{proc.stdout}{proc.stderr}")
        try:
            self._wait_api_ready()
        except Exception:
            self.stop()
            raise
        return self

    def _wait_api_ready(self) -> None:
        """就绪判据 = 用测试真正要走的那条路（KUBECONFIG + SDK）列一次 namespace。"""
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config

        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        last = ""
        while time.monotonic() < deadline:
            try:
                k8s_config.load_kube_config(config_file=self.kubeconfig)
                k8s_client.CoreV1Api().list_namespace()
                return
            except Exception as exc:
                last = str(exc).replace("\n", " ")[:200]
            time.sleep(2)
        raise RuntimeError(f"kind 集群 {self.name} 在 {_READY_TIMEOUT_SECONDS}s 内未就绪：{last}")

    def stop(self) -> None:
        if self.name in _live:
            self._kind("delete", "cluster", "--name", self.name, timeout=300)
            _live.discard(self.name)
        kube_dir = os.path.dirname(self.kubeconfig)
        if os.path.isdir(kube_dir):
            shutil.rmtree(kube_dir, ignore_errors=True)

    def node_container(self) -> str:
        return f"{self.name}-control-plane"

    def node_images(self) -> list[str]:
        """节点 containerd 里已有的镜像 tag（用来挑一个无需外网拉取的镜像）。"""
        out = subprocess.run(  # noqa: S603 受控 argv
            ["docker", "exec", self.node_container(), "crictl", "images", "-o", "json"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        ).stdout
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            return []
        tags: list[str] = []
        for image in payload.get("images", []):
            tags.extend(str(tag) for tag in (image.get("repoTags") or []))
        return tags

    def pause_image(self) -> str:
        """节点自带的 pause 镜像：主进程一直挂着（Running+Ready），不需要再拉外网镜像。"""
        pauses = sorted(tag for tag in self.node_images() if "/pause:" in tag or tag.endswith("pause"))
        if not pauses:
            raise RuntimeError(f"节点上没有 pause 镜像（实测只有：{sorted(self.node_images())}）")
        return pauses[0]


def _cleanup_leaked() -> None:
    """pytest 崩溃/被 kill 的兜底：删掉本进程建过名的集群（按前缀匹配，绝不动别人的）。"""
    for name in list(_live):
        binary = kind_binary()
        if binary is None:
            continue
        subprocess.run(  # noqa: S603 受控 argv
            [binary, "delete", "cluster", "--name", name],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    _live.clear()


atexit.register(_cleanup_leaked)


def poll_until(read, settled, *, what: str, timeout_seconds: float = 90.0, interval: float = 0.5):
    """等集群把某个事实做出来；到点仍不成立就报"前提未达成"，而不是抛 IndexError。

    为什么需要它：`wait_ready` 的判定窗口（负向对照只给 6s）与
    "Deployment→ReplicaSet→Pod 被控制器建出来 / 条件被写满"是两件事。宿主繁忙时
    后者可以晚于前者，于是"直接取 pods[0]"会崩在 IndexError 上——把一条与产品无关的
    夹具竞态报成用例红。本轮真实踩过一次（517 passed / 1 failed，红的正是负向对照里
    取 Pod 那一行）。
    """
    deadline = time.monotonic() + timeout_seconds
    last = None
    while True:
        last = read()
        done = settled(last)
        if done is not None:
            return done
        if time.monotonic() >= deadline:
            raise AssertionError(f"前提未达成：{timeout_seconds}s 内 {what}；最后一次读数 {last!r}")
        time.sleep(interval)


def await_pod(list_pods, *, what: str = "Pod", timeout_seconds: float = 90.0):
    """等到至少有一个 Pod 出现，返回第一个。"""
    return poll_until(
        list_pods,
        lambda pods: pods[0] if pods else None,
        what=f"{what} 尚未被集群建出来",
        timeout_seconds=timeout_seconds,
    )


def await_condition_reason(list_pods, reason: str, *, status: str = "False", timeout_seconds: float = 90.0):
    """等到某个 Pod 的 conditions 里出现指定判词（如调度器的 Unschedulable），返回该原因列表。"""

    def read():
        pods = list_pods()
        if not pods:
            return []
        return [c.reason for c in (pods[0].status.conditions or []) if c.status == status]

    return poll_until(
        read,
        lambda reasons: reasons if reason in reasons else None,
        what=f"没有任何 Pod 给出 {reason} 判词",
        timeout_seconds=timeout_seconds,
    )

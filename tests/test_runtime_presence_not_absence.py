"""runtime「还在但没跑起来」不得被读成「不在了」。

判据只有一道闸：`_release_admitted(workspace, command_succeeded=False)`
（app/services/orchestrator.py:477-509）在清理命令**失败**那一档只认 `MISSING`。
所以 provider 只要把 present-but-not-running 说成缺席，卡就会从还活着的 runtime 底下放走
（「一卡双跑」）；反过来把真缺席说成别的，卡就被永久钉死。两个方向都要有判据，本模块两极都钉。

先更正一条登记错的前提（本轮由本机 daemon 实测）：原先登记的措辞是
「restarting/paused/Pending 被判成缺席」——**其中 paused/restarting 那两半是假的**。
docker 路径的 `inspect` 先读 `Running` 标志（providers/docker.py:479；HEAD 的同一格是 :474），
引擎对 paused/restarting 自述 `Running:true`，于是这两档今天已经是 ALIVE，无需修改。
真正承重的两半是：① K8s 的 Pending（下面 A/B 组），② docker 对 `created`/`removing`
的 fallthrough（HEAD 的 docker.py:478 那条兜底 `return MISSING`；下面 C 组）。

本机 2026-09-28 实测表（containers `ec-n79-*`：创建/暂停/重启中/退出后全部移除；
命令 `docker inspect --format '{{json .State}}'`。夹具只保留判决用到的四个键
Status/Running/Paused/Restarting ＋ ExitCode —— 真串还有 StartedAt/Health 等十余字段，
整抄进来会读成"我们判的是全部"）。

逐行读数（`Status` 原文形状 → Running/Paused/Restarting → 本轮之前的判决 → 本轮之后）：

- `{"Status":"created",…,"Running":false,…}` → false/false/false → **MISSING**（HEAD
  docker.py:478 的 fallthrough）→ UNKNOWN
- `{"Status":"paused","Running":true,"Paused":true,…}` → **true**/true/false → ALIVE（无缺陷）
  → ALIVE
- `{"Status":"restarting","Running":true,"Restarting":true,…}` → **true**/false/true → ALIVE
  （无缺陷）→ ALIVE
- `{"Status":"exited","Running":false,"ExitCode":3,…}` → false/false/false → MISSING → MISSING

`dead`/`removing` **未做到**强制出来（瞬态，要 kill 失败/removal 在途才看得见；这是没做到，
不是没找到）：按 moby `api/types/container/state.go` 的七个常量与
`api/types/container/container.go:76` 的字段注释原文 "Can be one of \"created\", \"running\",
\"paused\", \"restarting\", \"removing\", \"exited\", or \"dead\"" 推理，两档都归「引擎还持有
对象」⇒ UNKNOWN，并如实标为推理档。

K8s 侧的承重证据（本轮改的就是这一条）：`providers/k8s.py` 改前只调
`read_namespaced_deployment_status(...).status.available_replicas`，一次都没读 `spec.replicas`，
却靠 docstring 承诺「404/0 副本 → MISSING」——注释许了一个代码不做的区分。后果：
`spec.replicas=1` 而 pod Pending（拉镜像 / 建 sandbox / 等 device-plugin 分 GPU）读成 MISSING。
K8s 自己说 Pending 不是缺席，逐字引自 kubernetes/website
`content/en/docs/concepts/workloads/pods/pod-lifecycle.md` 的 Pod phase 表（本机 2026-09-28
由 raw.githubusercontent.com 取回，该文件第 114 行）：

    `Pending`   | The Pod has been accepted by the Kubernetes cluster, but one or more of the
    containers has not been set up and made ready to run. This includes time a Pod spends
    waiting to be scheduled as well as the time spent downloading container images over the
    network.

本产品自己的控制面用例天天造这一档：带 GPU 请求的 Pod 在 kind 集群上「必然
Pending（`Insufficient nvidia.com/gpu`）」（tests/test_k8s_control_plane.py:9），而 workspace
的 Deployment 确实请求 `nvidia.com/gpu`（providers/k8s.py:265）加 cpu/memory（:259-260）。

必须说出来的代价（不是顺手的小改）：把 Pending/created 改成 UNKNOWN 之后，
**失败那一档拒绝放卡**，而 durable STOP 打满 `OperationWorker.MAX_ATTEMPTS = 3`
（app/services/worker.py:78，判决在 :352，落笔在 :354-377）之后**进程内没有人再试**：
`reconcile_all` 没有 UNKNOWN 分支——那里只有一行注释
「# UNKNOWN：无真实 runtime 可判定，保守不动」（`grep -n "UNKNOWN：无真实 runtime"
app/services/orchestrator.py`；按行号钉会在别人的 orchestrator.py 提交上腐烂，故按原文钉，
本轮 base 2dd2ba2 上是 :716），而 `reconcile_all` 本身不是周期任务；
`OperationType.RECONCILE` 有消费者（app/services/orchestrator.py:163-164
→ `self.reconcile_all()`）却没有任何入队点（`grep -rn OperationType.RECONCILE app/ tests/
scripts/` 本机读数：除消费者那一行外零命中）。
⇒ 这一笔是拿「一卡双跑」换「一张卡可能被钉住」。换法是本仓既定立场（比较 `_fail`
里 STOPPING+protected 的两条理由，app/services/orchestrator.py:333-345：宁可钉住也不放给
活对象——`recover_stuck_gpu_allocations` 只保护非终态，写 FAILED 会被它把卡放掉），
但代价本身要登记为缺一个周期驱动者，不在这里顺手补。

另一个方向的承重前提也在这里补常驻判据：`stop()`（providers/k8s.py:321-332）缩到
`replicas: 0` 而**保留对象**，正常停止路径靠的正是随后读到的 MISSING —— 本轮之前
`tests/` 里没有任何常驻用例钉「0 副本 → MISSING」（grep 读数见本轮交付说明），
所以那条行为既承重又无保护；B(b) 与 B(e2e) 两支把它钉住。
"""

import ast
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest
from k8s_fakes import make_fake_models
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from test_k8s_provider import (
    DEPLOYMENT_NAME,
    FakeAppsV1Api,
    FakeClientModule,
    make_settings,
)

from app.config import Settings
from app.db import Base
from app.models import (
    Gpu,
    GpuAllocation,
    GpuStatus,
    Template,
    Workspace,
    WorkspaceStatus,
)
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers import docker as docker_module
from app.services.providers.base import RuntimeState
from app.services.providers.docker import DockerProvider
from app.services.providers.k8s import KubernetesProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from tests.dbfiles import db_url

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_DIR = REPO_ROOT / "app" / "services" / "providers"

ENGINE = create_engine(db_url("runtime-presence"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)

WORKSPACE_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


try:  # 404 要按真 SDK 的异常形状喂：provider 读的是 `exc.status`（providers/k8s.py:117-123）
    from kubernetes.client import ApiException as SdkApiException
except ImportError:  # pragma: no cover - 离线环境无 SDK；兜底形状只多一个 status 属性
    class SdkApiException(RuntimeError):  # type: ignore[no-redef]
        def __init__(self, status=None, reason=""):
            super().__init__(f"{status} {reason}")
            self.status = status
            self.reason = reason


# ---------------------------------------------------------------------------
# 假客户端：一个 Deployment 对象，两个读口子
# ---------------------------------------------------------------------------


class DeploymentObject:
    """集群侧那一个 Deployment 的可变事实：spec.replicas 与 status.available_replicas。

    两个字段分开写，是因为本轮的分歧恰恰在「status 说没有可用副本」与「spec 说我们没退役」
    之间 —— 假对象若只有一个字段，就证不了这个区分。
    """

    def __init__(self, *, replicas: int | None = 1, available_replicas: int | None = None):
        self.spec = SimpleNamespace(replicas=replicas)
        self.status = SimpleNamespace(available_replicas=available_replicas)
        self.scale_calls: list[dict] = []
        self.scale_attempts = 0
        self.fail_scale = False

    def scale(self, body: dict) -> None:
        """`patch_namespaced_deployment_scale` 的效果：spec 改口，status 由控制器异步跟上。

        这里刻意**不**跟着改 available_replicas：缩到 0 之后 apiserver 立刻回的就是
        `available_replicas=None/0`，而本轮的判决必须只看 spec 就能说退役了 ——
        让 status 跟着变会把"0 副本必须先于可用性判"那条顺序失去反证力。
        """
        self.scale_attempts += 1
        self.scale_calls.append(body)
        if self.fail_scale:
            raise RuntimeError("simulated: apiserver rejected the scale request")
        self.spec.replicas = int(body["spec"]["replicas"])


class PresenceAppsApi(FakeAppsV1Api):
    """沿用兄弟模块那份 fake AppsV1Api 的形状（第一个构造参数＝记录用的 calls 列表）。

    两个 read 口子都实现 —— 真 apiserver 上它们本来就是同一个对象的两个视图，
    而"改前臂"走的是 status-only 那一个。只实现新口子的假客户端会让改前那一档
    AttributeError ⇒ 两档同形 ⇒ 夹具失去反证力。
    """

    def __init__(self, calls: list, deployment: DeploymentObject, *, raises: Exception | None = None):
        self.calls = calls
        self.deployment = deployment
        self.raises = raises

    def _read(self, kind: str, name: str, namespace: str):
        self.calls.append((kind, namespace, name))
        if self.raises is not None:
            raise self.raises
        return self.deployment

    def read_namespaced_deployment(self, name, namespace, **kwargs):
        return self._read("read_deployment", name, namespace)

    def read_namespaced_deployment_status(self, name, namespace, **kwargs):
        # status-only 视图：真对象上它根本不带 spec，这正是改前读不到退役证据的原因。
        return SimpleNamespace(status=self._read("read_deployment_status", name, namespace).status)

    def patch_namespaced_deployment_scale(self, name, namespace, body, **kwargs):
        self.calls.append(("patch_scale", namespace, name, body))
        self.deployment.scale(body)


class PresenceClientModule(FakeClientModule):
    def __init__(self, deployment: DeploymentObject, *, raises: Exception | None = None):
        super().__init__()
        self.deployment = deployment
        self.raises = raises

    def AppsV1Api(self):
        return PresenceAppsApi(self.calls, self.deployment, raises=self.raises)


def presence_provider(
    deployment: DeploymentObject, *, raises: Exception | None = None
) -> tuple[KubernetesProvider, PresenceClientModule]:
    fake = PresenceClientModule(deployment, raises=raises)
    return (
        KubernetesProvider(make_settings(), _client=fake, model_factory=make_fake_models),
        fake,
    )


def _workspace() -> Workspace:
    ws = Workspace(
        id=WORKSPACE_ID,
        name="n",
        template_id="t",
        provider="k8s",
        status=WorkspaceStatus.RUNNING.value,
    )
    ws.container_name = DEPLOYMENT_NAME
    return ws


# ---------------------------------------------------------------------------
# A. k8s reconcile 的五档
# ---------------------------------------------------------------------------


def test_pending_with_replicas_wanted_is_not_absence():
    """(a) 承重档：spec.replicas=1 而 available_replicas=None —— pod Pending，不是缺席。

    改前这一格是 MISSING（`available_replicas` 一缺位就兜底），于是清理命令失败那一档的
    释放准入会把卡放回池子。
    """
    provider, _ = presence_provider(DeploymentObject(replicas=1, available_replicas=None))

    state = provider.reconcile(_workspace())

    assert state is not RuntimeState.MISSING, f"Pending 被判成缺席：{state}"
    assert state is RuntimeState.UNKNOWN, f"present-but-not-running 应当问得出、但不算活着：{state}"


def test_scaled_to_zero_replicas_is_the_retired_case():
    """(b) 承重前提：spec.replicas=0 且 available=None ⇒ MISSING。

    `stop()`（providers/k8s.py:321-332）缩容但**保留对象**，正常停止路径放卡靠的就是这一格，
    所以它既承重又在本轮之前没有任何常驻用例钉过（见模块 docstring）。
    """
    provider, _ = presence_provider(DeploymentObject(replicas=0, available_replicas=None))

    assert provider.reconcile(_workspace()) is RuntimeState.MISSING


def test_scaled_to_zero_beats_a_stale_available_replica():
    """(b') 顺序档：0 副本必须**先于**可用性判——缩容请求已发、旧副本还没退的那一档。

    若把可用性排在前面，这一格会读成 ALIVE，而 spec 已经说我们退役了：放卡会拖到
    控制器把 status 追上为止，而 `_release_admitted` 那一档等不了。
    """
    provider, _ = presence_provider(DeploymentObject(replicas=0, available_replicas=1))

    assert provider.reconcile(_workspace()) is RuntimeState.MISSING


def test_available_replicas_means_alive():
    """(c) 合规侧：available_replicas=2 ⇒ ALIVE（这一档改前也绿，是护栏不是反证）。"""
    provider, _ = presence_provider(DeploymentObject(replicas=2, available_replicas=2))

    assert provider.reconcile(_workspace()) is RuntimeState.ALIVE


def test_api_404_is_absence():
    """(d) 合规侧：404 是 API server 亲口说对象没了 ⇒ MISSING（destroy 之后靠它放卡）。"""
    provider, _ = presence_provider(DeploymentObject(), raises=SdkApiException(status=404, reason="Not Found"))

    assert provider.reconcile(_workspace()) is RuntimeState.MISSING


def test_any_other_api_failure_is_not_absence():
    """(e) 问不到不是不在了：非 404 异常 ⇒ UNKNOWN（ADR 0008 同一口径）。"""
    for exc in (
        SdkApiException(status=500, reason="Internal"),
        SdkApiException(status=403, reason="Forbidden"),
        ConnectionError("simulated: apiserver unreachable"),
    ):
        provider, _ = presence_provider(DeploymentObject(replicas=1, available_replicas=None), raises=exc)
        assert provider.reconcile(_workspace()) is RuntimeState.UNKNOWN, exc


# ---------------------------------------------------------------------------
# B. k8s 端到端：同一夹具的两极（不停卡 / 停过就放卡）
# ---------------------------------------------------------------------------


def _seed_k8s_pool(db) -> None:
    GpuScheduler(Factory).sync_host(
        db,
        host_id="k8s-node-1",
        name="gpu-node-01",
        address="10.0.0.8",
        provider="k8s",
        gpus=[GpuInfo(gpu_uuid="gpu-node-01:gpu-0", model="K8s GPU", memory_total=24576, index=0)],
    )
    db.add(
        Template(
            id="cartpole",
            slug="cartpole",
            name="cartpole",
            description="test",
            category="test",
            runtime="isaaclab",
            launch_command="echo ok",
            enabled=True,
            recommended_vram_gb=16,
            estimated_hourly_cost_cny=1.0,
        )
    )
    db.commit()


def _running_k8s_workspace(provider: KubernetesProvider) -> WorkspaceOrchestrator:
    """真分配一张卡（scheduler 权威）、容器名落库（stop 才有对象可叫）、运行段真实存在。"""
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-runtime-presence")  # noqa: S108 测试隔离目录
    )
    with Factory() as db:
        _seed_k8s_pool(db)
        ws = Workspace(
            id=WORKSPACE_ID,
            name="w",
            template_id="cartpole",
            provider="k8s",
            status=WorkspaceStatus.RUNNING.value,
            started_at=datetime.now(UTC),
        )
        ws.container_name = DEPLOYMENT_NAME
        db.add(ws)
        db.commit()
        gpu = GpuScheduler(Factory).allocate(db, WORKSPACE_ID, gpu_requirement_gb=8)
        ws.gpu_id = gpu.id
        db.commit()
    return orchestrator


def _card(db) -> dict:
    """卡到底还占着没有：分配行是权威，Gpu.status 是它对外的说法，两个一起读。"""
    return {
        "allocation": db.scalar(
            select(GpuAllocation).where(GpuAllocation.workspace_id == WORKSPACE_ID)
        )
        is not None,
        "gpu_status": db.scalar(select(Gpu.status)),
    }


def test_pending_pod_with_failing_stop_does_not_release_the_card():
    """两极之一（改前会红）：STOP 命令报错而 pod 还在 Pending ⇒ 不许放卡。

    命令报错不等于 runtime 不在了：`_release_admitted(command_succeeded=False)` 只认
    MISSING，而改前的 MISSING 是 `available_replicas` 缺位的兜底，不是引擎的证词。
    """
    deployment = DeploymentObject(replicas=1, available_replicas=None)
    deployment.fail_scale = True
    provider, _fake = presence_provider(deployment)
    orchestrator = _running_k8s_workspace(provider)

    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, WORKSPACE_ID))

    assert deployment.scale_attempts == 1, "STOP 命令这一前提必须真的失败一次，否则测的不是失败极"
    assert deployment.spec.replicas == 1, f"失败的 scale 不许把 spec 改成 0：{deployment.spec.replicas}"
    with Factory() as db:
        assert _card(db) == {"allocation": True, "gpu_status": GpuStatus.ALLOCATED.value}, (
            "pod 还在 Pending（spec.replicas=1、无 available 副本）就把卡放回了池子"
        )
        assert db.get(Workspace, WORKSPACE_ID).status == WorkspaceStatus.STOPPING.value


def test_the_same_workspace_releases_once_stop_scaled_it_to_zero():
    """两极之二（合规侧，改前也绿）：同一夹具，stop() 把副本缩到 0 之后必须放卡。

    这条钉的是"改了 MISSING 的来源之后，正常停止路径没被改坏"：准入读的是 spec.replicas==0，
    而不是"命令成功了"——所以 `command_succeeded=True` 那档对 UNKNOWN 仍放行、对 ALIVE 仍拒绝。
    """
    deployment = DeploymentObject(replicas=1, available_replicas=None)
    provider, _fake = presence_provider(deployment)
    orchestrator = _running_k8s_workspace(provider)

    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, WORKSPACE_ID))

    assert deployment.spec.replicas == 0, f"stop() 没把 Deployment 缩到 0：{deployment.scale_calls}"
    with Factory() as db:
        assert _card(db) == {"allocation": False, "gpu_status": GpuStatus.AVAILABLE.value}, (
            "provider 已经退役了 runtime（spec.replicas=0）却不放卡 ⇒ 卡被永久钉死"
        )
        assert db.get(Workspace, WORKSPACE_ID).status == WorkspaceStatus.STOPPED.value


# ---------------------------------------------------------------------------
# C. docker：把本机实测表编成判据（未来重映射必须翻红）
# ---------------------------------------------------------------------------

# (engine Status, Running, Paused, Restarting, ExitCode, 期望判决, 取证档)
PROBE_TABLE = [
    ("running", True, False, False, 0, RuntimeState.ALIVE, "实测"),
    # paused/restarting：引擎自述 Running=true ⇒ 在 :479 那一格就返回 ALIVE，本轮未改这一路径
    ("paused", True, True, False, 0, RuntimeState.ALIVE, "实测"),
    ("restarting", True, False, True, 0, RuntimeState.ALIVE, "实测"),
    # created：moby 的定义是 "created, but not (yet) started" —— 引擎还持有可直接 start 的对象
    ("created", False, False, False, 0, RuntimeState.UNKNOWN, "实测"),
    ("exited", False, False, False, 3, RuntimeState.MISSING, "实测"),
    # 下面两档未做到强制（瞬态），按 moby 的七常量枚举推理；判决与本轮实现同源于那两处原文
    ("removing", False, False, False, 0, RuntimeState.UNKNOWN, "推理(未做到强制)"),
    ("dead", False, False, False, 0, RuntimeState.MISSING, "推理(未做到强制)"),
]


class ScriptedDocker(DockerProvider):
    """`_run` 的返回值由夹具决定（同 tests/test_provider_absence_evidence.py 的形状）。

    不建/不起任何容器：本轮判的是"引擎已经答了话之后怎么判"，argv 由兄弟模块那批判据覆盖。
    """

    def __init__(self, settings, *, stdout: str):
        super().__init__(settings)
        self.stdout = stdout
        self.commands: list[list[str]] = []

    def _run(self, args, *, check=True):
        self.commands.append(list(args))
        return CompletedProcess(args=args, returncode=0, stdout=self.stdout, stderr="")


def _state_json(status: str, running: bool, paused: bool, restarting: bool, exit_code: int) -> str:
    return (
        f'{{"Status":"{status}","Running":{str(running).lower()},'
        f'"Paused":{str(paused).lower()},"Restarting":{str(restarting).lower()},'
        f'"ExitCode":{exit_code}}}'
    )


def _docker_provider(settings, status: str, running: bool, paused: bool, restarting: bool, code: int):
    return ScriptedDocker(
        settings, stdout=_state_json(status, running, paused, restarting, code)
    )


def _docker_workspace() -> Workspace:
    return Workspace(
        id=WORKSPACE_ID,
        name="n",
        template_id="t",
        provider="docker",
        status=WorkspaceStatus.RUNNING.value,
        container_name=f"ec-{WORKSPACE_ID[:12]}",
    )


@pytest.fixture
def with_docker(monkeypatch):
    """本轮判"问到了引擎之后怎么判"，不是有没有二进制（与兄弟模块同一档口径）。"""
    monkeypatch.setattr(docker_module.shutil, "which", lambda _name: "/usr/local/bin/docker")
    return Settings(eula_accepted=True)


@pytest.mark.parametrize(
    ("status", "running", "paused", "restarting", "exit_code", "expected", "grade"),
    PROBE_TABLE,
    ids=[row[0] for row in PROBE_TABLE],
)
def test_docker_status_split_matches_the_measured_probe_table(
    with_docker, status, running, paused, restarting, exit_code, expected, grade
):
    """本机 2026-09-28 实测表编成常驻判据：任何把这七档重新折叠成"非 running 即缺席"的改动都要翻红。"""
    provider = _docker_provider(with_docker, status, running, paused, restarting, exit_code)

    got = provider.reconcile(_docker_workspace())

    assert got is expected, f"Status={status}（{grade}档，Running={running}）应判 {expected}，读到 {got}"
    if expected is not RuntimeState.MISSING:
        assert provider.reconcile(_docker_workspace()) is not RuntimeState.MISSING, (
            "释放准入只认 MISSING，这一档一旦被说成缺席就会把卡放给还活着的 runtime"
        )


def test_created_container_does_not_admit_the_release(with_docker):
    """跨文件后果（created 档）：引擎还持有可 start 的对象 ⇒ `_release_admitted` 拒绝放卡。"""
    provider = _docker_provider(with_docker, "created", False, False, False, 0)
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-runtime-presence-docker")  # noqa: S108 测试隔离目录
    )

    assert provider.reconcile(_docker_workspace()) is RuntimeState.UNKNOWN
    assert orchestrator._release_admitted(_docker_workspace(), command_succeeded=False) is False


def test_statuses_outside_the_engine_enum_are_not_absence(with_docker):
    """白名单性质（形状对照，非实测）：枚举之外的态不许默认算缺席。

    MISSING 那一档是白名单 {exited, dead}，所以 moby 将来加第八个态时它落进 UNKNOWN 而不是
    缺席——这条判据钉的是实现形状，不是某个具体字符串。
    """
    provider = _docker_provider(with_docker, "quiescing", False, False, False, 0)

    assert provider.reconcile(_docker_workspace()) is RuntimeState.UNKNOWN


def test_paused_without_the_running_flag_is_still_not_absence(with_docker):
    """顺序/ precedence 形状对照（非实测）：`paused` 若哪天不再带 Running=true，也不许算缺席。

    本机实测的 paused 带 Running=true，所以 ALIVE 是"先读 Running"的结果而不是"读了 paused"
    的结果。这一格把两个口子分开钉：判决不能退化成按 Status 字符串直接折 MISSING。
    """
    provider = _docker_provider(with_docker, "paused", False, True, False, 0)

    assert provider.reconcile(_docker_workspace()) is RuntimeState.UNKNOWN


# ---------------------------------------------------------------------------
# D. 结构判据：reconcile 必须读 spec.replicas（注释承诺 ≠ 代码实现）
# ---------------------------------------------------------------------------


def _attr_chain(node: ast.Attribute) -> list[str]:
    """属性链的末级在前：`dep.spec.replicas` → ["replicas", "spec"]。"""
    names: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        names.append(cur.attr)
        cur = cur.value
    return names


def reads_spec_replicas(node: ast.AST) -> bool:
    """node 里有没有读 `.spec…replicas`（属性链末级是 replicas、链上还带着 spec）。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "replicas" and "spec" in _attr_chain(sub):
            return True
    return False


def _covered(node: ast.AST) -> set[int]:
    """该节点覆盖到的源码行（用它判"这条 return 在不在某个分支里"）。"""
    out: set[int] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.stmt):
            out.update(range(sub.lineno, (sub.end_lineno or sub.lineno) + 1))
    return out


def _reconcile_def(source: str) -> ast.FunctionDef:
    for cls in ast.walk(ast.parse(source)):
        if isinstance(cls, ast.ClassDef) and cls.name == "KubernetesProvider":
            for item in cls.body:
                if isinstance(item, ast.FunctionDef) and item.name == "reconcile":
                    return item
    raise AssertionError("没找到 KubernetesProvider.reconcile —— 尺子选错了靶，别把看不见读成合规")


def reconcile_absence_offenders(source: str) -> list[str]:
    """`KubernetesProvider.reconcile` 里"没读 spec.replicas 就断定 MISSING"的落点，带 clause 名。

    两条独立 clause，各有自己的开火对照（合起来才是"注释承诺 ≠ 代码实现"看得见）：

    - **C1 判决落点**：函数体内每条 `return RuntimeState.MISSING`，若它既不在"比对 404"的
      except 处理器里、也不在任何 test 读了 `.spec…replicas` 的 if 里 ⇒ 开火。
      改前 :549 那条兜底 return 正是这一格：它只由 `available_replicas` 的**缺位**推出缺席。
    - **C2 承诺与实现分叉**：docstring 声称按副本数判（出现"副本"或"replicas"），
      而函数体一次都没读 `.spec…replicas` ⇒ 开火（行号给函数）。
      C1 全绿而 C2 开火的形状是真实存在的：比如把兜底 MISSING 换成 UNKNOWN、
      却只在 docstring 留着"0 副本 → MISSING"这句话——那行注释仍然在向读者承诺一个
      代码不做的区分，而它正是本轮之前那种"docstring 有、代码没有"的原始形状。
    """
    fn = _reconcile_def(source)
    offenders: list[str] = []

    guarded_lines: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and reads_spec_replicas(node.test):
            guarded_lines |= _covered(node)
    forty_four_lines: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.ExceptHandler) and any(
            isinstance(c, ast.Constant) and c.value == 404 for c in ast.walk(node)
        ):
            forty_four_lines |= _covered(node)

    for node in ast.walk(fn):
        if not (isinstance(node, ast.Return) and node.value is not None):
            continue
        if "MISSING" not in ast.dump(node.value):
            continue
        line = node.lineno
        if line in forty_four_lines or line in guarded_lines:
            continue
        offenders.append(f"C1:{fn.name}:{line}")

    doc = ast.get_docstring(fn) or ""
    if not reads_spec_replicas(fn) and ("副本" in doc or "replicas" in doc.lower()):
        offenders.append(f"C2:{fn.name}:{fn.lineno}")
    return offenders


PREFIX_RECONCILE = '''    def reconcile(self, workspace: Workspace) -> RuntimeState:
        """判定 Deployment runtime 存活：available_replicas>=1 → ALIVE；404/0 副本 → MISSING；其他 → UNKNOWN。"""
        deployment_name = workspace.container_name or self._deployment_name(workspace)
        try:
            api = self._require_client()
            status = api.AppsV1Api().read_namespaced_deployment_status(
                name=deployment_name, namespace=self.settings.k8s_namespace
            ).status
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return RuntimeState.MISSING
            return RuntimeState.UNKNOWN
        available = getattr(status, "available_replicas", None) or 0
        if int(available) >= 1:
            return RuntimeState.ALIVE
        return RuntimeState.MISSING
'''

SYNTHETIC_PREFIX = '''class KubernetesProvider:
    def reconcile(self, workspace):
        """判定 runtime：available_replicas>=1 → ALIVE；404/0 副本 → MISSING；其他 → UNKNOWN。"""
        try:
            status = self.api.read_namespaced_deployment_status(name="n", namespace="ns").status
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return RuntimeState.MISSING
            return RuntimeState.UNKNOWN
        if int(getattr(status, "available_replicas", None) or 0) >= 1:
            return RuntimeState.ALIVE
        return RuntimeState.MISSING
'''

# C2 单独开火的那一支：MISSING 只在 404 档出现（C1 全绿），docstring 却仍写着"0 副本"
SYNTHETIC_DOC_ONLY = '''class KubernetesProvider:
    def reconcile(self, workspace):
        """判定 runtime：404/0 副本 → MISSING；available_replicas>=1 → ALIVE；其他 → UNKNOWN。"""
        try:
            status = self.api.read_namespaced_deployment_status(name="n", namespace="ns").status
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return RuntimeState.MISSING
            return RuntimeState.UNKNOWN
        if int(getattr(status, "available_replicas", None) or 0) >= 1:
            return RuntimeState.ALIVE
        return RuntimeState.UNKNOWN
'''

SYNTHETIC_COMPLIANT = '''class KubernetesProvider:
    def reconcile(self, workspace):
        """判定 runtime：404 → MISSING；spec.replicas==0 → MISSING；available>=1 → ALIVE；其他 → UNKNOWN。"""
        try:
            dep = self.api.read_namespaced_deployment(name="n", namespace="ns")
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return RuntimeState.MISSING
            return RuntimeState.UNKNOWN
        if dep.spec is not None and dep.spec.replicas is not None and int(dep.spec.replicas) == 0:
            return RuntimeState.MISSING
        if int(getattr(dep.status, "available_replicas", None) or 0) >= 1:
            return RuntimeState.ALIVE
        return RuntimeState.UNKNOWN
'''


def test_the_ruler_fires_on_the_pre_fix_shapes_and_stays_quiet_on_the_fix():
    """must-fire + 合规控制：两条 clause 各有一支只归它开的火，合规写法不许开火。"""
    # C1：改前那份函数体（兜底 return MISSING 不在 404 档、也不在任何 spec.replicas 分支里）
    c1 = reconcile_absence_offenders(SYNTHETIC_PREFIX)
    # 行号由夹具自己算（缩进 8 空格的那一条兜底 return；404 档里那条缩进 16，不会被匹配）
    fallback_line = 1 + SYNTHETIC_PREFIX.splitlines().index("        return RuntimeState.MISSING")
    assert [o for o in c1 if o.startswith("C1")] == [f"C1:reconcile:{fallback_line}"], c1
    assert any(o.startswith("C2") for o in c1), "改前那份的 docstring 承诺同样没实现，两条都该点名"

    # C2 单独开火的那一支：C1 全绿（MISSING 只剩 404 档），docstring 还在写"0 副本"
    c2 = reconcile_absence_offenders(SYNTHETIC_DOC_ONLY)
    assert [o for o in c2 if o.startswith("C1")] == [], f"这一支不该被 C1 看见：{c2}"
    assert len([o for o in c2 if o.startswith("C2")]) == 1, c2

    # 合规控制：读了 spec.replicas 的写法两条都不许开火
    assert reconcile_absence_offenders(SYNTHETIC_COMPLIANT) == []


def test_the_ruler_is_not_blind_on_the_real_k8s_provider():
    """真语料两档：现源码必须合规且确实读了 spec.replicas；把改前形状 splice 回去尺子必须点名。

    先判合规侧再判变异侧：改前的文件会让第一条就带着 C1/C2 的落点翻红（那才是本轮要读的读数），
    而 splice 的 `mutant != src` 只在锚点没落地时开火 —— 锚点命中为 0 会被读成"尺子失灵"，
    所以那道断言留在后面单独守它自己那一件事。
    """
    src = (PROVIDERS_DIR / "k8s.py").read_text(encoding="utf-8")

    assert reconcile_absence_offenders(src) == [], reconcile_absence_offenders(src)
    assert reads_spec_replicas(_reconcile_def(src)), "reconcile 里根本没有 .spec.replicas 的读取"
    # 承重前提的另一半：docstring 那句「0 副本」现在背后真有一次代码读取，不是空头承诺。
    # 这句留着，C2 那条 clause 才有真语料上的靶：docstring 不再提副本的话 C2 就退化成只看代码。
    assert "副本" in (ast.get_docstring(_reconcile_def(src)) or "")

    marker = "    def reconcile(self, workspace: Workspace) -> RuntimeState:\n"
    assert src.count(marker) == 1, f"锚点命中 {src.count(marker)} 次，不是 1"
    mutant = src.split(marker)[0] + PREFIX_RECONCILE
    assert mutant != src, "锚点没落地（splice 回到了原文）"
    assert reconcile_absence_offenders(mutant), "把改前形状放回了真源码而尺子沉默"

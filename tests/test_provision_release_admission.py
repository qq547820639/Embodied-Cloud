"""Provision 失败那一路的释放准入（N-67）：卡只放回 provider 认账"已退役"的那一格。

不变量（本仓已在三处成立，这里是第四处）：GPU 能不能回池，只由 provider 自述的 runtime
事实决定，不由"清理命令没报错"决定。唯一的判据是
`WorkspaceOrchestrator._release_admitted(workspace, *, command_succeeded)`
（app/services/orchestrator.py:471-490），其余消费位是 `_stop_cleanup` 的成功档
（:452）与报错档（:456）、warm pool 的 claim 撤销档（app/services/warmpool.py 的 claim）。

缺陷形状（改前，`_execute_provision` 的补偿域）：
1. `with contextlib.suppress(Exception): self.provider.destroy(workspace)` 把"补偿到底
   成没成"原地丢掉；
2. 异常上抛到 `_fail`，后者无条件 `scheduler.release` + 清 `gpu_id/gpu_index/gpu_name`。
   于是 destroy 失败（daemon 断线、Pod 卡在 Terminating、PVC 删不掉）时，那个还挂着
   `--gpus device=N` 的容器被留在原地，卡却回了池子，下一个租户落在同一张卡上。

不放行档写 **STOPPING**（不是 PROVISIONING，也不是保持 QUEUED），两条都是实测出来的：
- `recover_stuck_gpu_allocations`（app/services/scheduler.py:271-297）按**状态**只保护
  {PROVISIONING, RUNNING, STOPPING}，另外还保护"有 active operation"的格；FAILED 与
  QUEUED 都在状态保护外。retryable 档当天写的 QUEUED 只在 operation 还活着时安全，
  attempts 用尽（operation → FAILED）那一刻它就成了"终态孤儿"，卡被强制放掉 ——
  本文件的
  `test_retryable_not_admitted_failure_survives_the_operation_going_terminal`
  用真函数把这两极都读了一遍。
- PROVISIONING 两个要求都不满足：`_execute_provision` 的幂等前置
  （status ∈ {RUNNING, PROVISIONING} 直接 return）会把 durable 重试变成空转并被 worker
  记成 SUCCEEDED；而不放行时 provider 恰恰说 ALIVE，`reconcile_all` 的 PROVISIONING+ALIVE
  档（:675-681）会把它 adopt 成 RUNNING —— 一个端口没落库、凭据没落库、provision 明确
  失败的 runtime 被对外 status 宣布"在跑"。STOPPING 反过来已经在
  `reconcile_all:686-698` 有一条真会再叫 provider 的退役路。

两极缺一不可：destroy 报错但 provider 亲口说 MISSING（K8s 对已删 Deployment 的 404）
必须照旧放卡，否则那张卡被永久钉在一个已经没有使用者的 runtime 上。
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    CreditHold,
    Gpu,
    GpuAllocation,
    GpuStatus,
    HoldStatus,
    LedgerType,
    OperationStatus,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from app.services.billing import BillingPolicy
from app.services.ledger import CreditLedgerService
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.base import RuntimeState
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler, recover_stuck_gpu_allocations
from app.services.worker import OperationWorker
from tests.dbfiles import db_url

ENGINE = create_engine(
    db_url("provision-admission"), connect_args={"check_same_thread": False}
)
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_SOURCE = (REPO_ROOT / "app" / "services" / "orchestrator.py").read_text(
    encoding="utf-8"
)

REPORTS = {
    "alive": RuntimeState.ALIVE,
    "missing": RuntimeState.MISSING,
    "unknown": RuntimeState.UNKNOWN,
}

PROVISION_ERROR = "simulated: runtime create failed"
DESTROY_ERROR = "simulated: compensating destroy failed"
STOP_ERROR = "simulated: runtime stop failed"


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


class ProvisionBoom(RuntimeError):
    """provider 侧的原始失败。类型刻意与"补偿失败"不同，用来验上抛的还是它。"""


class DestroyBoom(RuntimeError):
    """补偿 destroy 的失败：它绝不该变成逃出去的那个异常。"""


class ProvisionFaultProvider(MockProvider):
    """provision 恒失败；destroy/stop 成不成、provider 自述什么 runtime 事实，各自可配。

    mock 只会说 UNKNOWN（providers/mock.py:72-74），它证不了"还活着"，所以这里换一把
    自带 runtime 事实的替身（与 tests/test_stop_release_admission.py 的 `StatefulProvider`
    同一立场）。

    - `destroy_raises=True,  reports="alive"`   ⇒ 不放行档（卡必须留下）
    - `destroy_raises=True,  reports="unknown"` ⇒ 同样不放行（答不上来不算"亲口说没了"）
    - `destroy_raises=True,  reports="missing"` ⇒ 放行档（K8s 404 那一极）
    - `destroy_raises=False, reports="alive", retire_on_destroy=False`
                                               ⇒ 命令成功但 runtime 还在吃卡：不放行
    - `destroy_raises=False, reports="missing"/"unknown"` ⇒ 命令成功档（演示路径必须停得下来）
    - `stop_reports`：`reconcile_all` 的 STOPPING 档真去 stop 之后 provider 改不改口
    """

    def __init__(
        self,
        *,
        reports: str = "alive",
        destroy_raises: bool = True,
        retire_on_destroy: bool = True,
        stop_reports: str = "alive",
    ):
        super().__init__("http://127.0.0.1:8000")
        assert reports in REPORTS, reports
        assert stop_reports in REPORTS, stop_reports
        self.reports = reports
        self.destroy_raises = destroy_raises
        self.retire_on_destroy = retire_on_destroy
        self.stop_reports = stop_reports
        self.provision_calls = 0
        self.destroy_calls = 0
        self.stop_calls = 0
        self.wait_calls = 0

    def provision(self, workspace, template, workspace_dir, reservation=None):  # type: ignore[override]
        self.provision_calls += 1
        # 容器/Pod 已经建出来了：真实形状（补偿域就是为这一档存在的）
        workspace.container_name = f"ec-{workspace.id[:12]}"
        raise ProvisionBoom(PROVISION_ERROR)

    def wait_ready(self, workspace, template, timeout_seconds: int = 120) -> bool:
        self.wait_calls += 1
        return True

    def destroy(self, workspace):
        self.destroy_calls += 1
        if self.destroy_raises:
            raise DestroyBoom(DESTROY_ERROR)
        if self.retire_on_destroy and self.reports == "alive":
            self.reports = "missing"
        return None

    def stop(self, workspace):
        self.stop_calls += 1
        self.reports = self.stop_reports
        return None

    def reconcile(self, workspace: Workspace) -> RuntimeState:
        return REPORTS[self.reports]

    def retire(self) -> None:
        """provider 改答案：runtime 其实已经没了，destroy 也可以重试了。"""
        self.destroy_raises = False
        self.reports = "missing"


def _seed(db, *, with_user: bool = False) -> None:
    GpuScheduler(Factory).sync_host(
        db,
        host_id="host-1",
        name="h1",
        address="127.0.0.1",
        provider="mock",
        gpus=[GpuInfo(gpu_uuid="gpu-1", model="RTX", memory_total=24564, index=0)],
    )
    db.add(
        Template(
            id="cartpole",
            slug="cartpole",
            name="cartpole",
            description="test",
            category="test",
            runtime="mock",
            launch_command="echo ok",
            enabled=True,
            requires_streaming=False,
            recommended_vram_gb=16,
            estimated_hourly_cost_cny=1.0,
        )
    )
    if with_user:
        db.add(
            User(
                id="u1",
                email="u1@x",
                username="u1",
                password_hash="x",  # noqa: S106 测试桩用户，非真实密码
                role=Role.USER.value,
            )
        )
    db.commit()


def _make_orchestrator(provider: ProvisionFaultProvider, billing: BillingPolicy | None = None):
    return WorkspaceOrchestrator(
        Factory,
        provider,
        Path("/tmp/test-provision-admission"),  # noqa: S108 测试隔离目录
        billing=billing,
    )


def _new_workspace(orchestrator) -> str:
    with Factory() as db:
        _seed(db, with_user=billing_user_needed(orchestrator))
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        return ws.id


def billing_user_needed(orchestrator: WorkspaceOrchestrator) -> bool:
    """带 billing 的那一档需要真有一行 User（门禁与 hold 都按 user_id 走）。"""
    return orchestrator.billing is not None


def _effects(db, workspace_id: str) -> dict:
    """从权威表读"卡到底放没放"：分配行、卡状态、卡的归属。"""
    gpu = db.scalar(select(Gpu))
    assert gpu is not None, "库里没有卡，判据无从判起"
    return {
        "alloc": db.scalar(
            select(func.count(GpuAllocation.id)).where(
                GpuAllocation.workspace_id == workspace_id
            )
        )
        == 1,
        "gpu_free": gpu.status == GpuStatus.AVAILABLE.value,
        "gpu_holder": gpu.workspace_id == workspace_id,
    }


HELD = {"alloc": True, "gpu_free": False, "gpu_holder": True}
RELEASED = {"alloc": False, "gpu_free": True, "gpu_holder": False}

TERMINAL_STATES = {
    WorkspaceStatus.FAILED.value,
    WorkspaceStatus.STOPPED.value,
    WorkspaceStatus.DELETED.value,
}


def _fail_provision(provider: ProvisionFaultProvider) -> str:
    """真走一次同步 provision（`_start` 路径 = operation is None = 终态档）。"""
    orchestrator = _make_orchestrator(provider)
    wid = _new_workspace(orchestrator)
    orchestrator._start(wid)  # 失败不外抛（ADR 0002），状态由 `_fail` 落库
    assert provider.provision_calls == 1
    assert provider.destroy_calls == 1, "补偿 destroy 必须真的被叫过一次"
    return wid


# ---------------------------------------------------------------------------
# 必须开火档：destroy 没成、provider 没说 runtime 没了 ⇒ 留卡、留列、不写终态
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reports", ["alive", "unknown"])
def test_compensating_destroy_failure_keeps_the_card_until_the_provider_confirms(
    reports: str,
):
    """终态档（`_start`，operation is None）：provider 说 ALIVE / UNKNOWN ⇒ 不放行。

    `alive` 是"容器还在吃 --gpus device=N"那一极；`unknown` 是"provider 压根答不上来"
    那一极。判据在报错档只认 MISSING（orchestrator.py:477-479 与 :490），所以两极都不许放卡。
    """
    provider = ProvisionFaultProvider(reports=reports, destroy_raises=True)
    wid = _fail_provision(provider)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        # 1) 卡没回池：分配行还在、卡不是 AVAILABLE、卡的归属仍指向这一格
        assert _effects(db, wid) == HELD, (
            f"destroy 抛错、provider 说的是 {reports}，卡却被放回池子里了（一卡双跑）"
        )
        # 2) GPU 三列没被清掉（那是"这一格还占着哪张卡"的事实本体）
        assert ws.gpu_id is not None and ws.gpu_index is not None and ws.gpu_name
        # 3) 容器仍可寻址（重试与取证靠它）
        assert ws.container_name == f"ec-{wid[:12]}"
        # 4) 不写终态：FAILED 会被 recover_stuck_gpu_allocations 当孤儿把卡强制放掉
        assert ws.status == WorkspaceStatus.STOPPING.value, ws.status
        assert ws.status not in TERMINAL_STATES
        # 5) 原因写清：原始失败 + 不放行的理由（含 provider 自己的回话与 destroy 的错）
        msg = ws.error_message or ""
        assert PROVISION_ERROR in msg, msg
        assert "GPU not released" in msg, msg
        assert f"runtime {reports}" in msg, msg
        assert DESTROY_ERROR in msg, msg

    # 6) 那把"强制放孤儿卡"的尺子真在场，而且这一格因为它保护得住：
    #    先证没有 active operation 兜底（保护只能来自状态本身），再真调一次。
    with Factory() as db:
        active = db.scalar(
            select(func.count(WorkspaceOperation.id)).where(
                WorkspaceOperation.workspace_id == wid,
                WorkspaceOperation.status.in_(
                    [
                        OperationStatus.PENDING.value,
                        OperationStatus.RUNNING.value,
                        OperationStatus.RETRYING.value,
                    ]
                ),
            )
        )
        assert active == 0, f"前提塌了：还有 {active} 个 active operation 在替这张卡挡收割"
        recover_stuck_gpu_allocations(db)
    with Factory() as db:
        assert _effects(db, wid) == HELD, "STOPPING 没被 recover_stuck_gpu_allocations 保护"

        # 反证（同一行、同一把尺子）：只把状态改成 FAILED ⇒ 卡立刻被放掉。
        # 没有这一支，上一条"卡还在"可能只是那把尺子根本不干活。
        row = db.get(Workspace, wid)
        assert row is not None
        row.status = WorkspaceStatus.FAILED.value
        db.commit()
    with Factory() as db:
        recover_stuck_gpu_allocations(db)
    with Factory() as db:
        assert _effects(db, wid) == RELEASED, "终态行没被放卡 ⇒ 上一条反证不成立，尺子是假的"


def test_reconcile_keeps_retiring_the_orphan_and_releases_only_when_the_provider_agrees():
    """不放行档留下的 STOPPING 有人重试：`reconcile_all` 真再叫一次 provider。

    承重读数是"provider 又被叫了一次"（`stop_calls` 递增），不是"没报错"。
    诚实边界：驱动这条退役的是 `reconcile_all` 的 STOPPING+ALIVE 档（:684-696），
    它调的是 `provider.stop`（orchestrator.py:687-692）；`provider.destroy` 的再叫要靠显式 DESTROY operation
    （见下一条）。两档都不许在 provider 认账前把卡放掉。
    """
    provider = ProvisionFaultProvider(reports="alive", destroy_raises=True, stop_reports="alive")
    wid = _fail_provision(provider)
    with Factory() as db:
        assert _effects(db, wid) == HELD, "前提塌了：撤销时卡已经放了，收敛档无从谈起"
    assert provider.stop_calls == 0

    orchestrator = _make_orchestrator(provider)
    stats = orchestrator.reconcile_all()
    assert provider.stop_calls == 1, "STOPPING 档没再去叫 provider（只留痕不收敛）"
    assert stats["stopped"] == 0, f"没认账却计了已停数：{stats}"
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        assert ws.status == WorkspaceStatus.STOPPING.value
        assert _effects(db, wid) == HELD, "provider 还说 ALIVE，卡就回池了"

    stats = orchestrator.reconcile_all()
    assert provider.stop_calls == 2, "第二轮没有接着试"
    assert stats["stopped"] == 0

    # 单变量：provider 改口（runtime 真停了）之后，同一条档才放卡并收终态
    provider.stop_reports = "missing"
    stats = orchestrator.reconcile_all()
    assert provider.stop_calls == 3
    assert stats["stopped"] == 1, f"认账了却没计已停数：{stats}"
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        assert ws.status == WorkspaceStatus.STOPPED.value
        assert _effects(db, wid) == RELEASED, "provider 已认账退役，卡却没回池"


def test_an_explicit_destroy_operation_re_asks_the_provider_to_destroy():
    """DESTROY 轴：显式入队一次 DESTROY，`provider.destroy` 的计数必须增加。

    交互 workspace 不会被自动扫进 DESTROY（那是 warm pool `_cleanup_failed` 的活儿），
    但这一格必须**可以**被 destroy 收敛：改口的 provider 认账之后卡才回池、才置 DELETED。
    """
    provider = ProvisionFaultProvider(reports="alive", destroy_raises=True)
    wid = _fail_provision(provider)
    assert provider.destroy_calls == 1
    orchestrator = _make_orchestrator(provider)

    worker = OperationWorker(Factory, orchestrator)
    op = worker.enqueue(wid, "destroy")
    assert op is not None
    assert worker.tick_once() == 1
    # 还是没成：destroy 仍抛 ⇒ 计数 +1，卡照旧不放
    assert provider.destroy_calls == 2, "DESTROY operation 没有真再叫 provider.destroy"
    with Factory() as db:
        assert _effects(db, wid) == HELD
        assert db.get(Workspace, wid).status == WorkspaceStatus.STOPPING.value
        # 确定性推进 backoff：显式让 RETRYING 的 lease 过期（不依赖真实时钟）
        db_op = db.get(WorkspaceOperation, op.id)
        assert db_op is not None
        assert db_op.status == OperationStatus.RETRYING.value, db_op.status
        db_op.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    provider.retire()  # provider 改答案：runtime 其实已经没了
    assert worker.tick_once() == 1
    assert provider.destroy_calls == 3
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        assert _effects(db, wid) == RELEASED, "provider 认账退役后卡没回池"
        assert ws.status == WorkspaceStatus.DELETED.value


# ---------------------------------------------------------------------------
# 不得误伤档：provider 认账退役 ⇒ 今天的全部行为保持（改前改后都必须绿）
# ---------------------------------------------------------------------------


def test_destroy_failure_the_provider_vouches_for_still_releases_and_writes_the_terminal():
    """放行档（destroy 报错 + provider 亲口说 MISSING）：放卡 + FAILED，且措辞不被改动。

    K8s 对已删 Deployment 的 404 走这一极（providers/k8s.py:542-545 → MISSING）。
    不放行就是"把 GPU 永久钉死在一张已经没有使用者的卡上"，所以这一极是必须保住的
    既有行为，不是要修的东西。
    """
    provider = ProvisionFaultProvider(reports="missing", destroy_raises=True)
    wid = _fail_provision(provider)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        assert _effects(db, wid) == RELEASED
        assert ws.status == WorkspaceStatus.FAILED.value
        assert ws.gpu_id is None and ws.gpu_index is None and ws.gpu_name is None
        # 放行档的 error_message 与改前逐字相同：修复不往被误伤的那一极加话
        assert ws.error_message == PROVISION_ERROR, ws.error_message


@pytest.mark.parametrize("reports", ["missing", "unknown"])
def test_destroy_command_succeeded_and_provider_no_longer_says_alive_releases(reports: str):
    """放行档 B（`command_succeeded=True` 的两极）：provider 不再自述 ALIVE ⇒ 照旧放卡。

    `unknown` 是 mock 演示档（判据本体 :489，口径见 :474-476「演示路径必须停得下来」）：如果这一路的
    `command_succeeded` 被写死成 False，mock 的 UNKNOWN 就永远不放行，卡会被一直钉住。
    """
    provider = ProvisionFaultProvider(reports=reports, destroy_raises=False)
    wid = _fail_provision(provider)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        assert _effects(db, wid) == RELEASED
        assert ws.status == WorkspaceStatus.FAILED.value
        assert ws.gpu_id is None
        assert ws.error_message == PROVISION_ERROR, ws.error_message


def test_destroy_command_succeeding_is_not_evidence_the_runtime_is_gone():
    """反面对手（`command_succeeded=True` + ALIVE）：命令成功也不替 runtime 事实背书。

    这一档改前连"provider 说还活着"都不问，直接放卡。
    """
    provider = ProvisionFaultProvider(
        reports="alive", destroy_raises=False, retire_on_destroy=False
    )
    wid = _fail_provision(provider)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        assert _effects(db, wid) == HELD, "destroy 返回了成功，provider 却说 runtime 还在，卡却回池了"
        assert ws.status == WorkspaceStatus.STOPPING.value
        assert ws.gpu_id is not None
        msg = ws.error_message or ""
        assert "GPU not released" in msg and "runtime alive" in msg
        # 这一档没有 destroy 错误可抄，诊断尾巴落在"命令自报成功"上
        assert "compensating destroy reported success" in msg, msg
    # 失败档只问一次判据，绝不重试 destroy（补偿域只跑一遍）
    assert provider.destroy_calls == 1


# ---------------------------------------------------------------------------
# 判据问不出结果（provider 自己抛错）：ADR 0008 同一口径 —— 问不到不等于不存在
# ---------------------------------------------------------------------------


class UndecidableProvider(ProvisionFaultProvider):
    """连"runtime 还在不在"都答不上来的那一极：`reconcile` 自己抛。

    这一档要看两件事：`_fail` 既不许放卡，也不许把判据里冒出来的新异常盖成失败原因
    （调用方正在处理的是 provision 失败，不是"问不到"）。
    """

    def reconcile(self, workspace):  # type: ignore[override]
        raise RuntimeError("engine unreachable")


def test_undecidable_admission_keeps_the_card_and_the_original_failure():
    provider = UndecidableProvider(reports="alive", destroy_raises=True)
    wid = _fail_provision(provider)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert _effects(db, wid) == HELD, "判据都问不出结果，卡却回了池子"
        assert ws.status == WorkspaceStatus.STOPPING.value
        assert ws.gpu_id is not None
        msg = ws.error_message or ""
        assert PROVISION_ERROR in msg, f"原始失败被盖掉了：{msg}"
        assert "GPU not released" in msg and "unobservable" in msg, msg


# ---------------------------------------------------------------------------
# retryable 档（terminal=False）：两害相权，资源轴优先
# ---------------------------------------------------------------------------


def test_retryable_provision_failure_still_publishes_no_verdict_when_admitted():
    """放行档的既有行为必须原样保住：operation 还要重试时不得写终态（N-79 的判据）。

    这一条与 tests/test_worker.py::test_retryable_provision_failure_is_not_published_as_terminal
    同一立场，但用本文件的替身重跑一遍：修复把判据接到 `_fail` 上之后，
    "还要再试 ⇒ status 停在 QUEUED + 卡照旧回池"那一档不许被顺手改掉。
    """
    provider = ProvisionFaultProvider(reports="unknown", destroy_raises=False)
    orchestrator = _make_orchestrator(provider)
    wid = _new_workspace(orchestrator)
    op = orchestrator.start_async(wid)
    assert op is not None
    worker = OperationWorker(Factory, orchestrator)
    original = OperationWorker.RETRY_BASE_DELAY
    OperationWorker.RETRY_BASE_DELAY = 0
    try:
        assert worker.tick_once() == 1
    finally:
        OperationWorker.RETRY_BASE_DELAY = original
    with Factory() as db:
        db_op = db.get(WorkspaceOperation, op.id)
        assert db_op is not None
        assert db_op.status == OperationStatus.RETRYING.value, db_op.status
        ws = db.get(Workspace, wid)
        assert ws is not None
        assert ws.status == WorkspaceStatus.QUEUED.value, ws.status
        assert _effects(db, wid) == RELEASED, "放行档没放卡：重试永远抢不到卡，池子还会被抽干"
        assert ws.gpu_id is None


def test_retryable_not_admitted_failure_survives_the_operation_going_terminal():
    """不放行的 retryable 档：卡留下的状态必须**不依赖** operation 还活着。

    两害相权：这一档牺牲的是"status 只描述尝试结论"（改前 QUEUED，现在 STOPPING），
    换来的是"卡不在 runtime 还活着时回池"。理由与读数：
    - `recover_stuck_gpu_allocations` 对 QUEUED 的保护**只**来自"有 active operation"
      那一半（scheduler.py:285-297）；attempts 用尽后 operation → FAILED，保护即消失，
      卡会被它按孤儿强制放掉 —— 本函数的准入判据等于被绕过（下面的反证读的就是这一档）。
    - STOPPING 在状态保护集内（scheduler.py:274-281），与 operation 无关。
    另一个方向没有回归：没有写 FAILED，也没有写任何"这一次尝试的结论"之外的判决；
    STOPPING 说的是"正在退役它的 runtime"，而不放行档那一刻的真话正是这个。
    """
    provider = ProvisionFaultProvider(reports="alive", destroy_raises=True)
    orchestrator = _make_orchestrator(provider)
    wid = _new_workspace(orchestrator)
    op = orchestrator.start_async(wid)
    assert op is not None
    worker = OperationWorker(Factory, orchestrator)
    original = OperationWorker.RETRY_BASE_DELAY
    OperationWorker.RETRY_BASE_DELAY = 0
    try:
        assert worker.tick_once() == 1
        with Factory() as db:
            db_op = db.get(WorkspaceOperation, op.id)
            ws = db.get(Workspace, wid)
            assert db_op is not None and ws is not None
            # 前提：这确实是"还要再试"的一档，不是终态
            assert db_op.status == OperationStatus.RETRYING.value, db_op.status
            assert db_op.attempts < OperationWorker.MAX_ATTEMPTS
            assert ws.status == WorkspaceStatus.STOPPING.value, ws.status
            assert ws.status not in TERMINAL_STATES
            assert _effects(db, wid) == HELD
            assert ws.gpu_id is not None
            # 非终态 ⇒ status 没有替 worker 下结论
            assert ws.status != WorkspaceStatus.FAILED.value

        # 把 attempts 跑到用尽：operation → FAILED，"有 active operation"那半边保护消失
        for _ in range(OperationWorker.MAX_ATTEMPTS):
            with Factory() as db:
                db_op = db.get(WorkspaceOperation, op.id)
                assert db_op is not None
                if db_op.status == OperationStatus.RETRYING.value:
                    db_op.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    db.commit()
            worker.tick_once()
    finally:
        OperationWorker.RETRY_BASE_DELAY = original

    with Factory() as db:
        db_op = db.get(WorkspaceOperation, op.id)
        ws = db.get(Workspace, wid)
        assert db_op is not None and ws is not None
        assert db_op.status == OperationStatus.FAILED.value, db_op.status
        assert ws.status == WorkspaceStatus.STOPPING.value, ws.status
        assert _effects(db, wid) == HELD, "attempts 用尽后卡被放掉了 —— retryable 档不能只靠 operation 挡收割"
        active = db.scalar(
            select(func.count(WorkspaceOperation.id)).where(
                WorkspaceOperation.workspace_id == wid,
                WorkspaceOperation.status.in_(
                    [
                        OperationStatus.PENDING.value,
                        OperationStatus.RUNNING.value,
                        OperationStatus.RETRYING.value,
                    ]
                ),
            )
        )
        assert active == 0, "前提塌了：还能靠 active operation 挡收割"
        recover_stuck_gpu_allocations(db)
    with Factory() as db:
        assert _effects(db, wid) == HELD, "STOPPING 在真尺子下没被保护住"

        # 反证：同一行、同一把尺子，状态换成 retryable 放行档的 QUEUED ⇒ 卡立刻被放掉。
        # 这一支才是"为什么不保持 QUEUED"的读数，不是"尺子不干活"。
        row = db.get(Workspace, wid)
        assert row is not None
        row.status = WorkspaceStatus.QUEUED.value
        db.commit()
    with Factory() as db:
        recover_stuck_gpu_allocations(db)
    with Factory() as db:
        assert _effects(db, wid) == RELEASED, "QUEUED 没被强制放卡 ⇒ 上一条反证不成立，尺子是假的"


def test_the_original_provision_error_is_what_escapes_the_compensation():
    """补偿的错误不许顶掉原始异常（ADR 0002：边界不得给调用方换异常类型）。"""
    provider = ProvisionFaultProvider(reports="alive", destroy_raises=True)
    orchestrator = _make_orchestrator(provider)
    wid = _new_workspace(orchestrator)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        with pytest.raises(ProvisionBoom) as caught:
            orchestrator._execute_provision(db, ws)
    assert caught.value.args and PROVISION_ERROR in str(caught.value)
    assert DESTROY_ERROR not in str(caught.value), "补偿 destroy 的错顶掉了原始失败"
    assert not isinstance(caught.value, DestroyBoom)
    assert caught.value.__cause__ is None
    # 原始异常仍然被记账：状态与卡都按不放行档留在原位
    with Factory() as db:
        row = db.get(Workspace, wid)
        assert row is not None
        assert row.status == WorkspaceStatus.STOPPING.value
        assert PROVISION_ERROR in (row.error_message or "")


def test_the_billing_hold_return_is_not_gated_on_the_gpu_admission():
    """额度轴与资源轴分开：卡不放行，圈住的额度照样原样退回（N-80 已定的口径）。"""
    ledger = CreditLedgerService(Factory)
    policy = BillingPolicy(
        Factory, ledger, minimum_launch_minutes=5, enforce_preauthorization=True
    )
    provider = ProvisionFaultProvider(reports="alive", destroy_raises=True)
    orchestrator = _make_orchestrator(provider, billing=policy)
    wid = _new_workspace(orchestrator)
    with Factory() as db:
        ledger.record(
            db, type=LedgerType.RECHARGE, amount=1000, user_id="u1", description="recharge"
        )
        db.commit()
    orchestrator._start(wid)

    assert provider.destroy_calls == 1
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        assert ws.status == WorkspaceStatus.STOPPING.value
        # 资源轴：卡留着
        assert _effects(db, wid) == HELD
        # 额度轴：pending hold 已经退回，与准入判决无关
        pending = db.scalar(
            select(func.count(CreditHold.id)).where(
                CreditHold.workspace_id == wid,
                CreditHold.status == HoldStatus.PENDING.value,
            )
        )
        assert pending == 0, "释放准入把退款也一起挡了：两轴必须分开"
        released = db.scalars(
            select(CreditHold).where(
                CreditHold.workspace_id == wid,
                CreditHold.status == HoldStatus.RELEASED.value,
            )
        ).all()
        assert len(released) == 1, [h.status for h in released]
        assert released[0].reason == "provision failed"


# ---------------------------------------------------------------------------
# 文本面（AST）判据：`_fail` 里的那次 release 必须坐在准入判据之下
# ---------------------------------------------------------------------------


def fail_release_admission_shape(source: str) -> dict[str, int]:
    """AST 读 `_fail` 里"释放准入判据"与 GPU 触点/额度触点的接线形状。

    返回：
    - `fail` / `judgments` —— `_fail` 定义数、`_release_admitted` 调用数
    - `releases` / `guarded_releases` —— `*.release(...)` 调用数，其中落在
      "测试表达式里调了判据"的 if 分支内的数量
    - `gpu_clears` / `guarded_gpu_clears` —— `workspace.gpu_* = None` 赋值数与被守卫数
    - `billing_releases_outside_admission` —— `release_hold` 落在判据**之外**的数量
      （额度轴必须无条件执行，所以这一格必须是 1；把它挪进判据里就是缺陷）

    为什么必须按 AST 判：release 这个调用存在于 `_fail` 里不证明任何事 —— 它可以在判据
    之外；反过来把分支条件换成 `if True:` 之后，判据与 release **都还在**，按名字数数的
    尺子会照样点头（下面两份合成源码就是为了抓这两个形状）。
    """
    tree = ast.parse(source)
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_fail"),
        None,
    )
    assert fn is not None, "`_fail` 不存在：判据无从判起（这不是「没接线」，是尺子的靶没了）"

    def calls_judgment(node: ast.AST) -> bool:
        return any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "_release_admitted"
            for c in ast.walk(node)
        )

    def attr_call(node: ast.AST, attr: str) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attr
        )

    def guarded(node: ast.AST, guards: tuple[ast.If, ...]) -> bool:
        return any(calls_judgment(g.test) for g in guards)

    out = {
        "fail": 1,
        "judgments": 0,
        "releases": 0,
        "guarded_releases": 0,
        "gpu_clears": 0,
        "guarded_gpu_clears": 0,
        "billing_releases_outside_admission": 0,
    }

    def visit(node: ast.AST, guards: tuple[ast.If, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if attr_call(child, "_release_admitted"):
                out["judgments"] += 1
            if attr_call(child, "release"):
                out["releases"] += 1
                out["guarded_releases"] += 1 if guarded(child, guards) else 0
            if attr_call(child, "release_hold") and not guarded(child, guards):
                out["billing_releases_outside_admission"] += 1
            if isinstance(child, ast.Assign) and any(
                isinstance(t, ast.Attribute)
                and t.attr in {"gpu_id", "gpu_index", "gpu_name"}
                for t in child.targets
            ):
                out["gpu_clears"] += 1
                out["guarded_gpu_clears"] += 1 if guarded(child, guards) else 0
            visit(child, (*guards, child) if isinstance(child, ast.If) else guards)

    visit(fn, ())
    return out


def fail_call_wiring(source: str) -> dict[str, int]:
    """AST 读"`_fail` 的调用点有没有把补偿结果转交进去"（缺了这一转，判据只能盲放行）。"""
    tree = ast.parse(source)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_fail"
    ]
    forwards = sum(
        1
        for c in calls
        if any(k.arg == "command_succeeded" for k in c.keywords)
        and any(k.arg == "terminal" for k in c.keywords)
    )
    return {"fail_calls": len(calls), "forwards_command_succeeded": forwards}


# 合成反向对照一：release 与 GPU 清列**存在于** `_fail` 里、判据也**被调用**，
# 但放卡在判据之外（仓里没有这个形状，它存在的唯一目的是证明尺子会翻脸）。
UNGUARDED_FAIL_SOURCE = '''
class WorkspaceOrchestrator:
    def _fail(self, db, workspace, message, *, terminal=True, command_succeeded=True):
        self.billing.release_hold(db, workspace.id, reason="provision failed")
        self._release_admitted(workspace, command_succeeded=command_succeeded)
        workspace.status = "failed" if terminal else "queued"
        workspace.error_message = message
        self.scheduler.release(db, workspace.id)
        workspace.gpu_id = None
        workspace.gpu_index = None
        workspace.gpu_name = None
        db.flush()
'''

# 合成反向对照二：接线是对的（release 在判据里），但额度轴的退款被挪进了判据内 ——
# 那一格必须翻红，否则"billing 轴无条件"这句话没人守。
BILLING_INSIDE_GUARD_SOURCE = '''
class WorkspaceOrchestrator:
    def _fail(self, db, workspace, message, *, terminal=True, command_succeeded=True):
        if self._release_admitted(workspace, command_succeeded=command_succeeded):
            self.billing.release_hold(db, workspace.id, reason="provision failed")
            self.scheduler.release(db, workspace.id)
            workspace.gpu_id = None
            workspace.gpu_index = None
            workspace.gpu_name = None
'''


def test_the_gpu_release_in_the_provision_failure_path_sits_behind_the_admission_judgment():
    """真实源码：`_fail` 里的 release 与 GPU 清列恰好一处/三处，且都在判据为真的那一支。"""
    shape = fail_release_admission_shape(ORCHESTRATOR_SOURCE)
    assert shape == {
        "fail": 1,
        "judgments": 1,
        "releases": 1,
        "guarded_releases": 1,
        "gpu_clears": 3,
        "guarded_gpu_clears": 3,
        "billing_releases_outside_admission": 1,
    }, shape
    wiring = fail_call_wiring(ORCHESTRATOR_SOURCE)
    assert wiring == {"fail_calls": 1, "forwards_command_succeeded": 1}, wiring


def test_the_release_guard_can_see_the_unguarded_shape():
    """反向对照一（合成源码）：判据被算了却没当分支条件 ⇒ 放卡与清列都必须报 0。"""
    shape = fail_release_admission_shape(UNGUARDED_FAIL_SOURCE)
    assert shape["releases"] == 1, "合成夹具本身没造出 release 调用，等于没测"
    assert shape["judgments"] == 1, "合成夹具没保留判据调用，就抓不到「算了判决但不消费」"
    assert shape["gpu_clears"] == 3, "合成夹具没保留三处 GPU 清列"
    assert shape["guarded_releases"] == 0, f"尺子不开火：{shape}"
    assert shape["guarded_gpu_clears"] == 0, f"GPU 清列被守卫这件事看不见：{shape}"
    # 额度轴在这一份里本来就在判据外 ⇒ 尺子不把它误报成缺陷（防"见红就叫"的假牙）
    assert shape["billing_releases_outside_admission"] == 1


def test_the_release_guard_can_see_the_billing_axis_being_gated():
    """反向对照二（合成源码）：退款被挪进准入分支 ⇒ 那一格必须从 1 掉到 0。"""
    shape = fail_release_admission_shape(BILLING_INSIDE_GUARD_SOURCE)
    assert shape["guarded_releases"] == 1, "合成夹具的接线形状没被认出来，等于没测"
    assert shape["billing_releases_outside_admission"] == 0, "额度轴被挡在判据里却看不见"


def test_the_release_guard_fires_on_a_real_source_mutation():
    """反向对照三（真源码变异）：条件换成恒真，判据与 release 都在，接线却断了。"""
    anchor = "        if self._release_admitted(workspace, command_succeeded=command_succeeded):\n"
    assert ORCHESTRATOR_SOURCE.count(anchor) == 1, (
        f"锚点不是恰好一处（{ORCHESTRATOR_SOURCE.count(anchor)}）"
    )
    mutant = ORCHESTRATOR_SOURCE.replace(
        anchor,
        "        self._release_admitted(workspace, command_succeeded=command_succeeded)\n"
        "        if True:  # 变异：判据被算了，却没被用来决定放不放卡\n",
        1,
    )
    shape = fail_release_admission_shape(mutant)
    assert shape["releases"] == 1 and shape["gpu_clears"] == 3, "变异把夹具改坏了，不算开火"
    assert shape["guarded_releases"] == 0, "变异体没被抓到"
    assert shape["guarded_gpu_clears"] == 0, "GPU 清列的变异体没被抓到"
    # 另一极：同一把尺子跑真实源码不开火
    assert fail_release_admission_shape(ORCHESTRATOR_SOURCE)["guarded_releases"] == 1


def test_the_caller_wiring_checker_sees_a_dropped_compensation_outcome():
    """反向对照四：调用方不再转交 `command_succeeded=` ⇒ 尺子必须看见 0 处转交。

    没有这一支，"补偿结果被丢回 suppress 的老形状"就只剩行为判据看得见；
    行为判据要挑对 provider 才红，结构判据不需要。
    """
    anchor = "                command_succeeded=(not cleanup_attempted) or cleanup_error is None,\n"
    assert ORCHESTRATOR_SOURCE.count(anchor) == 1, (
        f"锚点不是恰好一处（{ORCHESTRATOR_SOURCE.count(anchor)}）"
    )
    mutant = ORCHESTRATOR_SOURCE.replace(anchor, "", 1)
    assert fail_call_wiring(mutant) == {
        "fail_calls": 1,
        "forwards_command_succeeded": 0,
    }, "丢掉转交参数看不见 ⇒ 这条判据是空的"
    assert fail_call_wiring(ORCHESTRATOR_SOURCE)["forwards_command_succeeded"] == 1

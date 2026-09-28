"""`gpu_seconds_total` 加的必须是**账本的增量**，不是可重放的段值（N-75）。

缺陷形状：`_finalize_stop` 每进一次收尾就对 Counter 做一次 `record_gpu_seconds(booked)`，
而 `booked` 是"这一段账本认了多少秒"。幂等键 `usage:{workspace.id}:{started_at.isoformat()}`
命中已有行时它照样返回那一行的 30 秒 ⇒ 账本每段只有一行，counter 却不幂等：
`stop` 重试（`scheduler.release` 抛错 → 状态留 STOPPING、`started_at` 不清零）和
`reconcile_all` 再来一趟，都会把同一段计第二次。

改前实测（2026-09-28，本 worktree，`/tmp/n75_probe.py` 跑真对象）：
stop 重试路 `after_first=+30.0 after_retry=+30.0 total=+60.0`，
reconcile_all 路 `+30.0 / +30.0 / +60.0`，而 `settled_gpu_seconds` 与独立 SUM 都是 30。

修法（与 N-74 的教义同一条）：Counter 只能单调增 ⇒ 加的量取结算前后两次
`settled_gpu_seconds` 之差（`_settle_run_delta` 的第二个读数）。
`_settle_run` 的公开契约**不变**：仍返回账本认下的段值，hold 转正继续用它
（N-74 的 C1 极与 `tests/test_credit_holds.py::test_zero_second_run_still_closes_the_hold`
都钉的是那个数）——`test_hold_capture_still_uses_the_ledger_segment_value` 专门钉
"两个数没有接反"，接反了 hold 会被记成 0 秒。

不做的两种替身（理由见本轮调研与两条实测探针）：
① 改成 Gauge 暴露账本和：违反 Prometheus 的 counter 语义与 `_total` 命名
（"an accumulating count has total as a suffix, in addition to the unit"），而仓里的 §8
文档门只核族名与标签、**看不见类型**（实测换 Gauge 后 `test_observability.py` 与
`test_metrics_spec.py` 全绿，负数 `inc` 也不再抛），所以类型这一格由本模块自己钉
（`test_gpu_seconds_is_declared_as_a_counter_under_its_documented_name` 加它的必开火对照）。
② 让 `CreditLedgerService.record` 回报"新建/命中"：为一个消费者给多个调用点加返回维度。
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.metrics import record_gpu_seconds
from app.models import (
    Course,
    CreditHold,
    CreditLedger,
    HoldStatus,
    Lab,
    LedgerType,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceStatus,
)
from app.services import orchestrator as orchestrator_module
from app.services.billing import BillingPolicy
from app.services.ledger import CreditLedgerService
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.base import RuntimeState
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.services.streaming import StreamingSessionService
from tests.dbfiles import db_url

ENGINE = create_engine(
    db_url("gpu-seconds-delta"), connect_args={"check_same_thread": False}
)
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
SEGMENT_SECONDS = 30
COUNTER_SERIES = "gpu_seconds_total"
COUNTER_NAME = "GPU_SECONDS"
RECORDER_NAME = "record_gpu_seconds"


@pytest.fixture(autouse=True)
def _db():
    """每例重建表并灌固定世界：一张 mock 卡 + cartpole 模板 + 学生 u1 + c1/lab-1。

    usage 行一条都不预写 —— 每条判据自己决定结算几次、counter 因此动几次。
    """
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    with Factory() as db:
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
                recommended_vram_gb=16,
                estimated_hourly_cost_cny=1.0,
            )
        )
        db.add(
            User(
                id="u1",
                email="u1@x",
                username="u1",
                password_hash="x",  # noqa: S106 测试桩用户，非真实密码
                role=Role.STUDENT.value,
            )
        )
        db.add(Course(id="c1", owner_id="u1", name="course", slug="course-1"))
        db.add(
            Lab(
                id="lab-1",
                course_id="c1",
                template_id="cartpole",
                name="lab",
                quota_seconds=3600,
            )
        )
        db.commit()
    yield
    Base.metadata.drop_all(ENGINE)


# ---------------------------------------------------------------------------
# 读数面与夹具件
# ---------------------------------------------------------------------------


class Clock:
    """可推进的假时钟：冻进 `orchestrator.utcnow` 后，结算秒数由它单独决定。"""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _freeze_clock(monkeypatch) -> Clock:
    clock = Clock(datetime.now(UTC))
    monkeypatch.setattr(orchestrator_module, "utcnow", clock)
    return clock


def counter_value() -> float:
    """从**默认注册表的暴露面**读 counter，不摸 `GPU_SECONDS._value` 的私有属性。

    读不到就显式红：那说明序列名或注册方式变了，本模块所有 delta 断言会与恒真同形。
    """
    value = REGISTRY.get_sample_value(COUNTER_SERIES)
    assert value is not None, (
        f"默认注册表里读不到序列 {COUNTER_SERIES}：读数口径已变，"
        "本轮判据看不见 counter，必须红而不是退回私有属性"
    )
    return float(value)


def _ledger_gpu_seconds(db, workspace_id: str) -> int:
    """账本这一侧的独立合计（同表同谓词，但不经被测的 `_settle_run_delta`）。

    与 `settled_gpu_seconds()` 各算各的，末尾再把两者对齐 —— 这样改前那份代码也能
    比出一条数，不必先有新方法才不崩。
    """
    total = db.scalar(
        select(func.coalesce(func.sum(CreditLedger.gpu_seconds), 0)).where(
            CreditLedger.workspace_id == workspace_id,
            CreditLedger.type == str(LedgerType.USAGE),
        )
    )
    return int(total or 0)


def _usage_rows(db, workspace_id: str) -> list[CreditLedger]:
    return list(
        db.scalars(
            select(CreditLedger).where(
                CreditLedger.workspace_id == workspace_id,
                CreditLedger.type == str(LedgerType.USAGE),
            )
        )
    )


def _orchestrator(
    provider=None, billing: BillingPolicy | None = None
) -> WorkspaceOrchestrator:
    return WorkspaceOrchestrator(
        Factory,
        provider or MockProvider("http://127.0.0.1:8000"),
        Path("/tmp/test-gpu-seconds-delta"),  # noqa: S108 测试隔离目录
        streaming=StreamingSessionService(Factory),
        ledger=CreditLedgerService(Factory),
        billing=billing,
    )


def _running_workspace(clock: Clock) -> str:
    """直接落一行 RUNNING workspace，`started_at` 在 clock 之前 SEGMENT_SECONDS 秒。"""
    with Factory() as db:
        ws = Workspace(
            id="ws-metric",
            name="metric",
            template_id="cartpole",
            provider="mock",
            user_id="u1",
            status=WorkspaceStatus.RUNNING.value,
            started_at=clock.now - timedelta(seconds=SEGMENT_SECONDS),
        )
        db.add(ws)
        db.commit()
    return ws.id


def _provisioned(
    clock: Clock, provider=None, billing: BillingPolicy | None = None
) -> tuple[WorkspaceOrchestrator, str]:
    """真走 provision 到 RUNNING（卡由 scheduler 原子分配），再把段挪到过去。

    release 失败那一档要求卡真被占着，否则抛不抛没人在等它。
    """
    orchestrator = _orchestrator(provider, billing=billing)
    with Factory() as db:
        wid = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1").id
    orchestrator._start(wid)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value, (
            f"前提未达成：provision 没带到 RUNNING（status={ws.status}）"
        )
        ws.started_at = clock.now - timedelta(seconds=SEGMENT_SECONDS)
        db.commit()
    return orchestrator, wid


class FailingReleaseScheduler:
    """只把 `release` 换成抛错，其余转给内层：结算已落库、状态留 STOPPING、`started_at` 未清。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0

    def release(self, db, workspace_id):
        self.calls += 1
        raise RuntimeError("simulated: GPU release failure")

    def __getattr__(self, name):
        return getattr(self.inner, name)


class StatefulProvider(MockProvider):
    """runtime 事实由 provider 自己声明（reconcile_all 的分支全靠它）。"""

    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:8000")
        self.alive = True

    def stop(self, workspace):
        self.alive = False

    def reconcile(self, workspace) -> RuntimeState:
        return RuntimeState.ALIVE if self.alive else RuntimeState.MISSING


# ---------------------------------------------------------------------------
# T1 登记项读到的那条路：stop 重试不得把同一段计两次
# ---------------------------------------------------------------------------


def test_stop_retry_increments_the_counter_once_for_one_segment(monkeypatch):
    """30 秒段：第 1 次 stop 结算后 release 抛错，第 2 次 stop 真收尾。

    判据三件事缺一不可：
    ① 第一次进收尾 +30（改前也是 +30，这一极不许被"修好"成 0）；
    ② 重试那一次 +0（改前是 +30，这就是 N-75）；
    ③ counter 的增量与**账本**对齐（`settled_gpu_seconds` 与独立 SUM 两个读数），
       不是与本轮新加的哪个 helper 对齐。
    """
    clock = _freeze_clock(monkeypatch)
    orchestrator, wid = _provisioned(clock)
    failing = FailingReleaseScheduler(orchestrator.scheduler)
    orchestrator.scheduler = failing

    start = counter_value()
    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))
    after_first = counter_value()
    assert failing.calls == 1
    with Factory() as db:
        ws = db.get(Workspace, wid)
        # 前提（不是结论）：这段结算过但没收尾，重试还会对它再结算一次
        assert ws.status == WorkspaceStatus.STOPPING.value, ws.status
        assert ws.started_at is not None, "started_at 被清了，重试根本不会再进结算"
        assert len(_usage_rows(db, wid)) == 1

    orchestrator.scheduler = failing.inner
    clock.advance(60)  # 重放之间又过了 60 秒：elapsed 会更大，段值不变
    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))
    after_retry = counter_value()

    assert after_first - start == SEGMENT_SECONDS, (
        f"第一次收尾加的是 {after_first - start}，应为账本认下的 {SEGMENT_SECONDS}"
    )
    assert after_retry - after_first == 0, (
        f"重放同一段把 counter 又加了 {after_retry - after_first}（N-75：账本一行没多）"
    )
    with Factory() as db:
        settled = orchestrator.ledger.settled_gpu_seconds(db, wid)
        independent = _ledger_gpu_seconds(db, wid)
        assert settled == independent == SEGMENT_SECONDS, (settled, independent)
        ratio = (after_retry - start) / max(settled, 1)
        assert after_retry - start == settled, (
            f"counter 增量 {after_retry - start} 与账本 {settled} 分叉（倍数 {ratio}）"
        )
        assert db.get(Workspace, wid).status == WorkspaceStatus.STOPPED.value


# ---------------------------------------------------------------------------
# T2 直接对同一段 finalize 两次：delta 为 0 而段值仍是 30（两个数各归各位）
# ---------------------------------------------------------------------------


def test_second_finalize_of_the_same_segment_reports_zero_but_keeps_the_segment_value(
    monkeypatch,
):
    """同一段 finalize 两趟（都不放行）：第二趟加 0；`booked` 仍是 30、`delta` 是 0。

    这一支把"设计决定"里那两个数钉死：改 `_settle_run` 的返回值会让 N-74 的 C1 极与
    hold 转正一起错位，所以这里同时断 `booked == 30` 与 `delta == 0`，并且两趟 finalize
    都用"release 不放行"的同一形状，保证第二次进的还是同一个幂等键。
    """
    clock = _freeze_clock(monkeypatch)
    orchestrator = _orchestrator()
    wid = _running_workspace(clock)
    failing = FailingReleaseScheduler(orchestrator.scheduler)
    orchestrator.scheduler = failing  # 两趟都不放行 ⇒ 段从未被干净收尾 ⇒ 第二趟就是同一段重放

    start = counter_value()
    with Factory() as db, pytest.raises(RuntimeError, match="GPU release failure"):
        orchestrator._finalize_stop(db, db.get(Workspace, wid))
    after_first = counter_value()
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.started_at is not None, "前提塌了：第一段把 started_at 清了，第二次不是重放"
        assert len(_usage_rows(db, wid)) == 1

    clock.advance(60)  # elapsed 变成 90：重算出来的段长不许进 counter
    with Factory() as db, pytest.raises(RuntimeError, match="GPU release failure"):
        orchestrator._finalize_stop(db, db.get(Workspace, wid))
    after_second = counter_value()

    assert after_first - start == SEGMENT_SECONDS, (
        f"第一次 finalize 加了 {after_first - start}，应为账本认下的 {SEGMENT_SECONDS}"
    )
    assert after_second - after_first == 0, (
        f"同一段第二次 finalize 又加了 {after_second - after_first}"
        f"（elapsed 已变 90、账本仍只有一行 {SEGMENT_SECONDS} 秒）"
    )

    # 第三趟才干净收尾。这里同时钉两根读数的分工：`booked` 仍是账本段值（hold 用它），
    # `delta` 是 0（counter 用它）。改前那一臂在上面就已经数值开火了，不必等到这一步。
    booked, delta = None, None
    with Factory() as db:
        ws = db.get(Workspace, wid)
        orchestrator.scheduler = failing.inner
        booked, delta = orchestrator._settle_run_delta(db, ws)
        orchestrator._finalize_stop(db, ws)

    assert booked == SEGMENT_SECONDS, (
        f"_settle_run_delta 的第一根读数不再是账本认下的段值：{booked}"
    )
    assert delta == 0, f"重放同一段的账本增量应为 0，实读 {delta}"
    with Factory() as db:
        assert len(_usage_rows(db, wid)) == 1, "重放多扣了钱"
        assert (
            counter_value() - start
            == orchestrator.ledger.settled_gpu_seconds(db, wid)
            == _ledger_gpu_seconds(db, wid)
            == SEGMENT_SECONDS
        ), f"counter 增量 {counter_value() - start} 与账本分叉"


# ---------------------------------------------------------------------------
# T3 连续两段：不得把第一次的 delta 缓存下来
# ---------------------------------------------------------------------------


def test_two_consecutive_segments_sum_into_the_counter(monkeypatch):
    """结算 30 秒段 → 换新 `started_at` 再结算 45 秒段：counter 两次都要动，合计 75。

    防的是"delta 只算一次就记住"（那会让第二段漏计）与"每次加账本总和"（翻倍）。
    这一条改前也通过（每段都 finalize 一次，段值恰好等于增量）——它是改后的护栏。
    """
    clock = _freeze_clock(monkeypatch)
    orchestrator = _orchestrator()
    wid = _running_workspace(clock)

    start = counter_value()
    with Factory() as db:
        orchestrator._finalize_stop(db, db.get(Workspace, wid))
    after_first = counter_value()

    with Factory() as db:  # 下一段：新的 started_at，新的幂等键
        ws = db.get(Workspace, wid)
        ws.status = WorkspaceStatus.RUNNING.value
        ws.started_at = clock.now - timedelta(seconds=45)
        db.commit()
    with Factory() as db:
        orchestrator._finalize_stop(db, db.get(Workspace, wid))
    after_second = counter_value()

    assert after_first - start == SEGMENT_SECONDS
    assert after_second - after_first == 45, (
        f"第二段只加了 {after_second - after_first}：delta 被缓存或漏算"
    )
    with Factory() as db:
        assert sorted(r.gpu_seconds for r in _usage_rows(db, wid)) == [30, 45]
        assert (
            after_second - start
            == _ledger_gpu_seconds(db, wid)
            == orchestrator.ledger.settled_gpu_seconds(db, wid)
            == 75
        ), f"counter={after_second - start} 账本={_ledger_gpu_seconds(db, wid)}"


# ---------------------------------------------------------------------------
# T4 零秒段
# ---------------------------------------------------------------------------


def test_zero_second_segment_increments_nothing(monkeypatch):
    """紧接着已入账的一段之后 finalize 一个 0 秒段：账本不写行，counter 也不动。

    标注：这一条**改前也通过**（`settle_workspace_run` 对 `seconds <= 0` 返回 None，
    `booked` 已是 0），所以它是护栏、不是本轮的反证。反证在 T1/T2/T5。
    """
    clock = _freeze_clock(monkeypatch)
    orchestrator = _orchestrator()
    wid = _running_workspace(clock)

    start = counter_value()
    with Factory() as db:
        orchestrator._finalize_stop(db, db.get(Workspace, wid))
    after_first = counter_value()

    with Factory() as db:  # 刚开就跑完（elapsed < 1s → run_seconds=0）
        ws = db.get(Workspace, wid)
        ws.status = WorkspaceStatus.RUNNING.value
        ws.started_at = clock.now
        db.commit()
    with Factory() as db:
        orchestrator._finalize_stop(db, db.get(Workspace, wid))

    assert after_first - start == SEGMENT_SECONDS
    assert counter_value() - after_first == 0, "0 秒段动了 counter"
    with Factory() as db:
        assert len(_usage_rows(db, wid)) == 1, "0 秒段写出了额外的账本行"
        assert _ledger_gpu_seconds(db, wid) == orchestrator.ledger.settled_gpu_seconds(
            db, wid
        ) == SEGMENT_SECONDS
        assert counter_value() - start == _ledger_gpu_seconds(db, wid)


# ---------------------------------------------------------------------------
# T5 reconcile_all 路：level-triggered 的对账同样能重放
# ---------------------------------------------------------------------------


def test_reconcile_all_path_increments_once_even_when_it_replays(monkeypatch):
    """实测结论：reconcile 这条路**确实**会重放（不是假设）。

    形状：STOPPING + provider 说 ALIVE → `_stop_cleanup` → 结算(+30) → release 抛错 →
    本轮 stopped=0、状态留 STOPPING、`started_at` 未清；下一轮 provider 说 MISSING →
    :720 直接 `_finalize_stop` → 同一段再结算一次。改前这里也是 +30/+30 = 60 vs 账本 30
    （`/tmp/n75_probe.py` B 段实测）。
    """
    clock = _freeze_clock(monkeypatch)
    provider = StatefulProvider()
    orchestrator, wid = _provisioned(clock, provider=provider)
    failing = FailingReleaseScheduler(orchestrator.scheduler)
    orchestrator.scheduler = failing

    with Factory() as db:
        ws = db.get(Workspace, wid)
        ws.status = WorkspaceStatus.STOPPING.value
        db.commit()

    start = counter_value()
    first = orchestrator.reconcile_all()
    after_first = counter_value()
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPING.value, ws.status
        assert ws.started_at is not None, "前提塌了：这一轮把段清了，下一轮不算重放"
    assert first["stopped"] == 0, first
    assert after_first - start == SEGMENT_SECONDS, f"第一轮 reconcile 加了 {after_first - start}"

    orchestrator.scheduler = failing.inner
    second = orchestrator.reconcile_all()
    after_second = counter_value()
    assert second["stopped"] == 1, second
    assert after_second - after_first == 0, (
        f"reconcile 再来一趟把同一段又加了 {after_second - after_first}"
    )
    with Factory() as db:
        assert len(_usage_rows(db, wid)) == 1
        assert (
            after_second - start
            == orchestrator.ledger.settled_gpu_seconds(db, wid)
            == _ledger_gpu_seconds(db, wid)
            == SEGMENT_SECONDS
        )
        assert db.get(Workspace, wid).status == WorkspaceStatus.STOPPED.value


# ---------------------------------------------------------------------------
# T6 结构判据：counter 只许被 inc，且不许收到负数
# ---------------------------------------------------------------------------


def _negative_literal(node: ast.AST) -> bool:
    """字面负数：`-5` 或常量 `-5`。变量（`inc(delta)`）静态判不出来，那是 T1–T5 的责任。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(
        node.value, bool
    ):
        return node.value < 0
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and node.operand.value > 0
    )


def counter_misuses(source: str) -> list[str]:
    """点名"COUNTER_NAME 上调了 inc 以外的方法"与"inc/record 收到负字面量"。

    三条从句：
      1) `GPU_SECONDS.set(...)` / `.dec(...)` —— Counter 对象上根本没有这两个名字
         （实测 prometheus_client 0.26.0：`hasattr(GPU_SECONDS,'set')` 为假），写出来即缺陷；
      2) `GPU_SECONDS.inc(<负字面量>)`；
      3) `record_gpu_seconds(<负字面量>)` —— 调用位送负数与 (2) 同罪。
    文档字符串里的复述不是实现（`ast.Constant`，不产生 Attribute/Call 节点）。
    """
    tree = ast.parse(source)
    out: list[str] = []
    for node in ast.walk(tree):
        bare_counter_attr = isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        if bare_counter_attr and node.value.id == COUNTER_NAME and node.attr != "inc":
            out.append(f"{node.lineno}: {COUNTER_NAME}.{node.attr} —— counter 只许 inc")
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        inc_on_counter = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == COUNTER_NAME
            and func.attr == "inc"
        )
        recordered = isinstance(func, ast.Name) and func.id == RECORDER_NAME
        if (inc_on_counter or recordered) and _has_negative_arg(node):
            target = f"{COUNTER_NAME}.inc" if inc_on_counter else RECORDER_NAME
            out.append(f"{node.lineno}: {target} 收到负字面量")
    return out


def _has_negative_arg(call: ast.Call) -> bool:
    """位置或关键字实参里出现负字面量（`inc(delta)` 这种非字面量判不出来，见 docstring）。"""
    values = list(call.args) + [k.value for k in call.keywords if k.value is not None]
    return any(_negative_literal(v) for v in values)


def counter_reference_sites(source: str) -> list[int]:
    """这份源码里"碰到这根 counter / 这个 recorder"的行号；空表说明尺子看不见语料。

    `ast.walk` 是层序不是行序，所以返回前按行号排一遍——不然对照臂的 `[1, 2]` 会随机读成 `[2, 1]`。
    """
    tree = ast.parse(source)
    sites: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == COUNTER_NAME:
                sites.add(node.lineno)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            if name == RECORDER_NAME:
                sites.add(node.lineno)
    return sorted(sites)


def _app_sources() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_shipped_metric_code_never_writes_the_counter_backwards():
    """全仓 app/ 里 `GPU_SECONDS` 只被 inc、且没有任何一处收到负字面量。

    同一趟扫描要求真的读到过引用点（`sites`）：一条什么都找不到的判据与恒真同形。
    """
    offenders: list[str] = []
    sites: list[int] = []
    for path in _app_sources():
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        offenders.extend(f"{rel}:{item}" for item in counter_misuses(src))
        sites.extend(counter_reference_sites(src))
    assert sites, "在 app/ 里一个 GPU_SECONDS/record_gpu_seconds 引用都没读到：本判据恒真"
    assert offenders == [], f"这根 counter 被往回拨/整体赋值/喂了负数：{offenders}"


def test_the_counter_shape_ruler_names_every_misuse_shape_with_its_own_control():
    """每条从句各自一支必开火的对照；合规侧不许开火（否则上一条的绿不值钱）。"""
    # 从句 1：set
    assert [i for i in counter_misuses("GPU_SECONDS.set(5)\n") if ".set" in i], "set 没被点名"
    # 从句 1：dec
    assert [i for i in counter_misuses("GPU_SECONDS.dec(5)\n") if ".dec" in i], "dec 没被点名"
    # 从句 2：inc 负字面量（两种写法）
    assert counter_misuses("GPU_SECONDS.inc(-5)\n"), "inc(-5) 没被点名"
    assert counter_misuses("GPU_SECONDS.inc(0 - 5)\n") == []  # 不是字面量形态，静态判不了
    assert counter_misuses("GPU_SECONDS.inc(delta)\n") == []
    assert counter_misuses("GPU_SECONDS.inc(30)\n") == []
    # 从句 3：调用位送负数
    assert counter_misuses("record_gpu_seconds(-30)\n"), "record_gpu_seconds(-30) 没被点名"
    assert counter_misuses("record_gpu_seconds(delta_seconds=-1)\n"), "关键字负数没被点名"
    assert counter_misuses("record_gpu_seconds(delta)\n") == []
    # 复述不算实现
    assert counter_misuses('"""GPU_SECONDS.set(5) 是错的"""\n') == []
    # 锚点：看不见语料的尺子不能算通过
    assert counter_reference_sites("GPU_SECONDS.inc(30)\nrecord_gpu_seconds(delta)\n") == [1, 2]
    assert counter_reference_sites("x = 1\n") == []


def test_the_counter_is_incremented_at_exactly_one_site():
    """`record_gpu_seconds` 在 app/ 里恰好一个调用点。

    多一处调用就多一个"同一个段被计两次"的入口（本轮缺陷正是第二个入口——重试）；
    少一处说明指标没人报了。两处都按"红"处理，不默认第二个合法。
    """
    hits: list[str] = []
    for path in _app_sources():
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == RECORDER_NAME
            ):
                hits.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}")
    assert len(hits) == 1, f"counter 的抬升点应为恰好一处，实读 {hits}"
    assert hits[0].startswith("app/services/orchestrator.py:"), hits


def test_the_increment_site_ruler_counts_two_calls_as_two():
    """上一条的必开火对照：两个调用点要数出 2，定义与文档字符串里的复述不许算。"""

    def call_sites(source: str) -> list[int]:
        return [
            node.lineno
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == RECORDER_NAME
        ]

    assert call_sites("def record_gpu_seconds(x):\n    pass\n") == []
    assert call_sites('"""record_gpu_seconds(booked)"""\n') == []
    assert call_sites("record_gpu_seconds(delta)\n") == [1]
    assert len(call_sites("record_gpu_seconds(a)\nrecord_gpu_seconds(b)\n")) == 2


# ---------------------------------------------------------------------------
# T7 单调性：负数在运行时也被拒（不是只靠上面那把静态尺子）
# ---------------------------------------------------------------------------


def gpu_seconds_declaration(source: str) -> tuple[str, str] | None:
    """模块级 `GPU_SECONDS = <Kind>("<序列名>", …)` 读出 `(<Kind>, <序列名>)`。

    读不到那条赋值返回 None，由调用方判成"尺子看不见语料"而不是"合规"。
    """
    tree = ast.parse(source)
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        if not (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == COUNTER_NAME
        ):
            continue
        func = node.value.func
        kind = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        first = node.value.args[0] if node.value.args else None
        name = (
            first.value
            if isinstance(first, ast.Constant) and isinstance(first.value, str)
            else ""
        )
        return (str(kind), name)
    return None


def test_gpu_seconds_is_declared_as_a_counter_under_its_documented_name():
    """族的**类型**也得钉住：仓里的文档门只核族名与标签，看不见类型。

    实测（本轮 P2 探针，`app/metrics.py` 里把 `Counter(...)` 换成 `Gauge(...)`、名字与标签
    都不动）：`tests/test_observability.py` 与 `tests/test_metrics_spec.py` 全绿，而
    Gauge 上 `record_gpu_seconds(-30)` 静默通过（一手读数 "DID NOT RAISE ValueError"）
    —— 单调性当场没了。名称与标签那两格有 §8 的双向对账守着（改名实测红在
    `tests/test_observability.py:173`："文档写了代码里没有的族：['gpu_seconds_total']"），
    类型这一格由本判据补上。
    """
    decl = gpu_seconds_declaration((APP_DIR / "metrics.py").read_text(encoding="utf-8"))
    assert decl is not None, (
        f"在 app/metrics.py 里读不到 {COUNTER_NAME} 的模块级赋值：本判据与恒真同形"
    )
    assert decl == ("Counter", COUNTER_SERIES), (
        f"gpu_seconds 这一族不再是 Counter（实读 {decl}）；§8 的文档门看不见类型"
    )


def test_the_declaration_ruler_names_a_gauge_swapped_in():
    """上一条的必开火对照：换类型、改名、看不见赋值三种形状各自读数不同。"""
    assert gpu_seconds_declaration(
        "GPU_SECONDS = Gauge('gpu_seconds_total', 'd')\n"
    ) == ("Gauge", "gpu_seconds_total"), "换成 Gauge 应当被读成 Gauge（上一条据此开火）"
    assert gpu_seconds_declaration(
        "GPU_SECONDS = Counter('gpu_seconds', 'd')\n"
    ) == ("Counter", "gpu_seconds"), "改名应当被读出新名字"
    assert gpu_seconds_declaration(
        "GPU_SECONDS = Counter('gpu_seconds_total', 'd')\n"
    ) == ("Counter", COUNTER_SERIES)
    assert gpu_seconds_declaration("OTHER = Counter('gpu_seconds_total', 'd')\n") is None
    # 复述不是声明
    assert gpu_seconds_declaration('"""GPU_SECONDS = Gauge(1)"""\n') is None


def test_a_negative_increment_is_refused_and_moves_nothing():
    """`record_gpu_seconds(-30)` 必须抛，且抛完 counter 没动。

    不匹配库的报错文案（'Counters can only be incremented by non-negative amounts.' 是
    prometheus_client 0.26.0 的措辞，见 site-packages/metrics.py:341）——那是库的措辞、
    不是本仓契约；这里只认 ValueError 与"值没变"这两根事实。
    """
    start = counter_value()
    with pytest.raises(ValueError):
        record_gpu_seconds(-SEGMENT_SECONDS)
    assert counter_value() == start


def test_a_zero_increment_moves_nothing():
    """重放/空段走的就是这一档：`inc(0)` 合法且值不动（不夹负、不回拨）。"""
    start = counter_value()
    record_gpu_seconds(0)
    assert counter_value() == start


# ---------------------------------------------------------------------------
# T8 两个数没接反：hold 转正继续用账本段值
# ---------------------------------------------------------------------------


def test_hold_capture_still_uses_the_ledger_segment_value(monkeypatch):
    """release 失败+重试之后，hold 的 `captured_amount` 仍是账本认下的 30 秒。

    本轮把"账本增量"引入 `_settle_run_delta` 的第二个读数；接反（hold 用 delta）会让
    重放那一路把已消费的额度记成 0 秒 —— 与 N-74 的 C1 极和
    `tests/test_credit_holds.py::test_zero_second_run_still_closes_the_hold` 同一族。
    """
    clock = _freeze_clock(monkeypatch)
    billing = BillingPolicy(
        Factory, CreditLedgerService(Factory), enforce_preauthorization=True,
        minimum_launch_minutes=5, hold_ttl_minutes=30,
    )
    with Factory() as db:
        billing.ledger.record(
            db, type=LedgerType.RECHARGE, amount=10_000, user_id="u1", idempotency_key="t8-top"
        )
    orchestrator, wid = _provisioned(clock, billing=billing)
    with Factory() as db:
        hold = billing.reserve_launch(db, db.get(User, "u1"), wid, minutes=5)
        assert hold is not None and hold.status == HoldStatus.PENDING.value, (
            "前提未达成：hold 没圈上"
        )

    failing = FailingReleaseScheduler(orchestrator.scheduler)
    orchestrator.scheduler = failing
    start = counter_value()
    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))
    orchestrator.scheduler = failing.inner
    clock.advance(60)
    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))

    with Factory() as db:
        held = db.scalar(select(CreditHold).where(CreditHold.workspace_id == wid))
        assert held is not None and held.status == HoldStatus.CAPTURED.value
        assert held.captured_amount == SEGMENT_SECONDS, (
            f"hold 转正用的是 {held.captured_amount}，应仍是账本段值 {SEGMENT_SECONDS}"
        )
        assert counter_value() - start == _ledger_gpu_seconds(db, wid) == SEGMENT_SECONDS

"""`accumulated_seconds` 是账本的投影，不是写侧的累加（N-64）。

缺陷形状：`_settle_run` 无条件 `workspace.accumulated_seconds += run_seconds`，而
`settle_workspace_run` 的幂等键只保证"同一运行段不重复**扣款**"。可达路径（C2 就是照它
写的）：`_finalize_stop` 先结算、后才 `scheduler.release`，release 抛错时状态留在
STOPPING 且 `started_at` 不清零 ⇒ 下一趟重试对同一段再结算一次，而 `run_seconds` 是按
此刻流逝的时间重算的，只会更大。净效果：钱扣一次，计数器涨两次。

读这一列的人跟着一起错：`app/routers/usage.py:36`（展示用量）与 :40（折算金额）、
`app/services/billing.py:380` 的 `course_usage_seconds`（QUOTA 门禁，判据是 `used >=
quota_seconds`）、`app/static/app.js:425,703`。

修法取 `ledger.py` 模块 docstring 已有的教义（"balance 始终 = SUM(amount)"）：既然
balance 与 `organization_balance` 都是从账本派生的读数，GPU 秒数也该是 —— 于是新增
`CreditLedgerService.settled_gpu_seconds()` 作为 USAGE 行的唯一读数口径，`_settle_run`
结算后 **SET** 这一列而不是 `+=`，并返回账本实际认下的秒数。
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    Course,
    CreditLedger,
    Lab,
    LedgerType,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceStatus,
)
from app.services import orchestrator as orchestrator_module
from app.services.billing import BillingError, BillingPolicy
from app.services.ledger import CreditLedgerService
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.services.streaming import StreamingSessionService
from tests.dbfiles import db_url

ENGINE = create_engine(
    db_url("settled-projection"), connect_args={"check_same_thread": False}
)
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
# 运行段的长度：把 orchestrator 的时钟冻在"现在"、started_at 挪到 seconds 之前，
# 于是 `int((utcnow() - started).total_seconds())` 恰好等于它，两次重放也一样。
SEGMENT_SECONDS = 30


@pytest.fixture(autouse=True)
def _db():
    """每例重建表并灌入固定世界：一张够用的 mock 卡 + cartpole 模板 + 学生 u1 + lab-1。

    usage 行一条都不预写 —— 每条判据自己决定结算几次、结算多少，那正是本轮要断的事。
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
# 夹具
# ---------------------------------------------------------------------------


class Clock:
    """可推进的假时钟：`monkeypatch` 进 `orchestrator.utcnow` 后，结算秒数由它单独决定。"""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _freeze_clock(monkeypatch) -> Clock:
    """只冻 orchestrator 那一个名字。

    models/ledger 的 `created_at` 走各自 import 的 `utcnow`，不受影响；本轮判据量的
    就是"结算读数 vs 账本读数"，它只需要这一根时钟。
    """
    clock = Clock(datetime.now(UTC))
    monkeypatch.setattr(orchestrator_module, "utcnow", clock)
    return clock


def _orchestrator(billing: BillingPolicy | None = None) -> WorkspaceOrchestrator:
    return WorkspaceOrchestrator(
        Factory,
        MockProvider("http://127.0.0.1:8000"),
        Path("/tmp/test-settled-projection"),  # noqa: S108 测试隔离目录
        streaming=StreamingSessionService(Factory),
        ledger=CreditLedgerService(Factory),
        billing=billing,
    )


def _ledger_gpu_seconds(db, workspace_id: str) -> int:
    """账本这一侧的独立合计（判据用它比对，不借被测方法自己的手）。

    与 `settled_gpu_seconds()` 同表同谓词：C1 末尾把两者对齐，所以这里不是把实现抄一遍
    当判据，而是让改前那份代码也能比出一条数（不必先有新方法才不崩）。
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


def _running_workspace(clock: Clock, *, accumulated_seconds: int = 0) -> str:
    """直接落一行 RUNNING workspace，`started_at` 在 clock 之前 SEGMENT_SECONDS 秒。

    写完就换会话复读：`started_at.isoformat()` 是幂等键的一部分，落库回读才是重放时
    真正拿到的那个串（SQLite 的 DATETIME 不带时区偏移，`+00:00` 形态不会进键）。
    """
    with Factory() as db:
        ws = Workspace(
            id="ws-seg",
            name="seg",
            template_id="cartpole",
            provider="mock",
            user_id="u1",
            status=WorkspaceStatus.RUNNING.value,
            started_at=clock.now - timedelta(seconds=SEGMENT_SECONDS),
            accumulated_seconds=accumulated_seconds,
        )
        db.add(ws)
        db.commit()
    return ws.id


def _settle_once(orchestrator, workspace_id: str) -> int:
    """按 `_settle_running_segment` 的形状结算一次（新会话 + 提交），返回它的读数。"""
    with Factory() as db:
        booked = orchestrator._settle_run(db, db.get(Workspace, workspace_id))
        db.commit()
    return booked


class FailingReleaseScheduler:
    """只把 `release` 换成抛错，其余全转给内层（N-63 那份替身的同一形状）。

    要的就是"结算已落库、release 失败、状态留 STOPPING、`started_at` 未清"这一档。
    """

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def release(self, db, workspace_id):
        self.calls += 1
        raise RuntimeError("simulated: GPU release failure")

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _provisioned_segment(monkeypatch):
    """真走 provision 到 RUNNING（GPU 由 scheduler 原子分配出来），再把运行段挪到过去。

    返回 (orchestrator, billing, workspace_id, clock)：release 失败那两档判据要从公开
    的 `stop()` 进去，所以卡必须是真占着的，否则 `release` 抛不抛都没人在等它。
    """
    clock = _freeze_clock(monkeypatch)
    billing = BillingPolicy(Factory, CreditLedgerService(Factory))
    orchestrator = _orchestrator(billing=billing)
    with Factory() as db:
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = ws.id
    orchestrator._start(wid)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value, (
            f"前提未达成：provision 没把 workspace 带到 RUNNING（status={ws.status}）"
        )
        ws.started_at = clock.now - timedelta(seconds=SEGMENT_SECONDS)
        db.commit()
    return orchestrator, billing, wid, clock


def _stop_until_release_fails(monkeypatch):
    """跑一遍登记项读到的那条路：第一次 stop 结算成功、release 抛错；第二次真收尾。"""
    orchestrator, billing, wid, clock = _provisioned_segment(monkeypatch)
    failing = FailingReleaseScheduler(orchestrator.scheduler)
    orchestrator.scheduler = failing

    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))
    assert failing.calls == 1
    with Factory() as db:
        ws = db.get(Workspace, wid)
        # 前提（不是结论）：这一段结算过了但没收尾，所以重试还会对它再结算一次
        assert ws.status == WorkspaceStatus.STOPPING.value
        assert ws.started_at is not None
        assert len(_usage_rows(db, wid)) == 1

    orchestrator.scheduler = failing.inner
    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))
    with Factory() as db:
        assert db.get(Workspace, wid).status == WorkspaceStatus.STOPPED.value
    return orchestrator, billing, wid, clock


# ---------------------------------------------------------------------------
# C1 重放：同一段结算两次
# ---------------------------------------------------------------------------


def test_replay_of_one_segment_books_one_row_and_the_column_follows_it(monkeypatch):
    """同一段（同 `started_at`）结算两次、第二次时钟更晚：账本一行、列=那一行、返回值也是它。

    改前这一条两处开火：列变成 30+90=120（`+=` 跟着 elapsed 涨），且第二次返回 90 ——
    一个账本从没认过的数，还被拿去做指标与 hold 转正。
    """
    clock = _freeze_clock(monkeypatch)
    orchestrator = _orchestrator()
    wid = _running_workspace(clock)

    assert _settle_once(orchestrator, wid) == SEGMENT_SECONDS

    clock.advance(60)  # 崩溃重放/重试：中间又过了 60 秒
    second = _settle_once(orchestrator, wid)

    with Factory() as db:
        rows = _usage_rows(db, wid)
        assert len(rows) == 1, f"重放多扣了钱：{len(rows)} 行 USAGE"
        assert rows[0].gpu_seconds == SEGMENT_SECONDS
        ws = db.get(Workspace, wid)
        assert ws.accumulated_seconds == rows[0].gpu_seconds, (
            f"累计列（{ws.accumulated_seconds}）与账本唯一一行（{rows[0].gpu_seconds}）分叉"
        )
        assert _ledger_gpu_seconds(db, wid) == SEGMENT_SECONDS
        assert orchestrator.ledger.settled_gpu_seconds(db, wid) == _ledger_gpu_seconds(db, wid)
    assert second == SEGMENT_SECONDS, (
        f"_settle_run 返回的是重放时重算的 elapsed（{second}），不是账本认下的数"
    )


# ---------------------------------------------------------------------------
# C2 可达形状：release 失败后的重试
# ---------------------------------------------------------------------------


def test_release_failure_retry_keeps_the_column_at_the_ledger_value(monkeypatch):
    """登记项读到的那条路：`_finalize_stop` 先结算、release 才抛错 → 重试不得让列翻倍。

    断的是 `accumulated_seconds == 账本 SUM`（钱仍只扣一次：USAGE 恰好一行）。改前这里
    是两段 `run_seconds` 之和（60），而账本只有 30 —— 扣一次、显示与配额两次。
    """
    _, _billing, wid, _clock = _stop_until_release_fails(monkeypatch)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        rows = _usage_rows(db, wid)
        assert len(rows) == 1, "重试把同一段又扣了一次"
        assert ws.accumulated_seconds == _ledger_gpu_seconds(db, wid) == SEGMENT_SECONDS, (
            f"列={ws.accumulated_seconds}、账本={_ledger_gpu_seconds(db, wid)}"
        )


# ---------------------------------------------------------------------------
# C3 自愈：投影是 SET，不是 patch
# ---------------------------------------------------------------------------


def test_projection_overwrites_a_pre_existing_inflated_counter(monkeypatch):
    """列上先写着 999（账本一行都没有）：结算 30 秒之后列恰好是 30。

    这是"修好的那一极"——投影由账本重算得出，所以先前被 `+=` 吹起来的值是**被纠正**
    而不是被保留：旧实现只会加，没有任何一条路把它拉回账本的数。
    """
    clock = _freeze_clock(monkeypatch)
    orchestrator = _orchestrator()
    wid = _running_workspace(clock, accumulated_seconds=999)
    assert _settle_once(orchestrator, wid) == SEGMENT_SECONDS

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.accumulated_seconds == SEGMENT_SECONDS, (
            f"投影没有覆盖旧值：列={ws.accumulated_seconds}"
        )
        assert _ledger_gpu_seconds(db, wid) == SEGMENT_SECONDS


# ---------------------------------------------------------------------------
# C4 历史：两段都要在投影里
# ---------------------------------------------------------------------------


def test_consecutive_segments_both_survive_the_projection(monkeypatch):
    """连续两段（结算 A → 清 started_at → 给新的 started_at 结算 B）：列 = A + B。

    钉的是"从账本派生"别把历史段一起换掉：`settled_gpu_seconds` 跨行求和，所以 A 不会
    被 B 覆盖。这一条改前也通过（`+=` 天然保序）——它是改后的护栏，不是改前的反证。
    """
    clock = _freeze_clock(monkeypatch)
    orchestrator = _orchestrator()
    wid = _running_workspace(clock)
    assert _settle_once(orchestrator, wid) == SEGMENT_SECONDS

    # 下一段：`_finalize_stop` 的成功路径就是"结算后清 started_at"，按同一形状推进
    with Factory() as db:
        db.get(Workspace, wid).started_at = None
        db.commit()
    with Factory() as db:
        db.get(Workspace, wid).started_at = clock.now - timedelta(seconds=45)
        db.commit()

    assert _settle_once(orchestrator, wid) == 45

    with Factory() as db:
        rows = _usage_rows(db, wid)
        assert sorted(r.gpu_seconds for r in rows) == [30, 45]
        assert db.get(Workspace, wid).accumulated_seconds == 30 + 45
        assert _ledger_gpu_seconds(db, wid) == 75


# ---------------------------------------------------------------------------
# C5 一份写入：结构判据 + 必开火的对照
# ---------------------------------------------------------------------------


def accumulated_accumulations(source: str) -> list[int]:
    """列出源码里"对 `.accumulated_seconds` 做增强赋值"的行号。

    按 AST 判：`total += w.accumulated_seconds` 是往别的名字上加（那是读数侧），
    文档字符串里复述这句也不算一份实现。
    """
    hits = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Attribute)
            and node.target.attr == "accumulated_seconds"
        ):
            hits.append(node.lineno)
    return hits


def _app_sources() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_write_side_accumulates_the_projection_column():
    """全仓 app/ 里不许再有任何 `x.accumulated_seconds += …`；投影读数只有一份定义。

    改前那份就在 `orchestrator.py::_settle_run` 里（`workspace.accumulated_seconds +=
    run_seconds`）——只要它还在，幂等的账本与不幂等的计数器就迟早再分叉一次。
    """
    offenders: list[str] = []
    defined = 0
    for path in _app_sources():
        src = path.read_text(encoding="utf-8")
        defined += src.count("def settled_gpu_seconds(")
        offenders.extend(
            f"{path.relative_to(REPO_ROOT)}:{line}" for line in accumulated_accumulations(src)
        )
    assert offenders == [], f"累计列又回到写侧累加：{offenders}"
    assert defined == 1, f"`settled_gpu_seconds` 该恰好一份定义，实际 {defined}"


def test_the_accumulation_counter_can_see_the_old_shape():
    """反向对照：改前那一行喂进同一把尺子必须点名，两种合法写法不许开火。

    没有这一支，上一条在"尺子什么都数不到"时会一直绿。
    """
    assert accumulated_accumulations("ws.accumulated_seconds += run_seconds\n") == [1]
    assert accumulated_accumulations("total += ws.accumulated_seconds or 0\n") == []
    assert accumulated_accumulations('"""ws.accumulated_seconds += run_seconds"""\n') == []


# ---------------------------------------------------------------------------
# C6 读者一致
# ---------------------------------------------------------------------------


def test_the_quota_gate_reports_the_booked_seconds(monkeypatch):
    """读者对齐：QUOTA 门禁（`billing.course_usage_seconds`）报的数 == 账本 SUM。

    覆盖的是 `app/services/billing.py:380` 那一位读者——它读的就是本轮改成投影的这一列，
    并且真的拿它拒启动（配额边界两个极点一起钉：`used >= quota` 拒、差一秒放行）。
    世界用 C2 那条 release 失败+重试的形状造，所以改前它读到的是账本的两倍。
    不覆盖：`app/routers/usage.py:36,40` 的 HTTP 展示算术与 `app/static/app.js:425,703`
    ——同一条 `accumulated_seconds + live`，要走真 HTTP（鉴权夹具）才量得到；且那条路
    上"仍在 RUNNING 而这一段已结算过"的窗口里 live 仍会在投影之外再加一次，那是 N-64
    的另一半，本轮设计没有动它。
    """
    orchestrator, billing, wid, _clock = _stop_until_release_fails(monkeypatch)

    with Factory() as db:
        # 先给钱包进钱：这里要量的是配额那一句。结算已经把余额压成 -30，
        # 不补的话 `check_launch_eligible` 会在"insufficient credits"那一步就返回。
        billing.ledger.record(
            db,
            type=LedgerType.RECHARGE,
            amount=10_000,
            user_id="u1",
            idempotency_key="c6-top",
        )

    with Factory() as db:
        lab = db.get(Lab, "lab-1")
        used = billing.course_usage_seconds(db, "u1", lab)
        assert used == _ledger_gpu_seconds(db, wid) == SEGMENT_SECONDS, f"门禁看到 {used}"

        student = db.get(User, "u1")
        template = db.get(Template, "cartpole")
        lab.quota_seconds = SEGMENT_SECONDS
        with pytest.raises(BillingError, match="quota exhausted"):
            billing.check_launch_eligible(db, student, template, lab=lab)
        lab.quota_seconds = SEGMENT_SECONDS + 1
        billing.check_launch_eligible(db, student, template, lab=lab)  # 差一秒仍放行
        # 门禁用的就是投影列：结算过的段清掉 started_at 后不再有 live 项
        assert db.get(Workspace, wid).started_at is None
        assert orchestrator.ledger.settled_gpu_seconds(db, wid) == used


# ---------------------------------------------------------------------------
# C7 零秒段
# ---------------------------------------------------------------------------


def test_zero_second_segment_books_nothing_and_keeps_the_history(monkeypatch):
    """紧接着已入账的一段之后结算一个 0 秒段：不写行、投影仍是那一段的数、返回 0。

    钉的是 `entry is None` 那一支——`settle_workspace_run` 对 `seconds <= 0` 返回 None，
    于是"账本认下的数"没有条目可取。这一支写错（例如直接取 `entry.gpu_seconds`）会把
    STOP/destroy 整条收尾路抛出去，被 `_stop_cleanup` 转成"finalize failed"字符串而
    workspace 永远停在 STOPPING；现存的 `test_credit_holds.py::test_zero_second_run_still_closes_the_hold`
    只量 hold 那一侧，所以这里补投影与返回值。
    """
    clock = _freeze_clock(monkeypatch)
    orchestrator = _orchestrator()
    wid = _running_workspace(clock)
    assert _settle_once(orchestrator, wid) == SEGMENT_SECONDS

    # 下一段刚开就跑完（elapsed < 1s → run_seconds=0）
    with Factory() as db:
        db.get(Workspace, wid).started_at = clock.now
        db.commit()
    assert _settle_once(orchestrator, wid) == 0

    with Factory() as db:
        rows = _usage_rows(db, wid)
        assert len(rows) == 1, f"0 秒段写出了额外的账本行：{len(rows)}"
        assert db.get(Workspace, wid).accumulated_seconds == SEGMENT_SECONDS, (
            "投影被空段清零/改大了"
        )

"""live 项不许把已经入账的运行段再计一次（N-64 的另一半：读者侧加在投影上的那个数）。

缺陷形状（复现读数：QUOTA 门禁读到 60，同一 workspace 的账本 SUM 只有 30）：
`workspaces.accumulated_seconds` 是账本的**投影**——N-64 之后每次结算把它 SET 成
`ledger.settled_gpu_seconds()`（`app/services/orchestrator.py:563`），仓里也有一条常驻
AST 规则禁止任何 `x.accumulated_seconds += …`。之后两个读者还在投影之外再加一次
"当前这一段"的墙钟秒（两处行号都是**改前**形状，本轮已把它们换成一次 `workspace_seconds_used`
调用；改前 base = `9665b1e`）：

- 改前 `app/routers/usage.py:31-36` —— 喂 :39-42 的折算金额与 :50 的 `accumulated_gpu_seconds`
- 改前 `app/services/billing.py:395-400` 的 `course_usage_seconds` —— QUOTA 门禁
  （`used >= lab.quota_seconds` ⇒ `course quota exhausted`，`billing.py:88-92`）与
  配额监控的停机判定（`app/services/orchestrator.py:854-857`）都吃它

可达路径（本模块的夹具就照它造）：`destroy()` 先结算（`orchestrator.py:615` →
`_settle_running_segment` :594-598，内部已提交），随后 `provider.destroy` 在
:623-629 抛错并原样上抛给 DESTROY 重试（注释就写着"上抛让 DESTROY 重试"）——
workspace 留在 RUNNING、那一段已经进账本、`started_at` 仍然有值。两头各算一次 ⇒ 60。

修法（本模块只认这一条口径）：一段运行的身份就是它的账本幂等键
（`CreditLedger.idempotency_key` 带 `unique=True`，`app/models.py:386`；全仓只有一处
可执行构造 —— `app/services/ledger.py` 的 `usage_idempotency_key`，写侧喂的是**原始**
列值 `workspace.started_at.isoformat()`）。于是"这一段入账了吗"是一次点查：
`CreditLedgerService.segment_booked`。"已用秒数"收敛成 `workspace_seconds_used`
（投影 + 仅当本段未入账的 live），两个读者都调它。

判据 C1–C6 见下面各节。本模块**取代**常驻
`tests/test_settled_projection.py:454-457` 里那句"（GET /api/usage 的展示算术）……
本轮设计没有动它"：N-64 那一轮把 `app/routers/usage.py` 显式排除在覆盖面之外，
本轮把它请回判据面（那条用例本身不改、仍然绿）。

第三个读者在浏览器里：`app/static/app.js` 自己算 `accumulated_seconds + live`（两秒一轮
重新渲染，数字要接着跳），所以服务端那句口径管不到它，管得到的是**谓词**。
`WorkspaceOut` 因此多带一个 `usage_segment_booked`：赋值位点全仓唯一
（`ledger.mark_usage_segments`），每个返回 WorkspaceOut 的路由都要带上它，前端两处
live 只在它为假时才加——这三条各有一支尺（C6b/C6d/C6f），两极各有一支 HTTP（C6a）。

两条陷阱也各自有判据钉住：键**不做时区归一**（C5b：归一化串与落库回读串是两把键，
点查必然落空 ⇒ live 照加，双计原样留着），亚秒段**不写账本行**
（`ledger.py` 对 `seconds <= 0` 返回 None，所以"行不存在"就是正确谓词 ⇒ live 必须照加，
C2b；`tests/test_settled_projection.py:513-518` 钉的投影那一侧不许翻）。
"""

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, func, select, update
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
from app.services import ledger as ledger_module
from app.services import orchestrator as orchestrator_module
from app.services.billing import BillingError, BillingPolicy
from app.services.ledger import CreditLedgerService
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.services.streaming import StreamingSessionService
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("live-usage"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
SEGMENT_SECONDS = 30


@pytest.fixture(autouse=True)
def _db():
    """每例重建表并灌固定世界：一张够用的 mock 卡 + cartpole 模板 + 学生 u1 + lab-1。

    USAGE 行一条都不预写——每条判据自己决定结算几次、哪一段进了账本。
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


@pytest.fixture(autouse=True)
def _http_rows_torn_down():
    """收掉 HTTP 判据在 **app 自己的库** 里 seed 的行，别给后续用例留一行 RUNNING 的残骸。

    app 库（`tests/conftest.py` 设的 `EMBODIEDCLOUD_DATABASE_URL`）是全套件共用的一份，
    注册出来的 user 行按本仓惯例留着（其他模块也这么留），但我直接 INSERT 的 workspace/
    template 不在任何生命周期里：不收回就是"admin 列表多一行 RUNNING、reconcile 多扫一个
    不存在的模板"这种跨模块污染。按 FK 方向删（先引用方，再 workspace，再 template）。
    """
    yield
    from sqlalchemy import inspect as sa_inspect

    from app.deps import SessionFactory
    from app.models import CreditHold, Gpu, WorkspaceOperation

    with SessionFactory() as db:
        # app 库的表是 lifespan 建的：本模块的 HTTP 判据没跑过时这里什么都没收，
        # 直接退（照 tests/conftest.py 的 `has_table("gpus")` 同一写法）。
        if not sa_inspect(db.bind).has_table("workspaces"):
            return
        ws_ids = [w.id for w in db.scalars(select(Workspace).where(Workspace.id.like("ws-live-%")))]
        tpl_ids = [t.id for t in db.scalars(select(Template).where(Template.id.like("tpl-live-%")))]
        if ws_ids:
            db.execute(delete(WorkspaceOperation).where(WorkspaceOperation.workspace_id.in_(ws_ids)))
            db.execute(delete(CreditHold).where(CreditHold.workspace_id.in_(ws_ids)))
            db.execute(
                update(Gpu)
                .where(Gpu.workspace_id.in_(ws_ids))
                .values(workspace_id=None, status="available")
            )
            db.execute(delete(CreditLedger).where(CreditLedger.workspace_id.in_(ws_ids)))
            db.execute(delete(Workspace).where(Workspace.id.in_(ws_ids)))
        db.execute(delete(CreditLedger).where(CreditLedger.idempotency_key.like("live-usage-topup%")))
        if tpl_ids:
            db.execute(delete(Template).where(Template.id.in_(tpl_ids)))
        db.commit()


# ---------------------------------------------------------------------------
# 夹具
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


def _orchestrator(billing: BillingPolicy | None = None) -> WorkspaceOrchestrator:
    return WorkspaceOrchestrator(
        Factory,
        MockProvider("http://127.0.0.1:8000"),
        Path("/tmp/test-live-usage"),  # noqa: S108 测试隔离目录
        streaming=StreamingSessionService(Factory),
        ledger=CreditLedgerService(Factory),
        billing=billing,
    )


def _ledger_gpu_seconds(db, workspace_id: str) -> int:
    """账本这一侧的独立合计（不借被测方法自己的手）。"""
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


def _running_workspace(clock: Clock, *, wid: str = "ws-live", accumulated: int = 0) -> str:
    """落一行 RUNNING workspace，`started_at` 在 clock 之前 SEGMENT_SECONDS 秒（无 USAGE 行）。"""
    with Factory() as db:
        db.add(
            Workspace(
                id=wid,
                name="live",
                template_id="cartpole",
                provider="mock",
                user_id="u1",
                status=WorkspaceStatus.RUNNING.value,
                started_at=clock.now - timedelta(seconds=SEGMENT_SECONDS),
                accumulated_seconds=accumulated,
            )
        )
        db.commit()
    return wid


def _settle_once(orchestrator, workspace_id: str) -> int:
    """按 `_settle_running_segment` 的形状结算一次（新会话 + 提交）。"""
    with Factory() as db:
        booked = orchestrator._settle_run(db, db.get(Workspace, workspace_id))
        db.commit()
    return booked


class FailingDestroyProvider:
    """只把 `provider.destroy` 换成抛错，其余全转给内层——登记项读到的那一档窗口。

    形状与 `tests/test_provision_rollback.py::FailingDockerProvider.destroy`、
    `tests/test_settled_projection.py::FailingReleaseScheduler` 同一族：代理内层、
    只翻一个方法，好让"结算已提交、清理失败上抛、状态没走"这一段是真跑出来的。
    """

    def __init__(self, inner):
        self.inner = inner
        self.destroy_calls = 0

    def destroy(self, workspace):
        self.destroy_calls += 1
        raise RuntimeError("simulated: provider destroy failure")

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _destroy_fails_after_settlement(monkeypatch) -> tuple[WorkspaceOrchestrator, BillingPolicy, str, Clock]:
    """真走 provision → 真走 destroy，把双计窗口建出来。

    返回 (orchestrator, billing, workspace_id, clock)。前提（不是结论）在函数尾部断言：
    那一段已进账本（1 行 30 秒）、状态仍是 RUNNING、`started_at` 未清。
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
        # 把运行段挪到过去（真实 provision 也是新会话读回，started_at 已是落库形状）
        ws.started_at = clock.now - timedelta(seconds=SEGMENT_SECONDS)
        db.commit()

    failing = FailingDestroyProvider(orchestrator.provider)
    orchestrator.provider = failing
    # worker 执行 DESTROY 用的就是这种"新会话里读回来的 workspace"
    with Factory() as db, pytest.raises(RuntimeError, match="provider destroy failure"):
        orchestrator.destroy(db, db.get(Workspace, wid))
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert failing.destroy_calls == 1, "provider.destroy 没被走到，窗口不是登记项那条"
        assert ws.status == WorkspaceStatus.RUNNING.value, (
            f"前提未达成：destroy 失败后状态没留在 RUNNING（{ws.status}）"
        )
        assert ws.started_at is not None, "前提未达成：started_at 被清了，live 项本来就没了"
        assert len(_usage_rows(db, wid)) == 1, "前提未达成：那一段没进账本，谈不上双计"
        assert ws.accumulated_seconds == _ledger_gpu_seconds(db, wid) == SEGMENT_SECONDS, (
            "前提未达成：投影不等于账本 SUM"
        )
    return orchestrator, billing, wid, clock


def _top_up(billing: BillingPolicy, amount: int = 10_000) -> None:
    """给钱包进钱：要量的是配额那一句，不能让它先撞在 insufficient credits 上。"""
    with Factory() as db:
        billing.ledger.record(
            db,
            type=LedgerType.RECHARGE,
            amount=amount,
            user_id="u1",
            idempotency_key=f"c-topup-{amount}",
        )


def _head_key(workspace_id: str, started_at_iso: str) -> str:
    """HEAD（`9665b1e`）`app/services/ledger.py:137` 那处裸 f-string 的原样写法。

    判据用它当"旧身份"的参照：本轮把键收进模板函数之后，产出的串必须与它逐字节相同，
    否则已入账的行了身份。
    """
    return f"usage:{workspace_id}:{started_at_iso}"


def _authority(name: str):
    """取本轮设计新增的口径本身（不从顶层 import，好让改前那一臂红得有名字）。"""
    got = getattr(ledger_module, name, None)
    assert got is not None, (
        f"缺少唯一口径 `app/services/ledger.py::{name}`：权威定义不在场，读者无处可去"
    )
    return got


def _booked_service() -> CreditLedgerService:
    service = CreditLedgerService(Factory)
    assert callable(getattr(service, "segment_booked", None)), (
        "缺少 `CreditLedgerService.segment_booked`：读者侧唯一的入账判定点不在场"
    )
    return service


# ---------------------------------------------------------------------------
# C1 缺陷形状：已入账的一段不许再加 live
# ---------------------------------------------------------------------------


def test_quota_gate_reports_the_ledger_sum_in_the_destroy_window(monkeypatch):
    """C1a destroy 失败窗口里 `course_usage_seconds` == 账本 SUM == 30（改前是 60）。

    取代 `tests/test_settled_projection.py:454-457` 把这一位读者排除掉的措辞
    （"本轮设计没有动它"）：同一个窗口、同一位数，本轮起它是判据面的一部分。
    """
    _orch, billing, wid, _clock = _destroy_fails_after_settlement(monkeypatch)
    _top_up(billing)

    with Factory() as db:
        booked = _ledger_gpu_seconds(db, wid)
        assert booked == SEGMENT_SECONDS, f"账本 SUM 不是 {SEGMENT_SECONDS}，窗口没建对"
        used = billing.course_usage_seconds(db, "u1", db.get(Lab, "lab-1"))
        assert used == booked == SEGMENT_SECONDS, (
            f"QUOTA 门禁读到 {used}，账本 SUM 只有 {booked}：已入账的段被又加了一次 live"
        )


def test_quota_boundary_both_poles_in_the_destroy_window(monkeypatch):
    """C1b 配额边界两个极点：`used >= quota` 拒、差一秒放行（窗口里 used 是 30 不是 60）。

    极点必须成对：只钉"拒"那一支，实现把 live 整个砍掉（漏计）也会一样绿。
    这里不把 `used == 30` 再前置断言一遍（那是 C1a 的活），好让改前那一臂红在**边界本身**：
    used=60 时 `quota = 31` 那一支也会拒，正是"多计一秒就把人挡在门外"的形状。
    """
    _orch, billing, _wid, _clock = _destroy_fails_after_settlement(monkeypatch)
    _top_up(billing)

    with Factory() as db:
        student = db.get(User, "u1")
        template = db.get(Template, "cartpole")
        lab = db.get(Lab, "lab-1")

        lab.quota_seconds = SEGMENT_SECONDS  # used 恰好等于配额 ⇒ 拒
        with pytest.raises(BillingError, match="quota exhausted"):
            billing.check_launch_eligible(db, student, template, lab=lab)

        lab.quota_seconds = SEGMENT_SECONDS + 1  # 差一秒 ⇒ 必须放行
        try:
            billing.check_launch_eligible(db, student, template, lab=lab)
        except BillingError as exc:  # 把"读到几秒"写进失败消息，别只留一句 raise
            pytest.fail(
                f"配额还剩 1 秒却被拒（{exc}）：窗口里 live 把已入账的 {SEGMENT_SECONDS}s 又算了一遍"
            )


def _http_running_workspace(
    *,
    email: str,
    ws_id: str,
    tpl_id: str,
    recharge_key: str,
    started_at: datetime,
    accumulated_seconds: int = 0,
) -> str:
    """在 app 自己的库（`app.deps.SessionFactory`）里 seed 一个 RUNNING workspace。

    库是全套件共用的一份（`tests/conftest.py` 设的 `EMBODIEDCLOUD_DATABASE_URL`），所以
    模板名/workspace 名/充值幂等键都由调用方给**本模块专用**的值，两个 HTTP 判据各用一套，
    否则第二条会在主键上撞第一行。
    app 库里的其他 lab 与这个专用模板无关，所以配额监控（`orchestrator.py:851-857`）在
    TestClient 期间扫不到它；再给钱包进钱，它就不会在这一条判据的亚秒窗口里把 workspace
    停掉（否则判据变概率）。
    """
    from app.deps import SessionFactory
    from app.deps import orchestrator as app_orchestrator

    with SessionFactory() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None, "前提未达成：注册没落库"
        db.add(
            Template(
                id=tpl_id,
                slug=tpl_id,
                name=tpl_id,
                description="probe",
                category="probe",
                runtime="mock",
                launch_command="echo ok",
                enabled=True,
                recommended_vram_gb=16,
                estimated_hourly_cost_cny=2.0,
            )
        )
        db.add(
            Workspace(
                id=ws_id,
                name="live-http",
                template_id=tpl_id,
                provider="mock",
                user_id=user.id,
                status=WorkspaceStatus.RUNNING.value,
                started_at=started_at,
                accumulated_seconds=accumulated_seconds,
            )
        )
        db.commit()
        app_orchestrator.billing.ledger.record(
            db,
            type=LedgerType.RECHARGE,
            amount=100_000,
            user_id=user.id,
            idempotency_key=recharge_key,
        )
    return ws_id


def test_usage_endpoint_reports_the_ledger_sum_in_the_destroy_window(monkeypatch):
    """C1c GET /api/usage 走的真 HTTP 面：同一个窗口里 `accumulated_gpu_seconds` == 账本 SUM。

    这一条用 app 自己的组合根（`app.deps`）跑 TestClient，量的是
    `app/routers/usage.py` 的展示算术与折算金额——常驻 N-64 那一轮显式没覆盖的那一位。
    """
    from fastapi.testclient import TestClient

    from app.deps import SessionFactory
    from app.deps import orchestrator as app_orchestrator
    from app.main import app
    from tests.http_auth import auth_headers, register_body

    clock = _freeze_clock(monkeypatch)
    with TestClient(app) as client:
        token = register_body(client, "live-usage@example.com", "live-usage")["token"]
        headers = auth_headers(token)
        wid = _http_running_workspace(
            email="live-usage@example.com",
            ws_id="ws-live-booked",
            tpl_id="tpl-live-booked",
            recharge_key="live-usage-topup-booked",
            started_at=clock.now - timedelta(seconds=SEGMENT_SECONDS),
        )

        # 组合根上的单例：必须用 monkeypatch 换 provider，否则"destroy 永远抛错"这份
        # 替身会漏给同一进程里后续所有用例（pytest teardown 自动还原）。
        monkeypatch.setattr(
            app_orchestrator, "provider", FailingDestroyProvider(app_orchestrator.provider)
        )
        with SessionFactory() as db, pytest.raises(RuntimeError, match="provider destroy failure"):
            app_orchestrator.destroy(db, db.get(Workspace, wid))

        with SessionFactory() as db:
            booked = _ledger_gpu_seconds(db, wid)
            ws = db.get(Workspace, wid)
            assert booked == SEGMENT_SECONDS, f"账本 SUM 不是 {SEGMENT_SECONDS}（{booked}）"
            assert ws.status == WorkspaceStatus.RUNNING.value and ws.started_at is not None, (
                "前提未达成：窗口没留在 RUNNING+started_at"
            )
            rate = db.scalar(
                select(Template.estimated_hourly_cost_cny).where(Template.id == "tpl-live-booked")
            )

        resp = client.get("/api/usage", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["running_workspaces"] == 1, body
        assert body["accumulated_gpu_seconds"] == booked == SEGMENT_SECONDS, (
            f"GET /api/usage 报 {body['accumulated_gpu_seconds']}，账本 SUM 只有 {booked}"
        )
        assert body["estimated_cost_cny"] == round(
            (SEGMENT_SECONDS / 3600.0) * float(rate), 2
        ), f"折算金额跟着 live 一起翻：{body['estimated_cost_cny']}"


# ---------------------------------------------------------------------------
# C2 live 该算的还得算
# ---------------------------------------------------------------------------


def test_unbooked_running_segment_is_still_admitted(monkeypatch):
    """C2a RUNNING 且未入账的一段必须照加 live：配额边界两个极点都跟着走。

    反向极点：把 live 整个砍掉（只报投影）会让"刚跑了一段、还没结算"的用量在门禁里
    消失，学生可以无限白跑——所以这里不能只断"没双计"。这一支改前改后都必须绿
    （它守的是"别修过头"，缺陷那半边由 C1 负责）。
    """
    clock = _freeze_clock(monkeypatch)
    billing = BillingPolicy(Factory, CreditLedgerService(Factory))
    wid = _running_workspace(clock)

    with Factory() as db:
        assert _ledger_gpu_seconds(db, wid) == 0, "前提未达成：这一段已经有账本行了"
        # 判据跑在 1s 之内，所以 live 恰好是夹具挪出来的那 SEGMENT_SECONDS 秒
        used = billing.course_usage_seconds(db, "u1", db.get(Lab, "lab-1"))
        assert used == SEGMENT_SECONDS, f"未入账的 live 没被计入：{used}"

    _top_up(billing, SEGMENT_SECONDS)
    with Factory() as db:
        student = db.get(User, "u1")
        template = db.get(Template, "cartpole")
        lab = db.get(Lab, "lab-1")
        lab.quota_seconds = SEGMENT_SECONDS + 1
        billing.check_launch_eligible(db, student, template, lab=lab)  # 差一秒放行
        lab.quota_seconds = SEGMENT_SECONDS
        with pytest.raises(BillingError, match="quota exhausted"):
            billing.check_launch_eligible(db, student, template, lab=lab)


def test_sub_second_settled_segment_still_admits_live(monkeypatch):
    """C2b 亚秒段不写账本行 ⇒ "行不存在"就是正确谓词，live 必须照加。

    `settle_workspace_run` 对 `seconds <= 0` 返回 None（`ledger.py:236-237`），所以刚结算
    过一个 0 秒段的 workspace 仍然"这一段没入账"。常驻
    `tests/test_settled_projection.py:513-518` 钉的是投影那一侧（0 秒段不清零、不改大
    `accumulated_seconds`）且只断到"没多写行"；这里补读者那一侧：同一个形状下 live 项
    不许被"结算过"这个事实顺手关掉。
    """
    clock = _freeze_clock(monkeypatch)
    billing = BillingPolicy(Factory, CreditLedgerService(Factory))
    orchestrator = _orchestrator(billing=billing)
    wid = _running_workspace(clock)
    assert _settle_once(orchestrator, wid) == SEGMENT_SECONDS

    with Factory() as db:
        db.get(Workspace, wid).started_at = clock.now  # 紧接着一段刚开就结束（elapsed=0）
        db.commit()
    assert _settle_once(orchestrator, wid) == 0, "0 秒段写出了非零入账"

    with Factory() as db:
        assert len(_usage_rows(db, wid)) == 1, "0 秒段写出了额外的账本行"
        assert db.get(Workspace, wid).accumulated_seconds == SEGMENT_SECONDS, (
            "投影被空段清零/改大了（与常驻 :513-518 同一条不变量）"
        )

    with Factory() as db:  # 那段现在走了 5 秒，仍然没进账本 ⇒ live 照加
        db.get(Workspace, wid).started_at = clock.now - timedelta(seconds=5)
        db.commit()
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value
        assert _ledger_gpu_seconds(db, wid) == SEGMENT_SECONDS
        # 判据跑在 1s 之内 ⇒ QUOTA 门禁读到的就是 30（投影）+5（未入账的 live）
        used = billing.course_usage_seconds(db, "u1", db.get(Lab, "lab-1"))
        assert used == SEGMENT_SECONDS + 5, (
            f"亚秒/未入账段的 live 项被误关（读者开始少算）：{used}"
        )


def test_usage_endpoint_still_counts_an_unbooked_running_segment(monkeypatch):
    """C2c HTTP 面的反极：没入账的 RUNNING 段在 /api/usage 里必须照加 live。

    C1c 证明"已入账不再加"，这一条证明"没入账还得加"——两条合起来才把口径钉成
    "恰好一次"。这里**不**触发 destroy 失败，账本里一段都没有，所以投影 0、全部来自 live。
    路由器自己走真实时钟（`usage.py:27`），从 seed 到读数隔的是亚秒级，故上下界各留 2 秒：
    上界 32 仍然远低于"双计"的 60，下界 28 仍然远高于"把 live 整个砍掉"的 0。
    """
    from fastapi.testclient import TestClient

    from app.deps import SessionFactory
    from app.main import app
    from tests.http_auth import auth_headers, register_body

    with TestClient(app) as client:
        token = register_body(client, "live-usage-fresh@example.com", "live-usage-fresh")["token"]
        headers = auth_headers(token)
        wid = _http_running_workspace(
            email="live-usage-fresh@example.com",
            ws_id="ws-live-unbooked",
            tpl_id="tpl-live-unbooked",
            recharge_key="live-usage-topup-unbooked",
            started_at=datetime.now(UTC) - timedelta(seconds=SEGMENT_SECONDS),
        )
        with SessionFactory() as db:
            assert _ledger_gpu_seconds(db, wid) == 0, "前提未达成：这一段已经进账本了"

        body = client.get("/api/usage", headers=headers).json()
        assert body["running_workspaces"] == 1, body
        assert SEGMENT_SECONDS - 2 <= body["accumulated_gpu_seconds"] <= SEGMENT_SECONDS + 2, (
            f"未入账的 live 在展示端被丢了/重算了：{body['accumulated_gpu_seconds']}"
        )


# ---------------------------------------------------------------------------
# C3 重启：第二段是新 started_at ⇒ 照加（杀掉"有 USAGE 行就跳过 live"的捷径）
# ---------------------------------------------------------------------------


def test_second_segment_after_restart_is_admitted(monkeypatch):
    """C3 两段：第一段已入账、第二段是新的 `started_at` ⇒ live 照加、投影不动。

    这一条是那种捷径的反例——"该 workspace 有任意 USAGE 行 ⇒ 认定已入账，跳过 live"。
    下面明确断言"存在 USAGE 行"与"live 仍被计入"同时成立：按 workspace_id 判存在
    的写法在这里会少算 7 秒，而按这一段自己的键点查不会。
    """
    clock = _freeze_clock(monkeypatch)
    billing = BillingPolicy(Factory, CreditLedgerService(Factory))
    orchestrator = _orchestrator(billing=billing)
    wid = _running_workspace(clock)
    assert _settle_once(orchestrator, wid) == SEGMENT_SECONDS

    with Factory() as db:  # restart：上一段收尾，started_at 清零
        ws = db.get(Workspace, wid)
        ws.started_at = None
        db.commit()
    with Factory() as db:  # 新的一段：新时刻 ⇒ 新键
        ws = db.get(Workspace, wid)
        ws.started_at = clock.now - timedelta(seconds=7)
        db.commit()

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert len(_usage_rows(db, wid)) == 1, "前提未达成：第二段已经自己入账了，反例失效"
        assert _ledger_gpu_seconds(db, wid) == SEGMENT_SECONDS
        assert ws.accumulated_seconds == SEGMENT_SECONDS, "投影被动过"
        # 判据跑在 1s 之内 ⇒ 门禁读到 30（投影）+7（新段 live）；
        # "有任意 USAGE 行就跳过 live"那种写法在这里只会读出 30，即开火。
        used = billing.course_usage_seconds(db, "u1", db.get(Lab, "lab-1"))
        assert used == SEGMENT_SECONDS + 7, f"重启后的新段被当成已入账（少算 live）：{used}"


def test_first_segments_key_does_not_answer_for_the_second_one(monkeypatch):
    """C3b 键是逐段的：拿第一段的 `started_at` 点查为真、拿第二段的是假。

    `segment_booked` 只认"这一段"，不给"有没有行"留口子。两个时刻都取**从库里读回来**
    的那一份（不是测试自己新造的 datetime）：键里存的就是那个形状，手搓的 tz-aware
    同刻值会落空——这正是 C5b 钉住的陷阱。
    """
    clock = _freeze_clock(monkeypatch)
    orchestrator = _orchestrator()
    wid = _running_workspace(clock)
    assert _settle_once(orchestrator, wid) == SEGMENT_SECONDS

    with Factory() as db:
        first_started = db.get(Workspace, wid).started_at
        assert first_started is not None
    with Factory() as db:  # 第二段：新的 started_at 落库
        ws = db.get(Workspace, wid)
        ws.started_at = clock.now - timedelta(seconds=7)
        db.commit()

    with Factory() as db:
        service = _booked_service()
        second_started = db.get(Workspace, wid).started_at  # 同样取读回来的那一份
        assert second_started is not None and second_started != first_started
        assert service.segment_booked(db, wid, first_started) is True
        assert service.segment_booked(db, wid, second_started) is False
        assert service.segment_booked(db, "no-such-workspace", first_started) is False


# ---------------------------------------------------------------------------
# C4 唯一定义（带必须开火的对照）
# ---------------------------------------------------------------------------


def usage_key_constructions(source: str) -> list[tuple[int, str]]:
    """数出源码里 `usage:` 这串 **f-string** 的构造位点，带它所在的函数名。

    只看 JoinedStr 的常量片段，所以文档串/注释里的同形文字（`ledger.py` 与
    `orchestrator.py` 的说明都还写着那把键的形状）不算构造。命中几处报几处，
    不 `break` 整个循环——否则第一处之后的副本会在尺子上一起消失。
    """
    tree = ast.parse(source)
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def enclosing_func(node: ast.AST) -> str:
        cur: ast.AST | None = node
        while cur is not None:
            if isinstance(cur, ast.FunctionDef):
                return cur.name
            cur = parent.get(cur)
        return "<module>"

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        if any(
            isinstance(part, ast.Constant)
            and isinstance(part.value, str)
            and "usage:" in part.value
            for part in node.values
        ):
            hits.append((node.lineno, enclosing_func(node)))
    return hits


def accumulated_reads(source: str) -> list[int]:
    """数出源码里对 `accumulated_seconds` 的**属性读取**位点（口径该在共享函数里）。"""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute) and node.attr == "accumulated_seconds"
    ]


def _app_sources(*, routers_only: bool = False, billing_only: bool = False) -> list[Path]:
    if routers_only:
        return sorted(p for p in (APP_DIR / "routers").rglob("*.py") if "__pycache__" not in p.parts)
    if billing_only:
        return [APP_DIR / "services" / "billing.py"]
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_usage_key_has_exactly_one_construction_in_app():
    """C4a 全仓 app/ 里 `usage:` 键只能有一处可执行构造，且它就在模板函数里。

    改前那处是 `ledger.py:137` 的裸 f-string；本轮把它收进 `usage_idempotency_key`，
    写侧与读侧都改为调它。计数仍须 == 1：读者若自己再拼一遍，双计只是换个地方复发。
    """
    found: list[tuple[str, int, str]] = []
    for path in _app_sources():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, owner in usage_key_constructions(path.read_text(encoding="utf-8")):
            found.append((rel, lineno, owner))
    assert len(found) == 1, f"`usage:` 键的构造位点必须恰好一处，实际：{found}"
    assert found[0][2] == "usage_idempotency_key", f"唯一那处必须在模板函数里，实际：{found}"
    assert found[0][0] == "app/services/ledger.py", f"模板必须住在账本服务里，实际：{found}"


def test_the_usage_key_ruler_can_see_a_duplicate_and_ignores_prose():
    """C4b 判据自测：合成副本必须开火，两处合规写法（调模板 / 仅说明文字）必须不开火。

    没有这一支，上一条在"尺子什么都数不到"时会一直绿。
    """
    assert usage_key_constructions('k = f"usage:{wid}:{iso}"\n') == [(1, "<module>")]
    assert usage_key_constructions(
        "def other():\n    return f\"x-usage:{wid}\"\n"
    ) == [(2, "other")]
    assert usage_key_constructions("k = usage_idempotency_key(wid, iso)\n") == []
    assert usage_key_constructions('"""f"usage:{wid}:{iso}" 只是说明文字"""\n') == []
    assert usage_key_constructions('f"GPU usage {seconds}s @ 1 credit/second"\n') == []


def test_the_two_readers_no_longer_read_the_projection_column():
    """C4c `app/routers/` 与 `app/services/billing.py` 里不许再留 `accumulated_seconds + live`。

    尺子取的是更硬的那条性质：这两个面上**任何**对 `accumulated_seconds` 的读取都算违规
    ——口径只能在 `workspace_seconds_used` 里（读侧只剩"属性读"这一种写法，f-string 的
    `+ live` 是它的特例，所以不必再钉那串文本）。
    """
    offenders: list[str] = []
    for path in [*_app_sources(routers_only=True), *_app_sources(billing_only=True)]:
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        offenders.extend(f"{rel}:{ln}" for ln in accumulated_reads(src))
    assert offenders == [], f"读者又自己抄投影列（该走 workspace_seconds_used）：{offenders}"


def test_the_reader_ruler_can_see_the_old_shape():
    """C4d 判据自测：改前那两位读者的写法必须开火，改后的合规写法不许。"""
    assert accumulated_reads("seconds_by_workspace[w.id] = w.accumulated_seconds + live\n") == [1]
    assert accumulated_reads("total += w.accumulated_seconds or 0\n") == [1]
    assert accumulated_reads("x = workspace_seconds_used(db, w, now=now)\n") == []
    assert accumulated_reads('"""w.accumulated_seconds + live 只是说明"""\n') == []


def test_the_used_seconds_authority_is_defined_once():
    """C4e "已用秒数"与"这段入账了吗"各自在 app/ 里只有一个定义。

    重复定义（哪怕在别的模块里再抄一份 live 公式）就是下一次分叉的开始。
    """
    authorities = (
        "def workspace_seconds_used(",
        "def usage_idempotency_key(",
        "def usage_segment_booked(",
        "def booked_usage_keys(",
    )
    counts = dict.fromkeys(authorities, 0)
    for path in _app_sources():
        src = path.read_text(encoding="utf-8")
        for name in counts:
            counts[name] += src.count(name)
    assert counts == {
        "def workspace_seconds_used(": 1,
        "def usage_idempotency_key(": 1,
        "def usage_segment_booked(": 1,
        "def booked_usage_keys(": 1,
    }, counts
    # 公开方法只是薄转接，不是第二处口径
    assert (APP_DIR / "services" / "ledger.py").read_text(encoding="utf-8").count(
        "def segment_booked("
    ) == 1


# ---------------------------------------------------------------------------
# C5 键的往返诚实性：真落库回读，不是手搓字符串
# ---------------------------------------------------------------------------


def test_segment_booked_matches_a_landed_row(monkeypatch):
    """C5a 对**真写进库又读回来**的 `started_at` 为真；换个时刻为假；None 为假。

    批量口径（`booked_keys`）与点查同判——列表端点用的就是它。
    """
    clock = _freeze_clock(monkeypatch)
    orchestrator = _orchestrator()
    wid = _running_workspace(clock)
    assert _settle_once(orchestrator, wid) == SEGMENT_SECONDS

    with Factory() as db:
        service = _booked_service()
        landed = db.get(Workspace, wid).started_at
        assert landed is not None
        assert service.segment_booked(db, wid, landed) is True, "落库回读的串点不中自己那一段"
        assert service.segment_booked(db, wid, landed - timedelta(seconds=1)) is False
        assert service.segment_booked(db, wid, None) is False
        keys = _authority("booked_usage_keys")(db, [wid])
        assert service.segment_booked(db, wid, landed, booked_keys=keys) is True
        assert _authority("booked_usage_keys")(db, []) == set()


def test_the_key_is_the_raw_column_value_not_a_normalized_one(monkeypatch):
    """C5b 陷阱钉死：SQLite 回读的 `started_at` 不带偏移，归一化后的串是**另一把键**。

    写侧喂的就是这个原始串，所以读侧必须原样喂；谁在中间补 `replace(tzinfo=UTC)`，
    点查就永远落空（等价于"这一段没入账"），live 照加——双计被修成静默不修。
    本模块的库按构造是 SQLite（`tests/dbfiles.db_url`），所以这条形状是确定的。

    键的期望值用 **HEAD 那处裸 f-string 的形状**（`_head_key`）自己算，不借被测实现，
    否则这条判据只是在复读实现写的字符串。
    """
    clock = _freeze_clock(monkeypatch)
    orchestrator = _orchestrator()
    wid = _running_workspace(clock)
    assert _settle_once(orchestrator, wid) == SEGMENT_SECONDS

    with Factory() as db:
        landed = db.get(Workspace, wid).started_at
        assert landed is not None and landed.tzinfo is None, (
            "本判据按 SQLite 回读形状写；引擎换了就要同步改这里，别让它静默失效"
        )
        row = _usage_rows(db, wid)[0]
        raw_iso = landed.isoformat()
        normalized_iso = landed.replace(tzinfo=UTC).isoformat()
        assert row.idempotency_key == _head_key(wid, raw_iso)
        assert normalized_iso != raw_iso, "两种形状本来就相同 → 这条陷阱判据失去意义"
        assert _head_key(wid, normalized_iso) != row.idempotency_key, (
            "归一化串居然命中了同一把键：谓词不再能区分"
        )
        assert _booked_service().segment_booked(db, wid, landed) is True


def test_the_key_template_is_byte_identical_to_the_head_construction(monkeypatch):
    """C5c 模板函数产出的串与 HEAD 那处裸 f-string 逐字节相同：老行的身份不变。

    键一改，已入账的行就再也点不中（投影与 live 的口径同时失效），所以这条必须钉住。
    样本含带偏移/不带偏移/含冒号的 workspace id 与空串。
    """
    samples = [
        ("ws-1", "2026-09-28T02:41:14.397612"),
        ("ws-1", "2026-09-28T02:41:14.397612+00:00"),
        ("5f0b4d6e-9a55-4d0a-9e00-000000000001", "2026-01-01T00:00:00"),
        ("ws:weird:id", "2026-09-28T02:41:14.397612+08:00"),
        ("", ""),
        ("ws-2", "2026-12-31T23:59:59.999999"),
    ]
    template = _authority("usage_idempotency_key")
    for wid, iso in samples:
        assert template(wid, iso) == _head_key(wid, iso)

    # 真行也对得上：账本里那一条的键 == HEAD 写法 == 模板写法
    clock = _freeze_clock(monkeypatch)
    orchestrator = _orchestrator()
    wid = _running_workspace(clock)
    assert _settle_once(orchestrator, wid) == SEGMENT_SECONDS
    with Factory() as db:
        landed = db.get(Workspace, wid).started_at
        row = _usage_rows(db, wid)[0]
        assert row.idempotency_key == _head_key(wid, landed.isoformat())
        assert row.idempotency_key == template(wid, landed.isoformat())


# ---------------------------------------------------------------------------
# C6 第三个读者是前端：谓词由服务端发，每个 WorkspaceOut 出口都得带上
# ---------------------------------------------------------------------------

APP_JS = REPO_ROOT / "app" / "static" / "app.js"


def unflagged_live_sites(source: str) -> list[int]:
    """app.js 里"看 `started_at` 现场加 live"却没问过服务端旗标的那几处行号。

    前端不能直接用服务端的秒数：它两秒一轮重新渲染，数字要接着跳。所以它需要的不是
    那个数，而是那句谓词——"这一段进账本了没有"。这一格看不见谓词就等于把 N-64 的
    双计留在 UI 上（账本 30 + live 30 ⇒ 显示 60）。
    """
    hits: list[int] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        code = line.split("//")[0]
        if (
            "const live" in code
            and "w.started_at" in code
            and "usage_segment_booked" not in code
        ):
            hits.append(lineno)
    return hits


def live_term_sites(source: str) -> list[int]:
    """app.js 里所有"现场加 live"的落点（分母）：尺子判的是其中几处没问旗标。"""
    return [
        lineno
        for lineno, line in enumerate(source.splitlines(), start=1)
        if "const live" in line.split("//")[0] and "w.started_at" in line
    ]


def unannotated_workspace_routes(source: str) -> list[tuple[int, str]]:
    """装饰器里出现 `WorkspaceOut` 的路由函数，体内没调 `mark_usage_segment(s)` 的那些。

    按词边界取名字（`WorkspaceAccessOut` 不是 `WorkspaceOut`），且把分母一起交出去，
    这样"一个都没找到"与"全都合规"在读数上是两件事。
    """
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef):
            continue
        decorated = any(
            re.search(r"\bWorkspaceOut\b", ast.unparse(d)) for d in node.decorator_list
        )
        if not decorated:
            continue
        annotated = any(
            (isinstance(c.func, ast.Name) and c.func.id.startswith("mark_usage_segment"))
            or (isinstance(c.func, ast.Attribute) and c.func.attr.startswith("mark_usage_segment"))
            for c in ast.walk(node)
            if isinstance(c, ast.Call)
        )
        if not annotated:
            offenders.append((node.lineno, node.name))
    return offenders


def workspace_out_routes(source: str) -> list[str]:
    """同一个面上被认出的路由名（分母）。"""
    return [
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef)
        and any(re.search(r"\bWorkspaceOut\b", ast.unparse(d)) for d in node.decorator_list)
    ]


def flag_writer_sites(source: str) -> list[int]:
    """源码里对 `usage_segment_booked` 的**赋值**位点（属性写，不是 schema 声明）。"""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Attribute) and t.attr == "usage_segment_booked"
            for t in node.targets
        )
    ]


def test_workspaces_endpoint_reports_the_flag_in_both_poles(monkeypatch):
    """C6a GET /api/workspaces 一个请求两行两极：入账那段 True、没入账那段 False。

    极性必须同时钉：只有 True 那一半的话，一个恒真字段也是绿的。账本侧的事实
    （一段有行、一段没行）在同一支里各数一遍，旗标不是抄来的常量。
    """
    from fastapi.testclient import TestClient

    from app.deps import SessionFactory
    from app.deps import orchestrator as app_orchestrator
    from app.main import app
    from tests.http_auth import auth_headers, register_body

    clock = _freeze_clock(monkeypatch)
    with TestClient(app) as client:
        token = register_body(client, "ws-flag@example.com", "ws-flag")["token"]
        headers = auth_headers(token)
        booked = _http_running_workspace(
            email="ws-flag@example.com",
            ws_id="ws-flag-booked",
            tpl_id="tpl-flag-booked",
            recharge_key="ws-flag-topup-booked",
            started_at=clock.now - timedelta(seconds=SEGMENT_SECONDS),
        )
        live = _http_running_workspace(
            email="ws-flag@example.com",
            ws_id="ws-flag-live",
            tpl_id="tpl-flag-live",
            recharge_key="ws-flag-topup-live",
            started_at=clock.now - timedelta(seconds=7),
        )

        monkeypatch.setattr(
            app_orchestrator, "provider", FailingDestroyProvider(app_orchestrator.provider)
        )
        with SessionFactory() as db, pytest.raises(RuntimeError, match="provider destroy failure"):
            app_orchestrator.destroy(db, db.get(Workspace, booked))

        resp = client.get("/api/workspaces", headers=headers)
        assert resp.status_code == 200, resp.text
        rows = {r["id"]: r for r in resp.json()}
        assert booked in rows and live in rows, f"两行没同时出现在列表里：{sorted(rows)}"
        assert rows[booked]["usage_segment_booked"] is True, rows[booked]
        assert rows[live]["usage_segment_booked"] is False, rows[live]

        with SessionFactory() as db:
            assert _ledger_gpu_seconds(db, booked) == SEGMENT_SECONDS
            assert _usage_rows(db, live) == [], "未入账那一行其实有账，两极就成了假的"


def test_the_frontend_live_term_consults_the_flag():
    """C6b 真语料：app.js 的 live 落点全部问过旗标，且落点数不是 0。"""
    src = APP_JS.read_text(encoding="utf-8")
    seen = live_term_sites(src)
    assert len(seen) == 2, f"前端 live 算术的落点读数变了（读到 {len(seen)}：{seen}）"
    assert unflagged_live_sites(src) == [], f"前端又自己决定这一段没入账：{unflagged_live_sites(src)}"


def test_the_frontend_ruler_can_see_the_old_shape():
    """C6c 判据自测：改前写法开火、问过旗标不开火、非 live 的 started_at 读取不进分母。"""
    old = 'const live = w.status === "running" && w.started_at\n'
    assert unflagged_live_sites(old) == [1]
    assert live_term_sites(old) == [1]
    assert unflagged_live_sites(
        'const live = w.status === "running" && w.started_at && !w.usage_segment_booked\n'
    ) == []
    # 不是 live 算术的 started_at 读取（比如只显示开始时间）不进分母
    assert live_term_sites('const age = new Date(w.started_at).getTime();\n') == []


def test_every_workspace_route_annotates_the_flag():
    """C6d 真语料：每个返回 WorkspaceOut 的路由都必须打上旗标（漏一处＝那一行回到双计）。"""
    routes: list[str] = []
    offenders: list[tuple[str, int, str]] = []
    for path in _app_sources(routers_only=True):
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        routes.extend(workspace_out_routes(src))
        offenders.extend((rel, lineno, name) for lineno, name in unannotated_workspace_routes(src))
    assert len(routes) == 7, f"被认出的 WorkspaceOut 路由数变了（读到 {len(routes)}：{routes}）"
    assert offenders == [], f"这些出口没带旗标：{offenders}"


def test_the_router_ruler_can_see_a_missing_annotation():
    """C6e 判据自测：漏标必须点名，`WorkspaceAccessOut` 这种别的名不许进分母。"""
    missing = (
        "from ..schemas import WorkspaceOut\n"
        "@router.get('/x', response_model=WorkspaceOut)\n"
        "def get_x(db, user):\n"
        "    return workspace\n"
    )
    assert unannotated_workspace_routes(missing) == [(3, "get_x")], unannotated_workspace_routes(
        missing
    )
    assert workspace_out_routes(missing) == ["get_x"]
    annotated = missing.replace("    return workspace", "    return mark_usage_segment(db, workspace)")
    assert unannotated_workspace_routes(annotated) == []
    other = (
        "from ..schemas import WorkspaceAccessOut\n"
        "@router.get('/y', response_model=WorkspaceAccessOut)\n"
        "def get_y(db, user):\n"
        "    return WorkspaceAccessOut(workspace_id='x')\n"
    )
    assert workspace_out_routes(other) == []
    assert unannotated_workspace_routes(other) == []


def test_the_flag_has_exactly_one_writer_in_app():
    """C6f 旗标只有一个书写点（`ledger.mark_usage_segments`），别处再赋一次就是第二套口径。"""
    found: list[tuple[str, int]] = []
    for path in _app_sources():
        rel = path.relative_to(REPO_ROOT).as_posix()
        found.extend((rel, lineno) for lineno in flag_writer_sites(path.read_text(encoding="utf-8")))
    assert len(found) == 1, f"旗标的赋值位点必须恰好一处，实际：{found}"
    assert found[0][0] == "app/services/ledger.py", f"唯一那处该在账本服务里，实际：{found}"
    assert flag_writer_sites("ws.usage_segment_booked = x\nws.usage_segment_booked = y\n") == [1, 2]
    assert flag_writer_sites("usage_segment_booked: bool = False\n") == []

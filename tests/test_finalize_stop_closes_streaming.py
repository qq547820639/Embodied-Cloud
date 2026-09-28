"""`_finalize_stop` 写 STOPPED 却没终结串流会话（N-117，由本轮 N-119 闭合）。

改前的形状（一手核实，`app/services/orchestrator.py`）：`_finalize_stop` 做结算 →
`scheduler.release` → `workspace.status = WorkspaceStatus.STOPPED.value`，全程不调
`streaming.terminate_for_workspace`。它是 `app/` 里**唯一**一处"写出 WorkspaceStatus
终态、而同一执行路径上此前没有任何终结"的位点（B 组尺子在全 app/ 上实测得出一处，
不是读一眼 stop() 的猜测；射程外的间接写见下面"不证明的东西"第 4 条）。
它的两个正常调用方本来都先关会话——`_stop_cleanup`（终结在 `:448`，`_finalize_stop` 在
`:472`）与 reconcile 的 STOPPING 档（`:837` → `:846`）——所以今天的读数看起来没事。
洞在重试档：`stop()` 的幂等分支 `:421-427` 直接
`self._finalize_stop(db, workspace)`，它把"STOPPED ⇒ 会话已经关过"当成结构保证，
而这份保证并不存在。

不成立的机制（逐行核实）：`_stop_cleanup` 的 `try`（`:446-452`）把
`terminate_for_workspace` 与 `provider.stop` 放进**同一个** `try`，而 `except`（`:453-456`）
不 `db.rollback()`；若终结自己抛错、而 provider 仍把 runtime 报成没了
（`_release_admitted(workspace, command_succeeded=False)` → True，MockProvider 的
`reconcile` 说 MISSING 就是这一极，`app/services/providers/mock.py:72-74`），那么
`_finalize_stop` 照样跑完并 commit 出 STOPPED，而 `streaming_sessions.status` 还是
`connected`、两侧端口还占着。此后每一次 `stop()` 重试都走 `:421` 那一档，
**永远修不回来**——两表互相打脸（与 N-82/N-116 同族）。

判据分两组：
- A 行为面（真库读数）：A1 钉"给成那个现场，`stop()` 必须能把它修回来"（改前红，本批转绿）；
  A2 钉"那个现场用真代码就构造得出来"（前提，含桩替协作者，不 monkeypatch 被测代码）；
  A3 是不许开火的对照——正常那次 stop 依旧"恰好关一次"，资源与账本都不许多动。
- B 结构面（纯 AST，不碰库）：把 N-116 那把只看 `_reconcile_one` 的尺子宽成
  全 `app/` 逐 def 扫：`X.status = WorkspaceStatus.<FAILED|STOPPED|DELETED>.value`
  必须能在"从这条赋值往上、到最近外层函数为止"的路径上、排在它之前的兄弟语句里
  找到 `terminate_for_workspace`/`_stop_cleanup`。

判据极性：本批把草稿（`/tmp/n117-draft/`）里的两处 **as-is** 断言翻成了应然 —— B 组那条
现在钉 `readings["offenders"] == set()`（同一条里留着 `per_file` 分母自证，空集不来自
"什么都没数到"）；A2 的中段钉"一次 stop 之后会话必须 `failed`"，改前读到的是 `connected`
加原样端口，所以它是开火极性而不是描述洞。修法落在 `_finalize_stop` 的写状态之前
（终结在 `app/services/orchestrator.py:596`，写 STOPPED 紧接其后在 `:597`），docstring 一并改。

不证明的东西（诚实边界）：
- 桩替的是协作者（streaming 服务），所以 A2 证的是"**在** provider 认账退役、而终结调用抛错
  时，终态写入不会被那次抛错阻断"，它**不**证明真实 Docker/K8s 下 `terminate_for_workspace`
  到底会不会抛、也不证明真库里那次抛错的原因。
- 尺子只看"在先它执行的语句里有没有调过终结"，不看那次调用是否被包在一个会吞异常的 `try` 里
  （`_stop_cleanup:448` 与 `destroy:618` 都包着），也不看它是否真的写完：于是
  "把 `_stop_cleanup` 内部那行终结删掉"这把变异本尺仍绿——守那一面的是 A2 与
  `tests/test_streaming_lifecycle.py:137`、`:186`。
- 尺子按 AST 根节点判枚举名 ⇒ 值经局部变量的间接写不在射程内。实测 `app/` 里非直接值的
  `X.status` 写共 7 处，其中含 WorkspaceStatus 终态的只有 `_fail` 的两行
  （`orchestrator.py:370`、`:383`，`settled = ...FAILED... if terminal else ...QUEUED...`）；
  它今天不构成第二个洞（唯一调用点 `:301` 在 provision 失败档，本条不判那里有没有会话可关），
  但这是**尺子的盲区**而不是"已证没有"。同理漏 `w.status = "stopped"` 这种常量字面量写法
  （实测 `app/` 内 WorkspaceStatus 侧无此形态，只有 `course.py:432` 的 `'completed'`）。
- 尺子只扫 `def` 内的 `ast.Assign`：模块级/类体里的写、`AnnAssign`、
  `update(...).values(status=...)` 批量 UPDATE 都不判（实测 `app/` 的三处
  `values(status=...)` 分别是 `DeploymentStatus.DOWNLOADING` 与两张卡的 `GpuStatus.AVAILABLE`，
  没有 WorkspaceStatus 终态）。
- 语料用 `pathlib.Path("app").rglob("*.py")` 枚举，不用 `git ls-files`：常驻用例必须
  在没有 .git 的副本里也读得出同一个分母。
- 会话行一律用 ORM 直接建（与 N-116 同一立场）：被测的是**终结**那一半，
  `streaming.start()` 要的整套所有权/凭据前置只会稀释判据。
"""

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    CreditLedger,
    Gpu,
    GpuAllocation,
    GpuStatus,
    LedgerType,
    Role,
    StreamingSession,
    StreamingStatus,
    Template,
    User,
    Workspace,
    WorkspaceStatus,
)
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.base import RuntimeState
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.services.streaming import StreamingSessionService
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("finalize-stop"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)
REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
BASE = datetime(2026, 1, 1, tzinfo=UTC)
# `_finalize_stop:590` 那条赋值今天的位点串。行号会随它上方的改动漂移，
# 所以下面用 `_statement_at()` 把位点还原成语句原文，漂移时失败消息能自证。


class ScriptedProvider(MockProvider):
    """按脚本说话的那一极（本文件只需要 MISSING：provider 认账 runtime 已不在）。"""

    def __init__(self, base_url: str, *, state: RuntimeState) -> None:
        super().__init__(base_url)
        self.scripted = state
        self.calls = 0

    def reconcile(self, workspace: Workspace) -> RuntimeState:
        self.calls += 1
        return self.scripted


class FaultOnFirstClose(StreamingSessionService):
    """第一次终结就抛错、之后都委托真实现的那一档桩。

    它替的是**协作者**（streaming 服务），不是被测的 orchestrator 代码。抛点刻意放在
    "还没碰任何列之前"——那是唯一能留下"会话仍 connected"的形状：若在 `_transition`
    之后才抛，`except`（`:453-456`）不 rollback，随后 `scheduler.release` 内部那次
    `db.commit()`（`app/services/scheduler.py:310`）会把已改脏的 FAILED 一起提交掉，
    损害反而被抹平。这一点本身就是洞的一半，因此把它写在这里而不是另开一档。
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        super().__init__(session_factory)
        self.calls = 0

    def terminate_for_workspace(self, db: Session, workspace_id: str) -> int:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated: streaming close failed before touching any column")
        return super().terminate_for_workspace(db, workspace_id)


def _orchestrator(
    provider: MockProvider,
    streaming: StreamingSessionService | None = None,
) -> WorkspaceOrchestrator:
    return WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-finalize-stop"), streaming=streaming  # noqa: S108
    )


@pytest.fixture(autouse=True)
def _clean_db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


def _seed_cell() -> None:
    """一格 RUNNING 的 workspace（own 一张卡、两侧带端口）+ 一条 connected 的会话。"""
    scheduler = GpuScheduler(Factory)
    with Factory() as db:
        scheduler.sync_host(
            db,
            host_id="host-1",
            name="h1",
            address="127.0.0.1",
            provider="mock",
            gpus=[GpuInfo(gpu_uuid="gpu-0", model="RTX", memory_total=24564, index=0)],
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
        db.commit()
        db.add(
            Workspace(
                id="ws-1",
                name="cell",
                template_id="cartpole",
                provider="mock",
                user_id="u1",
                status=WorkspaceStatus.RUNNING.value,
                started_at=BASE,
                signal_port=8887,
                media_port=8888,
                created_at=BASE,
            )
        )
        db.commit()
        gpu = scheduler.allocate(db, "ws-1", gpu_requirement_gb=8)
        db.get(Workspace, "ws-1").gpu_id = gpu.id
        db.add(
            StreamingSession(
                id="s-1",
                workspace_id="ws-1",
                status=StreamingStatus.CONNECTED.value,
                signal_port=8887,
                media_port=8888,
            )
        )
        db.commit()


def _force_the_crash_left_shape() -> None:
    """把那一格写成"崩溃留下的现场"：STOPPED + 卡已回池 + 会话仍 connected + 两侧端口还占着。

    为什么这一档用 ORM 直写、而不用真路（A2 那档才用真路）：真路产出的这个现场
    **随修法一起消失**——修法要做的正是"第一次 stop 就把会话关掉"。常驻用例的前提
    不能只在"还没修"的世界里构造得出来，否则 A1 与 A2 被绑成同一条极性、
    修法落地后 A1 直接失去可构造性。
    卡这一半用真函数 `GpuScheduler.release`（幂等，且 N-86 的"三件成对"不是本条要
    怀疑的对象）；`STOPPED` / `started_at=None` 这两列按 `_finalize_stop`（`:597-599`）
    留下的形状用 ORM 写。会话行与 workspace 两侧的端口都**原样留着**，那才是洞。
    """
    _seed_cell()
    scheduler = GpuScheduler(Factory)
    with Factory() as db:
        scheduler.release(db, "ws-1")
        workspace = db.get(Workspace, "ws-1")
        assert workspace is not None
        workspace.status = WorkspaceStatus.STOPPED.value
        workspace.started_at = None
        workspace.stopped_at = BASE
        db.commit()


def _reading() -> tuple[str, str, tuple[int | None, int | None], tuple[int | None, int | None]]:
    """(workspace.status, session.status, 会话(signal, media), workspace(signal, media))。"""
    with Factory() as db:
        ws = db.scalar(select(Workspace).where(Workspace.id == "ws-1"))
        session = db.scalar(select(StreamingSession).where(StreamingSession.id == "s-1"))
        assert ws is not None and session is not None
        return (
            str(ws.status),
            str(session.status),
            (session.signal_port, session.media_port),
            (ws.signal_port, ws.media_port),
        )


def _usage_rows() -> list[CreditLedger]:
    with Factory() as db:
        return list(
            db.scalars(
                select(CreditLedger).where(
                    CreditLedger.workspace_id == "ws-1",
                    CreditLedger.type == LedgerType.USAGE.value,
                )
            )
        )


def _card() -> tuple[str, str | None, str | None]:
    """(Gpu.status, Gpu.workspace_id, Workspace.gpu_id)——卡回池 + 格子不再声称持有它。"""
    with Factory() as db:
        gpu = db.scalar(select(Gpu))
        ws = db.scalar(select(Workspace).where(Workspace.id == "ws-1"))
        assert gpu is not None and ws is not None
        return (str(gpu.status), gpu.workspace_id, ws.gpu_id)


def _stream_failure_samples() -> float:
    """`stream_failure_total{workspace_id="ws-1"}` 的当前值（series 还没建就是 0）。"""
    return REGISTRY.get_sample_value("stream_failure_total", {"workspace_id": "ws-1"}) or 0.0


# ---------------------------------------------------------------------------
# A 组：行为面
# ---------------------------------------------------------------------------
def test_a_stop_that_never_closed_the_session_repairs_it():
    """写成"崩溃留下的现场"之后，`stop()` 的重试档必须能把会话关掉（改前红，本批转绿）。

    走的是 `stop()` 的幂等分支 `:421-427`：这一格已经是 STOPPED，所以本条不叫 provider
    （那一档压根不调它），传 MISSING 只为让"修法若把重试改走 `_stop_cleanup`"也不改变读数。
    修法应落在 `_finalize_stop` 里（B 组尺子要求的就是那一处），A1 对它两种接线都成立。
    """
    _force_the_crash_left_shape()
    # 前提复核：现场确实是洞留下的那样，而不是已经被谁修好了
    assert _reading() == (
        WorkspaceStatus.STOPPED.value,
        StreamingStatus.CONNECTED.value,
        (8887, 8888),
        (8887, 8888),
    ), "夹具没能构造出崩溃现场，本条就成了恒真断言"
    assert _card() == (GpuStatus.AVAILABLE.value, None, None)
    assert _usage_rows() == [], "形状里 started_at 已被清成 None，本条不该自带一段账"

    orchestrator = _orchestrator(ScriptedProvider("http://mock", state=RuntimeState.MISSING))
    with Factory() as db:
        workspace = db.scalar(select(Workspace).where(Workspace.id == "ws-1"))
        assert workspace is not None
        orchestrator.stop(db, workspace)

    ws_status, session_status, session_ports, workspace_ports = _reading()
    assert ws_status == WorkspaceStatus.STOPPED.value
    assert session_status == StreamingStatus.FAILED.value, (
        "重试档把 STOPPED 当成「会话已关」的证据，而它自己就是没关的那一个"
    )
    assert session_ports == (None, None) and workspace_ports == (None, None), "两侧端口都要交还"
    # 资源轴不许被顺手重做：卡已在现场里回池，repair 只是补关会话
    assert _card() == (GpuStatus.AVAILABLE.value, None, None)
    # 结算轴：`_finalize_stop` 先跑 `_settle_run_delta`，而 `started_at` 已是 None ⇒
    # 走 `(0, 0)` 早退（`:550-551`）。这里钉的是"repair 不许按时钟凭空补一段"；
    # "已有一段时第二次 stop 也只算一段（1→1）"那一面在 A2 里钉，因为本档的现场是 ORM 造的。
    assert _usage_rows() == [], f"repair 多写了一笔 USAGE：{[u.gpu_seconds for u in _usage_rows()]}"
    with Factory() as db:
        assert db.scalar(select(Workspace).where(Workspace.id == "ws-1")).accumulated_seconds == 0
        assert db.scalar(
            select(GpuAllocation).where(GpuAllocation.workspace_id == "ws-1")
        ) is None


def test_the_crash_shape_that_produces_it_is_reachable():
    """证前提不只证修复：终结第一次就抛错 + provider 认账退役 ⇒ 终态照样落库，而会话必须已被关。

    桩的第一次抛错发生在协作者内部、`_stop_cleanup` 的 `try` 里（`:446-452`），
    `except`（`:453-456`）不 rollback；provider 报 MISSING ⇒ `_release_admitted(..., False)`
    放行 ⇒ `_finalize_stop` 跑完并 commit 出 STOPPED。这条不依赖"有没有脏写待回滚"
    （桩在改动任何列之前就抛），它依赖的是"那次 commit 确实发生了"。

    中段钉的是**应然**：一次 stop 就得把会话收干净。改前这一句读到的是 `connected` 加
    原样端口（那正是 N-117 的现场），所以本条在修法落地前开火、落地后转绿；草稿第一版
    把它钉成"仍是 connected"是在描述洞而不是防回归，同批改回。末段（第二次 stop）
    与 A1 同极：改前永久打脸，改后幂等无事可做。
    """
    _seed_cell()
    provider = ScriptedProvider("http://mock", state=RuntimeState.MISSING)
    orchestrator = _orchestrator(provider, streaming=FaultOnFirstClose(Factory))

    with Factory() as db:
        workspace = db.scalar(select(Workspace).where(Workspace.id == "ws-1"))
        assert workspace is not None
        orchestrator.stop(db, workspace)

    ws_status, session_status, session_ports, workspace_ports = _reading()
    assert ws_status == WorkspaceStatus.STOPPED.value, "终结抛错只该阻断终结自己，不该阻断收尾"
    assert provider.calls == 1, "准入判据（except 档里那一次 `_release_admitted`）没问过 provider"
    assert session_status == StreamingStatus.FAILED.value, (
        "第一次 stop 之后会话没被关 ⇒ STOPPED 与 connected 并存，而重试档补不回来（N-117）"
    )
    assert session_ports == (None, None) and workspace_ports == (None, None)
    # 资源轴没坏（这正是"只有两张表互相打脸"的意思），账本入了**一段**
    assert _card() == (GpuStatus.AVAILABLE.value, None, None)
    assert len(_usage_rows()) == 1, [u.gpu_seconds for u in _usage_rows()]
    # 本档就是 A1 那句注释里说的 1→1 档：第二次 stop 走 `:421` 幂等分支，`started_at` 已是
    # None ⇒ 不许多出第二行 USAGE。

    with Factory() as db:
        workspace = db.scalar(select(Workspace).where(Workspace.id == "ws-1"))
        assert workspace is not None
        orchestrator.stop(db, workspace)

    ws_status, session_status, session_ports, workspace_ports = _reading()
    assert ws_status == WorkspaceStatus.STOPPED.value
    assert session_status == StreamingStatus.FAILED.value, "重试档修不回来 = 永久打脸"
    assert session_ports == (None, None) and workspace_ports == (None, None)
    assert len(_usage_rows()) == 1, "第二次 stop 多结了一段账"


def test_a_normal_stop_still_closes_the_session_exactly_once():
    """不许开火的对照：正常那次 stop 依旧"恰好关一次"，账本与卡都不许多动。

    "恰好一次"在库里没有可观察面：`terminate_for_workspace` 的 SELECT 把已 failed 的行
    排除在外（`app/services/streaming.py:113-118`），第二次调用是静默 no-op，而
    `_transition` 对 FAILED→FAILED 会抛 ValueError（`:160-164`）—— 于是库里 1 次与 2 次
    完全同形。能分开的只有计数器（`affected > 0` 才 `inc(affected)`，`:128-129`），
    所以这里按 `tests/test_warmpool.py:601-614` 已有的 delta 读法补计数器这一面。
    既有串流用例（`tests/test_streaming_lifecycle.py`）不读它，只读库；除这一句之外
    本条断言的全是库事实。
    """
    _seed_cell()
    before = _stream_failure_samples()
    orchestrator = _orchestrator(ScriptedProvider("http://mock", state=RuntimeState.MISSING))

    with Factory() as db:
        workspace = db.scalar(select(Workspace).where(Workspace.id == "ws-1"))
        assert workspace is not None
        orchestrator.stop(db, workspace)

    assert _stream_failure_samples() - before == 1.0, "终结次数不是恰好一次"
    ws_status, session_status, session_ports, workspace_ports = _reading()
    assert ws_status == WorkspaceStatus.STOPPED.value
    assert session_status == StreamingStatus.FAILED.value
    assert session_ports == (None, None) and workspace_ports == (None, None)
    with Factory() as db:
        session = db.scalar(select(StreamingSession).where(StreamingSession.id == "s-1"))
        assert session is not None
        assert session.error_message == "workspace stopped", session.error_message
        assert len(db.scalars(select(StreamingSession)).all()) == 1, "不许凭空多出一条会话行"
        assert db.scalar(
            select(GpuAllocation).where(GpuAllocation.workspace_id == "ws-1")
        ) is None
    assert _card() == (GpuStatus.AVAILABLE.value, None, None)
    assert len(_usage_rows()) == 1, [u.gpu_seconds for u in _usage_rows()]


# ---------------------------------------------------------------------------
# B 组：宽化后的结构尺（纯 AST，不碰库）
# ---------------------------------------------------------------------------
TERMINAL = {"FAILED", "STOPPED", "DELETED"}
CLOSERS = {"terminate_for_workspace", "_stop_cleanup"}
WORKSPACE_STATUS = "WorkspaceStatus"


def _defs(tree: ast.AST) -> list[ast.FunctionDef]:
    """文件里全部 `def`（含方法与嵌套 def）——每条都要被尺过一遍。"""
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _blocks_of(node: ast.AST) -> list[list]:
    """这个节点持有的语句列表：body / elif-else 链 / finally / 各 except handler。"""
    blocks: list[list] = []
    for field in ("body", "orelse", "finalbody"):
        candidate = getattr(node, field, None)
        if isinstance(candidate, list) and all(isinstance(s, ast.stmt) for s in candidate):
            blocks.append(candidate)
    if isinstance(node, ast.Try):
        blocks.extend(handler.body for handler in node.handlers)
    return blocks


def _own_nodes(fn: ast.FunctionDef):
    """只走 fn 自己的语句，不进嵌套 def/lambda —— 那一层由它自己那一轮判。"""
    skip = {
        id(n)
        for n in ast.walk(fn)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and n is not fn
    }
    stack: list[ast.AST] = [fn]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if id(child) not in skip:
                stack.append(child)


def _writes_terminal_status(stmt: ast.Assign) -> bool:
    """`X.status = WorkspaceStatus.<终态>[.value]`，枚举名取 AST **根节点**。

    取径两半都是收紧：目标属性必须是 `status`（`w.state = ...` 不算），根节点必须是
    `WorkspaceStatus` 这个 Name（于是 `deployment.status = DeploymentStatus.FAILED.value`
    与 `w.status = "stopped"` 都不算，后者是已知盲区，见模块 docstring）。
    少写 `.value`（直接给枚举成员）照判——同一件事的另一种写法不该成为豁免。
    """
    if not any(isinstance(t, ast.Attribute) and t.attr == "status" for t in stmt.targets):
        return False
    attrs: list[str] = []
    value: ast.AST = stmt.value
    while isinstance(value, ast.Attribute):
        attrs.append(value.attr)
        value = value.value
    if not (isinstance(value, ast.Name) and value.id == WORKSPACE_STATUS):
        return False
    return bool(set(attrs) & TERMINAL)


def _closes_sessions(stmt: ast.stmt) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in CLOSERS
        for node in ast.walk(stmt)
    )


def _closed_before(stmt: ast.stmt, position: dict[int, tuple[list, int, ast.AST]]) -> bool:
    """从这条赋值往上逐层走，只看每层里排在它**前面**的兄弟语句。

    判"成对"只认在写的之前执行过的调用；`finally`/`except` 里的终结排在写之后运行，
    与"调用写在赋值之后"同一类，一律判为漏配对（这是本轮**定下的取径**，控制档
    `test_b_indirect_shapes_are_named` 里两档都按它钉）。爬到嵌套 def 的边界就停：
    外层函数里的一次终结不算内层那条写的证据。
    """
    current: ast.AST = stmt
    climbed: set[int] = set()
    while id(current) in position and id(current) not in climbed:
        climbed.add(id(current))
        block, index, holder = position[id(current)]
        if any(_closes_sessions(sibling) for sibling in block[:index]):
            return True
        current = holder
    return False


def _positions(fn: ast.FunctionDef) -> dict[int, tuple[list, int, ast.AST]]:
    """语句 → (它所在的语句列表, 序号, 持有该列表的节点)：既能取"前面的兄弟"也能往上一层。"""
    position: dict[int, tuple[list, int, ast.AST]] = {}
    for node in _own_nodes(fn):
        for block in _blocks_of(node):
            for index, stmt in enumerate(block):
                position[id(stmt)] = (block, index, node)
    return position


def _scan(tree: ast.AST, label: str, *, only_offenders: bool) -> set[str]:
    """尺子本体：`only_offenders=False` 时返回全部终态写入位点（语料分母）。"""
    found: set[str] = set()
    for fn in _defs(tree):
        position = _positions(fn)
        for stmt in _own_nodes(fn):
            if not (isinstance(stmt, ast.Assign) and _writes_terminal_status(stmt)):
                continue
            if only_offenders and _closed_before(stmt, position):
                continue
            found.add(f"{label}:{stmt.lineno}")
    return found


def terminal_writes_without_closing(source: str, *, label: str = "synthetic.py") -> set[str]:
    """这份源码里"写了 WorkspaceStatus 终态、却没在先它执行的语句里终结会话"的位点集合。

    位点串一律是 `<标签>:<行号>`，便于跨文件读数互相打脸时一眼定位。
    """
    return _scan(ast.parse(source), label, only_offenders=True)


def workspace_terminal_writes(source: str, *, label: str = "synthetic.py") -> set[str]:
    """这份源码里全部的 WorkspaceStatus 终态写入位点（不分有没有配对）。"""
    return _scan(ast.parse(source), label, only_offenders=False)


def app_ruler() -> dict[str, set[str]]:
    """全 app/ 跑一遍尺子，返回 {"offenders": …, "writes": …}，位点串是 `相对仓库根:行号`。"""
    offenders: set[str] = set()
    writes: set[str] = set()
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders |= _scan(tree, rel, only_offenders=True)
        writes |= _scan(tree, rel, only_offenders=False)
    return {"offenders": offenders, "writes": writes}


def _statement_at(site: str) -> str:
    """把 `相对路径:行号` 还原成那条语句的 AST 原文（行号漂移时失败消息能自证是哪一处）。"""
    rel, _, line = site.partition(":")
    tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and node.lineno == int(line):
            return ast.unparse(node)
    raise AssertionError(f"位点 {site} 已经不是一条赋值")


def test_b_the_app_wide_ruler_finds_every_terminal_write_paired():
    """全仓 `app/` 的每一处 `WorkspaceStatus` 终态写入，都必须自己带齐终结（闭 N-117）。

    期望写成**应然空集**，不写成"今天有哪几处漏"：草稿第一版钉的是 as-is 基线
    `{"app/services/orchestrator.py:590"}`，那会把这把尺子永远钉在"不认自己的修法"上，
    所以断言反转与修法同批落地（登记册里那条"随修法同批改判据"的规矩）。行号只进
    报错文案，不进判据 —— 本函数加一行就会让位点漂移。
    """
    readings = app_ruler()
    assert readings["offenders"] == set(), sorted(readings["offenders"])
    # 空集不许来自"什么都没数到"：分母自证。app/ 里 WorkspaceStatus 终态写入只有这两个文件、
    # 共 5 处；deployment.py 那三处 `DeploymentStatus.FAILED` 不在分母内（枚举名判别见 B 组末支）。
    per_file: dict[str, int] = {}
    for site in readings["writes"]:
        rel = site.rsplit(":", 1)[0]
        per_file[rel] = per_file.get(rel, 0) + 1
    assert per_file == {
        "app/services/orchestrator.py": 4,  # _finalize_stop 的 STOPPED、destroy 的 DELETED、_reconcile_one 两处 FAILED
        "app/services/warmpool.py": 1,  # claim 的撤销档，终结在同一块的在前兄弟里
    }, readings["writes"]
    # 位点身份：`_finalize_stop` 那一处确实被数进了分母，而且现在带着配对。
    stopped = [
        site for site in readings["writes"]
        if _statement_at(site) == "workspace.status = WorkspaceStatus.STOPPED.value"
    ]
    assert len(stopped) == 1, stopped


def test_b_paired_shapes_are_not_named():
    """不开火两档：同层在前、上层在前，都算"在先它执行的语句里终结过"。"""
    same_block = (
        "def _arm(db, w):\n"
        "    self.streaming.terminate_for_workspace(db, w.id)\n"
        "    self._settle_run_delta(db, w)\n"
        "    w.status = WorkspaceStatus.STOPPED.value\n"
    )
    assert terminal_writes_without_closing(same_block) == set(), same_block

    parent_block = (
        "def _arm(db, w):\n"
        "    error = self._stop_cleanup(db, w)\n"
        "    if error is None:\n"
        "        w.status = WorkspaceStatus.STOPPED.value\n"
    )
    assert terminal_writes_without_closing(parent_block) == set(), parent_block

    inside_try = (
        "def _arm(db, w):\n"
        "    self.streaming.terminate_for_workspace(db, w.id)\n"
        "    try:\n"
        "        w.status = WorkspaceStatus.STOPPED.value\n"
        "    except Exception:\n"
        "        db.rollback()\n"
    )
    assert terminal_writes_without_closing(inside_try) == set(), inside_try


def test_b_indirect_shapes_are_named():
    """开火四档：忘了、排在之后、`finally` 里、跨了嵌套 def 边界——都判漏配对。

    后两档是本轮**定下的取径**（不是实现顺手的产物）：尺子钉的是"写之前"，
    `finally` 与 handler 都在写之后运行，与"调用排在赋值之后"同一类；N-117 的教训恰好
    就是"终态已经落库、之后有没有人补做"不能信（`:421` 那一档就是没人补做的证据）。
    """
    forgets = (
        "def _arm(db, w):\n"
        "    self._settle_run_delta(db, w)\n"
        "    w.status = WorkspaceStatus.STOPPED.value\n"
    )
    assert terminal_writes_without_closing(forgets) == {"synthetic.py:3"}, forgets

    after_the_write = (
        "def _arm(db, w):\n"
        "    w.status = WorkspaceStatus.STOPPED.value\n"
        "    self.streaming.terminate_for_workspace(db, w.id)\n"
    )
    assert terminal_writes_without_closing(after_the_write) == {"synthetic.py:2"}, after_the_write

    closed_in_finally = (
        "def _arm(db, w):\n"
        "    try:\n"
        "        if ok:\n"
        "            w.status = WorkspaceStatus.STOPPED.value\n"
        "    finally:\n"
        "        self.streaming.terminate_for_workspace(db, w.id)\n"
    )
    assert terminal_writes_without_closing(closed_in_finally) == {"synthetic.py:4"}, (
        "内层 if + try/finally：终结排在写之后运行 ⇒ 判漏配对（本轮定的取径，见 docstring）"
    )

    closer_in_outer_function = (
        "def _arm(db, w):\n"
        "    self.streaming.terminate_for_workspace(db, w.id)\n"
        "    def _inner():\n"
        "        w.status = WorkspaceStatus.STOPPED.value\n"
        "    _inner()\n"
    )
    assert terminal_writes_without_closing(closer_in_outer_function) == {"synthetic.py:4"}, (
        "配对只在最近外层函数体内找，不跨 def 边界"
    )


def test_b_other_enums_and_other_targets_are_out_of_scope():
    """枚举名与目标属性的判别力：别的枚举、别的列、别的写法都不许算终态写入。"""
    other_enum = (
        "def _arm(db, d):\n"
        "    d.status = DeploymentStatus.FAILED.value\n"
    )
    assert terminal_writes_without_closing(other_enum) == set(), other_enum
    assert workspace_terminal_writes(other_enum) == set(), "枚举名判别也要管分母那一半"

    other_target = (
        "def _arm(db, w):\n"
        "    w.state = WorkspaceStatus.STOPPED.value\n"
    )
    assert terminal_writes_without_closing(other_target) == set(), other_target

    bare_member = (
        "def _arm(db, w):\n"
        "    w.status = WorkspaceStatus.DELETED\n"
    )
    assert terminal_writes_without_closing(bare_member) == {"synthetic.py:2"}, (
        "少写 .value 也照判：同一件事的另一种写法不该成为豁免"
    )

    # 已知盲区（模块 docstring 里写明，不假装看见）：常量字面量与经变量的间接写。
    literal = 'def _arm(db, w):\n    w.status = "stopped"\n'
    assert terminal_writes_without_closing(literal) == set(), "本尺按 AST 根节点判，不判字符串"
    via_name = (
        "def _arm(db, w):\n"
        "    settled = WorkspaceStatus.FAILED.value\n"
        "    w.status = settled\n"
    )
    assert terminal_writes_without_closing(via_name) == set(), "同上：`_fail` 那一处的形状"

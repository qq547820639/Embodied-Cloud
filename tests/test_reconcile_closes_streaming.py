"""runtime 失踪那一档要把串流会话一起终结（N-116）。

一手核实过的形状：`_stop_cleanup`（STOP 与"节点不一致"两档共用，`orchestrator.py:448`）
与 DESTROY（`:618`）都调 `streaming.terminate_for_workspace`，唯独 reconcile 的
MISSING 档不调——它结算、放卡、写 `FAILED`、记 `stopped_at`，却把
`streaming_sessions` 留成 `connected`，workspace 的 signal/media 端口也不清。
后果是两张表互相打脸（与 N-82 同族）：`GET /api/workspaces/{id}` 说这一格已经死了，
`GET /api/streaming/workspace/{id}` 与前端那一行端口还说它连着一条流。

判据分两组。行为面把"该终结的一起终结、不该动的不许动"钉成三极
（MISSING / ALIVE / UNKNOWN），再加一趟复跑证明它不重复入账。结构面把那句
"每一处把 workspace 写成终态的分支都必须先终结会话"做成尺子——本轮修的是**一处**
漏掉的配对；不钉成规则，下一处新加的终态分支照样会漏（这条循环已经长过四次了）。

会话行用 ORM 直接建：被测的是**终结**那一半，`streaming.start()` 要 RUNNING 之外的
整套所有权/凭据前置，在这里只会稀释判据。

尺子的作用面写死在 `_reconcile_one`：它只判"这条分支里写终态之前有没有终结过会话"，
不在被调函数体内找终结调用（`_stop_cleanup` 那一档靠"上层在前"命中，是因为调用点本身
在这一层）。于是"把 `_stop_cleanup` 内部那行终结删掉"这把变异不在本尺射程内——守着它
的是行为面的 `tests/test_streaming_lifecycle.py:137` 与 `:186`，N-116 电池 P3 实测两红。
"""

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    CreditLedger,
    Gpu,
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
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("reconcile-streaming"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)
ORCHESTRATOR_SRC = Path(__file__).resolve().parents[1] / "app" / "services" / "orchestrator.py"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


class ScriptedProvider(MockProvider):
    """每一格都按脚本说话（MISSING / ALIVE / UNKNOWN）。"""

    def __init__(self, base_url: str, *, state: RuntimeState) -> None:
        super().__init__(base_url)
        self.scripted = state
        self.calls = 0

    def reconcile(self, workspace: Workspace) -> RuntimeState:
        self.calls += 1
        return self.scripted


def _orchestrator(provider: MockProvider) -> WorkspaceOrchestrator:
    return WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-reconcile-streaming")  # noqa: S108 测试隔离目录
    )


@pytest.fixture(autouse=True)
def _clean_db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


def _seed_running_workspace() -> None:
    """一格 RUNNING 的 workspace + 一张 mock 卡 + 一条 connected 的会话（两侧都带端口）。"""
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


def _reading() -> tuple[str, str, int | None, int | None]:
    """(workspace.status, session.status, session.signal_port, workspace.signal_port)。"""
    with Factory() as db:
        ws = db.scalar(select(Workspace).where(Workspace.id == "ws-1"))
        session = db.scalar(select(StreamingSession).where(StreamingSession.id == "s-1"))
        return (
            str(ws.status),
            str(session.status),
            None if session.signal_port is None else int(session.signal_port),
            None if ws.signal_port is None else int(ws.signal_port),
        )


# ---------------------------------------------------------------------------
# 行为面：三极 + 复跑
# ---------------------------------------------------------------------------
def test_a_missing_runtime_also_closes_the_stream_session():
    _seed_running_workspace()
    provider = ScriptedProvider("http://mock", state=RuntimeState.MISSING)

    stats = _orchestrator(provider).reconcile_all()

    assert stats["failed"] == 1, stats
    ws_status, session_status, session_port, workspace_port = _reading()
    assert ws_status == WorkspaceStatus.FAILED.value
    assert session_status == StreamingStatus.FAILED.value, "会话不得留在 connected"
    assert session_port is None and workspace_port is None, "两侧端口都要交还"
    with Factory() as db:
        session = db.scalar(select(StreamingSession).where(StreamingSession.id == "s-1"))
        assert session.error_message == "workspace stopped", session.error_message
        gpu = db.scalar(select(Gpu))
        assert gpu.status == GpuStatus.AVAILABLE.value and gpu.workspace_id is None


def test_a_live_runtime_leaves_the_session_connected():
    """不开火对照：adopt 那一档什么都不该终结。"""
    _seed_running_workspace()
    provider = ScriptedProvider("http://mock", state=RuntimeState.ALIVE)

    stats = _orchestrator(provider).reconcile_all()

    assert stats["kept"] == 1, stats
    ws_status, session_status, session_port, workspace_port = _reading()
    assert ws_status == WorkspaceStatus.RUNNING.value
    assert session_status == StreamingStatus.CONNECTED.value
    assert session_port == 8887 and workspace_port == 8887


def test_an_unknown_runtime_touches_nothing():
    """ADR 0008：问不到不是缺席——不写终态，也不许顺手终结会话。"""
    _seed_running_workspace()
    provider = ScriptedProvider("http://mock", state=RuntimeState.UNKNOWN)

    stats = _orchestrator(provider).reconcile_all()

    assert (stats["failed"], stats["kept"]) == (0, 0), stats
    ws_status, session_status, session_port, workspace_port = _reading()
    assert (ws_status, session_status) == (
        WorkspaceStatus.RUNNING.value,
        StreamingStatus.CONNECTED.value,
    )
    assert session_port == 8887 and workspace_port == 8887


def test_a_second_pass_does_not_double_count_or_reclose():
    """复跑：那一格已成终态 ⇒ 不再被扫，也不产生第二笔结算。"""
    _seed_running_workspace()
    orchestrator = _orchestrator(ScriptedProvider("http://mock", state=RuntimeState.MISSING))
    first = orchestrator.reconcile_all()
    second = orchestrator.reconcile_all()

    assert first["failed"] == 1 and second["failed"] == 0, (first, second)
    assert second["scanned"] == 0, second
    with Factory() as db:
        entries = db.scalars(
            select(CreditLedger).where(CreditLedger.type == LedgerType.USAGE.value)
        ).all()
        assert len(entries) <= 1, [entry.idempotency_key for entry in entries]


# ---------------------------------------------------------------------------
# 结构面：终态写入必须与"先它执行的终结"成对
# ---------------------------------------------------------------------------
TERMINAL = {"FAILED", "STOPPED", "DELETED"}
CLOSERS = {"terminate_for_workspace", "_stop_cleanup"}


def terminal_writes_without_closing(source: str) -> list[str]:
    """`_reconcile_one` 里把 workspace 写成终态、却没有在先它执行的语句里终结过会话的位置。

    配对条件刻意收紧成**"同一条路径上、写之前"**：从这条赋值往上一层层走，只看每层里
    排在它前面的兄弟语句。于是
    - MISSING 档：赋值之前同层就是 `terminate_for_workspace(...)` ⇒ 成对；
    - "节点不一致"档：赋值嵌在内层 `if error is None:` 里，那一层没有终结调用，
      但上一层排在内层 If 之前的是 `error = self._stop_cleanup(db, w)`（它内部就终结）⇒ 成对；
    - "祖先块里出现过一次调用"不算证据：调用排在写之后仍然是漏配对，所以按顺序判。
    """
    tree = ast.parse(source)
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_reconcile_one"),
        None,
    )
    if fn is None:
        return ["_reconcile_one 不存在"]
    # 语句 → (它所在的语句列表, 序号, 持有该列表的节点)：既能取"前面的兄弟"也能往上一层
    position: dict[int, tuple[list, int, ast.AST]] = {}
    for node in ast.walk(fn):
        for block in _blocks_of(node):
            for index, stmt in enumerate(block):
                position[id(stmt)] = (block, index, node)
    offenders: list[str] = []
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign) and _writes_terminal_status(stmt) and not _closed_before(
            stmt, position
        ):
            offenders.append(f"L{stmt.lineno}: 写了终态却没在先它执行的语句里终结会话")
    return sorted(offenders)


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


def _writes_terminal_status(stmt: ast.Assign) -> bool:
    if not any(isinstance(t, ast.Attribute) and t.attr == "status" for t in stmt.targets):
        return False
    chain: list[str] = []
    value: ast.AST = stmt.value
    while isinstance(value, ast.Attribute):
        chain.append(value.attr)
        value = value.value
    if isinstance(value, ast.Constant):
        chain.append(str(value.value))
    return bool(set(chain) & TERMINAL)


def _closes_sessions(stmt: ast.stmt) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in CLOSERS
        for node in ast.walk(stmt)
    )


def _closed_before(stmt: ast.stmt, position: dict[int, tuple[list, int, ast.AST]]) -> bool:
    current: ast.AST = stmt
    climbed: set[int] = set()
    while id(current) in position and id(current) not in climbed:
        climbed.add(id(current))
        block, index, holder = position[id(current)]
        if any(_closes_sessions(sibling) for sibling in block[:index]):
            return True
        current = holder
    return False


def test_every_terminal_write_in_reconcile_closes_the_session():
    assert terminal_writes_without_closing(ORCHESTRATOR_SRC.read_text(encoding="utf-8")) == []


def test_the_structural_ruler_fires_on_a_terminal_write_that_forgets():
    """反向对照：只写终态的新分支必须点名；同层在前／上层在前都算成对；排在之后不算。"""
    forgetting = (
        "def _reconcile_one(db, w, stats):\n"
        "    if state == RuntimeState.MISSING:\n"
        "        self._settle_running_segment(db, w)\n"
        "        w.status = WorkspaceStatus.FAILED.value\n"
        "    elif state == RuntimeState.DRAINING:\n"
        "        w.status = WorkspaceStatus.STOPPED.value\n"
    )
    assert len(terminal_writes_without_closing(forgetting)) == 2, forgetting

    paired = (
        "def _reconcile_one(db, w, stats):\n"
        "    if state == RuntimeState.MISSING:\n"
        "        self.streaming.terminate_for_workspace(db, w.id)\n"
        "        w.status = WorkspaceStatus.FAILED.value\n"
        "    if node_mismatch:\n"
        "        error = self._stop_cleanup(db, w)\n"
        "        if error is None:\n"
        "            w.status = WorkspaceStatus.FAILED.value\n"
    )
    assert terminal_writes_without_closing(paired) == [], "同层在前与上层在前都该算成对"

    after_the_write = (
        "def _reconcile_one(db, w, stats):\n"
        "    if state == RuntimeState.MISSING:\n"
        "        w.status = WorkspaceStatus.FAILED.value\n"
        "        self.streaming.terminate_for_workspace(db, w.id)\n"
    )
    assert len(terminal_writes_without_closing(after_the_write)) == 1, "调用排在写之后不算成对"

"""被释放准入拒下的格要有周期驱动者（N-99，闭登记项 N-98）。

N-96 把「存在但没在跑」从 MISSING 改成 UNKNOWN 之后，代价就露出来了：释放准入拒下那一格
（不给卡），STOP operation 打满 `OperationWorker.MAX_ATTEMPTS = 3` 之后**进程内没有任何人再试**——
`reconcile_all` 里没有 UNKNOWN 分支（只有一行注释「UNKNOWN：无真实 runtime 可判定，保守不动」），
`reconcile_all` 本身也不是周期任务，而 `OperationType.RECONCILE` 有消费者（orchestrator.py 的
`reconcile_all()`）却没有任何入队点（本轮现算 grep：除消费者那一行外，app/ 与 scripts/ 零命中）。
⇒ 一张卡可以永久钉在一个已经没人用的 runtime 上。

本轮给的就是那个驱动者，形状是**定向档**而不是全量扫描：只碰「队列没有在做」且「比阈值老」的格，
一趟最多看 `RECONCILE_CELLS_PER_PASS` 格。理由不是审美：这一趟跑在 worker 线程里，每格一次
provider 往返（本机实测 `docker inspect` 中位 93.6 ms／p95 217 ms，见 N-91 的取证），
不设上界时一格慢就把同线程里用户的 start/stop 一起拖住——那正是登记项里排除
「给每格入队一条 RECONCILE operation」的原因（同 workspace 的活跃 operation 会被串行门挡住，
后台重试会饿死用户动作）。

`reconcile_all` 的两个新参数默认 None ⇒ 默认档就是改前的全量扫描，启动恢复
（`main.py` → `run_crash_recovery`）与既有判据走的就是这一档；这条由
`test_the_default_mode_is_still_a_full_sweep` 反向钉住，免得"加了过滤器"被读成"扫描变小了没人发现"。
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    Gpu,
    GpuStatus,
    OperationStatus,
    OperationType,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.base import RuntimeState
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.services.worker import OperationWorker
from app.utils import utcnow
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("reconcile-driver"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)
REPO_ROOT = Path(__file__).resolve().parents[1]
DEPS_SRC = REPO_ROOT / "app" / "deps.py"
ORCH_SRC = REPO_ROOT / "app" / "services" / "orchestrator.py"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


class SequencedProvider(MockProvider):
    """按 workspace 给出一串 runtime 事实（用完就重复最后一档），并记下被问过几次。"""

    def __init__(self, *, script: dict[str, list[RuntimeState]]) -> None:
        super().__init__("http://127.0.0.1:8000")
        self.script = {wid: list(states) for wid, states in script.items()}
        self.observations: list[str] = []

    def reconcile(self, workspace: Workspace) -> RuntimeState:
        self.observations.append(workspace.id)
        states = self.script.get(workspace.id)
        if not states:
            return RuntimeState.UNKNOWN
        return states.pop(0) if len(states) > 1 else states[0]

    def stop(self, workspace: Workspace) -> None:  # 定向档不许真的去停东西
        return None


def _orchestrator(provider: MockProvider) -> WorkspaceOrchestrator:
    return WorkspaceOrchestrator(
        Factory,
        provider,
        Path("/tmp/test-reconcile-driver"),  # noqa: S108 测试隔离目录
    )


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    ENGINE.dispose()


def _seed_host(cells: int) -> None:
    """一趟种好：一个用户、一个模板、`cells` 张 mock 卡（列在 `gpu-0…gpu-(n-1)`）。"""
    with Factory() as db:
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
        GpuScheduler(Factory).sync_host(
            db,
            host_id="host-1",
            name="h1",
            address="127.0.0.1",
            provider="mock",
            gpus=[
                GpuInfo(gpu_uuid=f"gpu-{i}", model=f"RTX-{i}", memory_total=24564, index=i)
                for i in range(cells)
            ],
        )
        db.commit()


def _seed_cell(
    wid: str,
    *,
    status: str,
    age_seconds: int,
    gpu_index: int | None = 0,
    active_operation: str | None = None,
) -> None:
    """造一格：指定状态、指定「最后一次状态变化」有多老，可选挂一条活跃 operation。

    `created_at` 与 `started_at` 都按 age_seconds 往前挪：定向档的时间基准是
    `stopped_at or started_at or created_at`，这里要让年龄在两个字段上都成立，
    否则判据会退化成"恰好某一列碰上了阈值"。
    """
    with Factory() as db:
        # 阈值量的是"距今多久"，所以基准必须是真实时钟而不是 BASE（BASE 在 1 月，
        # 拿它当锚的话每一格都会老到远超阈值，阈值判据就成了恒真）。
        when = utcnow() - timedelta(seconds=age_seconds)
        db.add(
            Workspace(
                id=wid,
                name=wid,
                template_id="cartpole",
                provider="mock",
                user_id="u1",
                status=status,
                created_at=when,
                started_at=when,
            )
        )
        db.commit()
        if gpu_index is not None:
            gpu = db.scalar(select(Gpu).where(Gpu.gpu_uuid == f"gpu-{gpu_index}"))
            assert gpu is not None, f"没先 _seed_host({gpu_index + 1})，卡 {gpu_index} 不在场"
            gpu.status = GpuStatus.ALLOCATED.value
            gpu.workspace_id = wid
            ws = db.get(Workspace, wid)
            ws.gpu_id = gpu.id
            ws.gpu_index = gpu.gpu_index
            ws.gpu_name = gpu.model
            db.commit()
        if active_operation is not None:
            db.add(
                WorkspaceOperation(
                    id=f"op-{wid}",
                    workspace_id=wid,
                    operation_type=OperationType.STOP.value,
                    status=active_operation,
                    attempts=1,
                )
            )
            db.commit()


def _gpu_free(wid: str) -> bool:
    with Factory() as db:
        gpu = db.scalar(select(Gpu).where(Gpu.workspace_id == wid))
        return gpu is None


def _status(wid: str) -> str:
    with Factory() as db:
        return db.get(Workspace, wid).status


def bounded_pass(provider: MockProvider, *, limit=None, older_than_seconds=None):
    """按定向档跑一趟（就是 `deps._reconcile_stuck_cells` 的形状，参数在这里显式给）。"""
    return _orchestrator(provider).reconcile_all(
        limit=limit, older_than_seconds=older_than_seconds
    )


# ---------------------------------------------------------------------------
# A 接线：驱动者真的在周期表上，且用的是有界档
# ---------------------------------------------------------------------------


def test_the_periodic_schedule_has_a_reconcile_driver() -> None:
    """A1 `app.deps` 装配出来的 worker 周期表里真的有这一格，间隔取自常量而不是手抄数字。

    这条是"写了没人接"的解毒剂：登记项 N-98 的整条成因就是机制在场而无触发者。
    """
    from app import deps

    tasks = deps.worker.periodic_tasks
    callables = [c for _every, c in tasks]
    assert deps._reconcile_stuck_cells in callables, (
        f"周期表里没有 reconcile 驱动者：{[getattr(c, '__name__', c) for c in callables]}"
    )
    every = [n for n, c in tasks if c is deps._reconcile_stuck_cells]
    assert every == [OperationWorker.PERIODIC_RECONCILE_EVERY], (
        f"驱动者的间隔必须取自常量，实际 {every}（常量 = "
        f"{OperationWorker.PERIODIC_RECONCILE_EVERY}）"
    )
    # 恒等而非相等：驱动者调的必须是**那个** orchestrator 单例的方法，
    # 不是某个自己 new 出来的 orchestrator（§8 立的规矩）。
    # 真跑一次：`deps` 用的是 app 自己的会话工厂，表要由 app 的 lifespan 建出来，
    # 所以这一格借 TestClient 起一次装配（不是起 worker 线程，周期表只是被读了一遍）。
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app):
        result = deps._reconcile_stuck_cells()
    assert isinstance(result, dict) and "scanned" in result, result


def test_the_driver_asks_for_the_bounded_mode() -> None:
    """A2 结构：`_reconcile_stuck_cells` 调 reconcile_all 时两个界参数都给了，且给的是常量。"""
    offenders = bounded_mode_offenders(DEPS_SRC.read_text(encoding="utf-8"))
    assert offenders == [], f"驱动者没走有界档：{offenders}"


def bounded_mode_offenders(source: str) -> list[str]:
    """`_reconcile_stuck_cells` 里对 `reconcile_all` 的调用缺了哪个界参数（或把它写成了字面量）。

    两个界参数各有含义，缺任何一个都改变成本形状：缺 `older_than_seconds` 就变成每趟全量扫，
    缺 `limit` 就没有上界。写成字面量则让 `OperationWorker` 的常量变成没人读的装饰。
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "_reconcile_stuck_cells":
            calls = [
                c
                for c in ast.walk(node)
                if isinstance(c, ast.Call)
                and (
                    (isinstance(c.func, ast.Attribute) and c.func.attr == "reconcile_all")
                    or (isinstance(c.func, ast.Name) and c.func.id == "reconcile_all")
                )
            ]
            if not calls:
                offenders.append("D1:没有 reconcile_all 调用")
                continue
            for call in calls:
                kwargs = {k.arg for k in call.keywords}
                for needed in ("limit", "older_than_seconds"):
                    if needed not in kwargs:
                        offenders.append(f"D2:缺关键字 {needed}")
                        continue
                    value = next(k.value for k in call.keywords if k.arg == needed)
                    if isinstance(value, ast.Constant):
                        offenders.append(f"D3:{needed} 写成字面量")
    if not offenders and "_reconcile_stuck_cells" not in source:
        offenders.append("D4:驱动者函数不存在")
    return offenders


def test_the_bounded_mode_ruler_can_see_every_break():
    """A3 判据自测：三种破坏形状各自开火，合规写法不开火。"""
    good = (
        "def _reconcile_stuck_cells():\n"
        "    return orchestrator.reconcile_all(\n"
        "        limit=OperationWorker.RECONCILE_CELLS_PER_PASS,\n"
        "        older_than_seconds=OperationWorker.RECONCILE_STUCK_OLDER_THAN_SECONDS,\n"
        "    )\n"
    )
    assert bounded_mode_offenders(good) == []
    assert "D2:缺关键字 limit" in bounded_mode_offenders(
        "def _reconcile_stuck_cells():\n"
        "    return orchestrator.reconcile_all(older_than_seconds=60)\n"
    )
    assert "D3:limit 写成字面量" in bounded_mode_offenders(
        "def _reconcile_stuck_cells():\n"
        "    return orchestrator.reconcile_all(limit=8, older_than_seconds=60)\n"
    )
    assert bounded_mode_offenders(
        "def _reconcile_stuck_cells():\n    return {}\n"
    ) == ["D1:没有 reconcile_all 调用"]
    assert bounded_mode_offenders("def other():\n    return 1\n") == ["D4:驱动者函数不存在"]


# ---------------------------------------------------------------------------
# B 行为：被放下的格真的被重新拿起来
# ---------------------------------------------------------------------------


def test_a_refused_stopping_cell_is_picked_up_once_the_queue_gives_up() -> None:
    """B1 STOPPING＋队列已放弃（无活跃 operation）＋provider 现在说 MISSING ⇒ 收敛并放卡。

    这一条就是 N-98 描述的那一站：operation 打满 attempts 之后没人再试，卡被永久钉住。
    """
    _seed_host(1)
    _seed_cell(
        "ws-refused",
        status=WorkspaceStatus.STOPPING.value,
        age_seconds=600,
    )
    provider = SequencedProvider(script={"ws-refused": [RuntimeState.MISSING]})

    stats = bounded_pass(provider, limit=8, older_than_seconds=60)

    assert provider.observations == ["ws-refused"], provider.observations
    assert stats["stopped"] == 1, stats
    assert _status("ws-refused") == WorkspaceStatus.STOPPED.value
    assert _gpu_free("ws-refused"), "格收住了但卡没回池：那是 N-82 的同类"
    assert stats["skipped"] == 0 and stats["scanned"] == 1, stats


def test_a_cell_the_queue_is_still_working_on_is_left_alone() -> None:
    """B2 反向极点：同格挂一条 PENDING 的 STOP ⇒ 驱动者不许碰它（队列拥有它）。

    没有这一极，「周期驱动者」就会变成第二个判决源：worker 正在执行的格被后台再判一次，
    两边的提交互相回退，而且后台那一脚还会绕过 attempts/lease 的记账。
    """
    _seed_host(1)
    _seed_cell(
        "ws-busy",
        status=WorkspaceStatus.STOPPING.value,
        age_seconds=600,
        active_operation=OperationStatus.PENDING.value,
    )
    provider = SequencedProvider(script={"ws-busy": [RuntimeState.MISSING]})

    stats = bounded_pass(provider, limit=8, older_than_seconds=60)

    assert provider.observations == [], f"队列在做的格被后台抢了：{provider.observations}"
    assert stats["skipped"] == 1 and stats["scanned"] == 0, stats
    assert _status("ws-busy") == WorkspaceStatus.STOPPING.value
    assert not _gpu_free("ws-busy"), "被跳过的格不该顺带把卡放了"


def test_a_failed_operation_releases_the_cell_back_to_the_driver() -> None:
    """B3 阈值另一极：attempts 打满变 FAILED（不再是活跃态）之后，下一趟必须接手。

    这条把「活跃」的边界钉在 pending/running/retrying 三态上——登记项说的"没人再试"
    正是在 FAILED 之后发生的，如果 FAILED 也算活跃，驱动者就等于没修。
    """
    _seed_host(1)
    _seed_cell(
        "ws-givenup",
        status=WorkspaceStatus.STOPPING.value,
        age_seconds=600,
        active_operation=OperationStatus.FAILED.value,
    )
    provider = SequencedProvider(script={"ws-givenup": [RuntimeState.MISSING]})

    stats = bounded_pass(provider, limit=8, older_than_seconds=60)

    assert provider.observations == ["ws-givenup"], provider.observations
    assert stats["stopped"] == 1 and _status("ws-givenup") == WorkspaceStatus.STOPPED.value


def test_the_age_threshold_keeps_fresh_cells_out_of_the_pass() -> None:
    """B4 成本极点：比阈值新的格这一趟不看（阈值是"别追刚动的格"，不是"永远不看"）。"""
    _seed_host(2)
    _seed_cell("ws-fresh", status=WorkspaceStatus.RUNNING.value, age_seconds=5)
    _seed_cell("ws-old", status=WorkspaceStatus.RUNNING.value, age_seconds=600, gpu_index=1)
    provider = SequencedProvider(
        script={"ws-fresh": [RuntimeState.ALIVE], "ws-old": [RuntimeState.ALIVE]}
    )

    stats = bounded_pass(provider, limit=8, older_than_seconds=60)

    assert provider.observations == ["ws-old"], f"太新的格也被问了：{provider.observations}"
    assert stats["skipped"] == 1 and stats["scanned"] == 1, stats
    assert _status("ws-fresh") == WorkspaceStatus.RUNNING.value


def test_a_pass_is_bounded_to_the_configured_number_of_cells() -> None:
    """B5 上界：五格都老，limit=2 ⇒ 只看最早的两格，其余留给下一趟。

    上界是给同线程的用户操作留余量的那一只手；次序由 created_at 决定，所以"哪两格"是确定的。
    """
    _seed_host(5)
    for i in range(5):
        _seed_cell(
            f"ws-{i}",
            status=WorkspaceStatus.RUNNING.value,
            age_seconds=660 - i,
            gpu_index=i,
        )
    provider = SequencedProvider(script={f"ws-{i}": [RuntimeState.ALIVE] for i in range(5)})

    stats = bounded_pass(provider, limit=2, older_than_seconds=60)

    assert stats["scanned"] == 2, stats
    # created_at 升序＝最老的先看；ws-0 是六百米前那格
    assert provider.observations == ["ws-0", "ws-1"], provider.observations


def test_the_default_mode_is_still_a_full_sweep() -> None:
    """B6 默认档不许被过滤器改掉：不传参数时，新格与队列在做的格都照看。

    启动恢复（`run_crash_recovery`）走的就是这一档；如果默认值被写成"有界"，
    崩溃恢复就会漏格，而漏掉的格子恰好是本轮要修的那一类。
    """
    _seed_host(2)
    _seed_cell("ws-fresh2", status=WorkspaceStatus.RUNNING.value, age_seconds=5)
    _seed_cell(
        "ws-busy2",
        status=WorkspaceStatus.RUNNING.value,
        age_seconds=600,
        gpu_index=1,
        active_operation=OperationStatus.PENDING.value,
    )
    provider = SequencedProvider(
        script={"ws-fresh2": [RuntimeState.ALIVE], "ws-busy2": [RuntimeState.ALIVE]}
    )

    stats = _orchestrator(provider).reconcile_all()

    assert sorted(provider.observations) == ["ws-busy2", "ws-fresh2"], provider.observations
    assert stats["skipped"] == 0 and stats["scanned"] == 2, stats


# ---------------------------------------------------------------------------
# C 持久化那条路不许被这轮拆掉
# ---------------------------------------------------------------------------


def reconcile_consumer_present(source: str) -> bool:
    """分发器里还有 `OperationType.RECONCILE` → `reconcile_all()` 这一档吗。"""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.If):
            continue
        if "RECONCILE" not in ast.dump(node.test):
            continue
        if any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "reconcile_all"
            for c in ast.walk(node)
        ):
            return True
    return False


def test_the_durable_reconcile_consumer_is_still_wired() -> None:
    """C1 本轮是"补生产者"，不是"把持久化那条路换成周期档"：消费者必须在场。"""
    assert reconcile_consumer_present(ORCH_SRC.read_text(encoding="utf-8"))


def test_the_consumer_ruler_can_see_the_branch_being_removed() -> None:
    """C2 判据自测：把那一档的调用换成 pass，尺子要塌下来。"""
    src = ORCH_SRC.read_text(encoding="utf-8")
    marker = "            self.reconcile_all()\n"
    assert src.count(marker) == 1, f"锚点命中 {src.count(marker)} 次，不是 1"
    assert reconcile_consumer_present(src.replace(marker, "            pass\n")) is False
    assert reconcile_consumer_present(src) is True

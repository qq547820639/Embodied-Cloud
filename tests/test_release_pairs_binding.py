"""`GpuScheduler.release` 放卡时必须把那一格的绑定一起清掉（N-86）。

缺陷形状：放卡这件事在代码里有三个写点，只有回收器那条批量路径成对。
`release`（唯一的分配权威）只写 `gpus` 与 `gpu_allocations`，把「格上还剩
`gpu_id/gpu_index/gpu_name`」留给调用方自觉 —— 于是：

- `_fail` 自己清了（`orchestrator.py:386-388`，N-81 立的形状），
- `_stop_cleanup` 的放行档（`orchestrator.py:573-585` 里 `scheduler.release` 之后只动
  `status/stopped_at/started_at`）**不清**，
- warm pool 认领撤销的放行档（`warmpool.py:396-408`）清了 `container_name` 与三个端口，
  也**不清**这三列。

后果不是报表难看，是**这类漂移回收器看不见**：`recover_stuck_gpu_allocations` 顺着
`gpu_allocations` 与 `Gpu.workspace_id` 找孤儿，而这两处放掉之后既没有分配行、
`Gpu.workspace_id` 也不再指向那一格 ⇒ 一张已易主的卡被一格 STOPPED/FAILED 的行继续"持有"，
读者是 `schemas.py:104` 一路到 API 与 `static/app.js`，以及 `warmpool.py:559-570` 的
unbooked inflight 计数（它按 `gpu_id IS NULL` 判断「这一格还会去占卡」）。

修法取「谁放卡谁成对」：清列收进 `release` 本身，而不是在第三个调用点再手写一遍。
写在 `release` 里必须是 **ORM 赋值**而不是批量 UPDATE —— 调用方手上正拿着同一个
Workspace 实例，批量语句绕过身份图，读者在同一会话里仍会看到旧值（这正是本文件
`test_release_clears_the_binding_in_the_callers_own_session` 要分两极的原因）。
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
    GpuAllocation,
    GpuHost,
    GpuStatus,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceStatus,
)
from app.services.ledger import CreditLedgerService
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.services.streaming import StreamingSessionService
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("release-pairs-binding"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)
REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_SRC = REPO_ROOT / "app" / "services" / "scheduler.py"


@pytest.fixture(autouse=True)
def _db():
    """每例重建表并灌一份够用的世界：一张 mock 卡 + cartpole 模板 + 学生 u1。"""
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
        db.commit()
    yield
    Base.metadata.drop_all(ENGINE)


def _workspace(db, *, workspace_id: str = "ws-1", status: str = WorkspaceStatus.RUNNING.value) -> Workspace:
    ws = Workspace(
        id=workspace_id,
        name="bind",
        template_id="cartpole",
        provider="mock",
        user_id="u1",
        status=status,
        started_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    db.add(ws)
    db.commit()
    return ws


def _hold_a_card(db, workspace_id: str) -> Gpu:
    """真走分配权威占住那张卡：格上的三列由调用方按生产的形状写上去。"""
    gpu = GpuScheduler(Factory).allocate(db, workspace_id, gpu_requirement_gb=8)
    ws = db.get(Workspace, workspace_id)
    ws.gpu_id = gpu.id
    ws.gpu_index = gpu.gpu_index
    ws.gpu_name = gpu.model
    db.commit()
    return gpu


def _orchestrator() -> WorkspaceOrchestrator:
    return WorkspaceOrchestrator(
        Factory,
        MockProvider("http://127.0.0.1:8000"),
        Path("/tmp/test-release-pairs-binding"),  # noqa: S108 测试隔离目录
        streaming=StreamingSessionService(Factory),
        ledger=CreditLedgerService(Factory),
    )


def _binding(db, workspace_id: str) -> tuple[object, object, object]:
    ws = db.get(Workspace, workspace_id)
    return ws.gpu_id, ws.gpu_index, ws.gpu_name


# ---------------------------------------------------------------------------
# C1 行为：release 之后，同一会话与新会话都必须读到空绑定
# ---------------------------------------------------------------------------


def test_release_clears_the_binding_in_the_callers_own_session() -> None:
    """同一会话里读：批量 UPDATE 绕过身份图，这一极专治「写了 SQL 但调用方还看见旧值」。"""
    scheduler = GpuScheduler(Factory)
    with Factory() as db:
        ws = _workspace(db)
        _hold_a_card(db, ws.id)
        assert _binding(db, ws.id)[0] == ws.gpu_id, "前提：这一格确实占着卡"
        holder = db.get(Workspace, ws.id)
        scheduler.release(db, ws.id)
        assert _binding(db, ws.id) == (None, None, None), (
            f"release 之后同一会话仍读到绑定：{_binding(db, ws.id)}"
        )
        assert holder.gpu_id is None


def test_release_clears_the_binding_for_a_fresh_reader() -> None:
    """换会话读（落库为证）：卡的归属、分配行、格上绑定必须一起改口。"""
    scheduler = GpuScheduler(Factory)
    with Factory() as db:
        ws = _workspace(db)
        gpu = _hold_a_card(db, ws.id)
        scheduler.release(db, ws.id)

    with Factory() as db:
        assert _binding(db, ws.id) == (None, None, None)
        card = db.get(Gpu, gpu.id)
        assert card.status == GpuStatus.AVAILABLE.value and card.workspace_id is None
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == ws.id)) is None


def test_release_of_a_ghost_workspace_id_frees_the_card_without_raising() -> None:
    """格子不存在（ghost 分配）时 release 仍是幂等可用的：清列那一步必须让得开空。"""
    scheduler = GpuScheduler(Factory)
    with Factory() as db:
        ws = _workspace(db)
        gpu = _hold_a_card(db, ws.id)
        db.delete(db.get(Workspace, ws.id))
        db.commit()

        scheduler.release(db, "ws-1")

        card = db.get(Gpu, gpu.id)
        assert card.status == GpuStatus.AVAILABLE.value, "ghost 档把放卡也一起弄坏了"


# ---------------------------------------------------------------------------
# C2 那两个原先不清列的调用点：现在经 release 成对
# ---------------------------------------------------------------------------


def test_stop_path_leaves_no_binding_on_a_settled_workspace() -> None:
    """`_stop_cleanup` 的放行档：provider 认账 ⇒ 卡回池且这一格不再声称持有它。

    改前这里 release 之后只动 `status/stopped_at/started_at`，三列原样留着。
    """
    orchestrator = _orchestrator()
    with Factory() as db:
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = ws.id
    orchestrator._start(wid)
    with Factory() as db:
        assert db.get(Workspace, wid).status == WorkspaceStatus.RUNNING.value, (
            "前提未达成：provision 没把 workspace 带到 RUNNING"
        )
        assert _binding(db, wid)[0] is not None, "前提未达成：这一格没绑上卡"
    with Factory() as db:
        orchestrator.stop(db, db.get(Workspace, wid))

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.STOPPED.value
        assert _binding(db, wid) == (None, None, None), f"停止之后绑定仍在：{_binding(db, wid)}"
        assert db.scalar(select(Gpu).where(Gpu.status == GpuStatus.ALLOCATED.value)) is None


def test_release_under_the_reclaimer_scope_clears_only_the_cell_it_freed() -> None:
    """回收器与 release 的分工不许互相踩：受保护的格（RUNNING）留着绑定。

    这一支是给"顺手写成全局清列"那个变异留的反证（N-84 里已经证过回收器那一侧，
    这里钉 release 加入之后整个不变量仍然只作用于被放掉的那一格）。
    """
    with Factory() as db:
        protected = _workspace(db, workspace_id="ws-keep", status=WorkspaceStatus.PROVISIONING.value)
        card = _hold_a_card(db, protected.id)
        host = db.get(GpuHost, card.host_id)
        assert host is not None

        from app.services.scheduler import recover_stuck_gpu_allocations

        recover_stuck_gpu_allocations(db)

        # PROVISIONING 在保护集内：卡与绑定都该原样留着
        assert _binding(db, protected.id)[0] == card.id
        assert db.get(Gpu, card.id).status == GpuStatus.ALLOCATED.value


# ---------------------------------------------------------------------------
# C3 结构：放卡的写点与清列同在一个函数里
# ---------------------------------------------------------------------------


def cleared_binding_columns(source: str, function_name: str) -> set[str]:
    """列出 `function_name` 的函数体里被赋成 None 的 `gpu_id/gpu_index/gpu_name` 属性名。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            for stmt in ast.walk(node):
                if (
                    isinstance(stmt, ast.Assign)
                    and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value is None
                ):
                    for target in stmt.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and target.attr in {"gpu_id", "gpu_index", "gpu_name"}
                        ):
                            found.add(target.attr)
    return found


def test_release_is_the_authority_that_pairs_the_columns() -> None:
    """`release` 自己必须清那三列——调用点各写一遍就是这条不变量的第三份副本。"""
    source = SCHEDULER_SRC.read_text(encoding="utf-8")
    assert cleared_binding_columns(source, "release") == {"gpu_id", "gpu_index", "gpu_name"}, (
        "release 不再成对清列：格上的绑定会重新变成没人负责的第二次改口"
    )


def test_the_column_ruler_can_see_the_unpaired_shape() -> None:
    """反向对照：把 release 换成"只动两张表"的旧形状，尺子必须什么都认不出。"""
    unpaired = (
        "def release(db, workspace_id):\n"
        "    gpu.status = AVAILABLE\n"
        "    db.commit()\n"
    )
    assert cleared_binding_columns(unpaired, "release") == set()
    paired = (
        "def release(db, workspace_id):\n"
        "    holder.gpu_id = None\n"
        "    holder.gpu_index = None\n"
        "    holder.gpu_name = None\n"
    )
    assert cleared_binding_columns(paired, "release") == {"gpu_id", "gpu_index", "gpu_name"}
    # 别的函数里清列不算数（防止把不变量写到隔壁去）
    assert cleared_binding_columns("def other():\n    w.gpu_id = None\n", "release") == set()

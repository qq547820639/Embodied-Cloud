"""release 自己抛错留下的那一格，要能被 N-99 的驱动者收回来（N-101）。

形状（读 `app/services/orchestrator.py:367-389` 的放行档看到的）：释放准入已经放行
（provider 认账 runtime 不在），于是 `_fail` 清掉格子上的三列并把卡交给 `scheduler.release`——
可 `release` 自己抛错了。这里的处理是"回滚 + 重新置位 + 继续清列 + 记一条 will be reconciled"，
也就是**故意**留下一个跨表冲突：`Gpu.workspace_id` 还指着这格、卡还是 ALLOCATED，
而格子上的 `gpu_id/gpu_index/gpu_name` 已经空了。

这不是"错了没管"——前提是它会被补做。可"会被补做"这句话此前没有任何常驻判据：
既有那支 `tests/test_provision_rollback.py:396-397` 只断到"格子侧三样是空的 + 状态是 FAILED"，
从不回头看卡那一行，也从不跑那个"补做"的入口。普查（N-100 引出的同一轮）也点名了这一格。

本轮把它钉成三档：

1. 抛错之后，冲突必须是**可点名**的（`tests/gpu_drift.py` 那把尺子从 `card-points-at-holder`
   这一侧量到它），且卡不许已经被当成空闲——否则就是在骗分配器。
2. 同格还挂着**活跃 operation**（这里用 RUNNING）时，驱动者不许动这张卡：
   `recover_stuck_gpu_allocations` 的保护集在这一档生效，抢过来就是又一次"两张表互相打脸"。
3. operation 走成终态（FAILED）之后，`reconcile_all()` ——也就是 N-99 注册进周期表的那个入口——
   必须把卡放回池子且冲突清零；再跑一趟不产生第二次变化（幂等）。

三档合起来才是那句"will be reconciled"的完整意思：不是"有人会被动"，而是"这一格最终会被收掉，
而在它还被队列占着的时候不许有人去收"。

牙齿（六臂变异电池实测，2026-09-28，各臂恢复后 `cmp` 逐字节相同、末跑 4 passed）：
基线与 M0（在驱动者调用前插一行无关注释）都 0 红——所以下面的红归因于变异本身。
- M1 删掉 `reconcile_all` 末尾的 `recover_stuck_gpu_allocations(db)` ⇒ 只有 T3 红。
- M2 删掉 `recover_stuck_gpu_allocations` 的 active-operation 保护 ⇒ 只有 T2 红。
- M3 让 `_fail` 在 release 抛错那一档顺手把卡放回池子 ⇒ T1/T2/T3/T4 全红。
  全红是因为四档共用同一个前提断言（造不出冲突就没有可钉的东西）；这一臂说的是"冲突根本不该留下"
  这条路，T1 与 T4 是它点名两侧的档。
- M4 把 `tests/gpu_drift.py` 的 `card-points-at-holder` 那一侧改成读不到 ⇒ T1/T2 红、T3/T4 绿。
  证明 T1/T2 真在消费这把尺子的第二个方向，而不是自己手抄列值。
"""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    Gpu,
    GpuHost,
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
from app.services.providers.mock import MockProvider
from app.utils import utcnow
from tests.dbfiles import db_url
from tests.gpu_drift import gpu_ownership_disagreements

ENGINE = create_engine(db_url("release-self-heal"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield


def _fail_with_release_raising(tmp_path: Path, monkeypatch) -> str:
    """照 `test_provision_rollback.py:398` 的形状造那一格：准入放行、release 抛错。

    返回 workspace id。前置三件（PROVISIONING / 卡 ALLOCATED 且指着这格 / 格子三列已绑）
    在这里逐条断言——它们就是后面三档的坐标系，塌了的话"没变化"会被读成"守住了"。
    """
    with Factory() as db:
        db.add(
            User(id="u1", email="u1@x", username="u1", password_hash="x", role=Role.STUDENT.value)  # noqa: S106
        )
        db.add(
            GpuHost(id="host-1", name="h1", address="127.0.0.1", provider="mock")
        )
        db.add(
            Template(
                id="cartpole", slug="cartpole", name="cartpole", description="d", category="c",
                runtime="mock", launch_command="echo ok", enabled=True,
                recommended_vram_gb=16, estimated_hourly_cost_cny=1.0,
            )
        )
        db.add(
            Gpu(
                id="gpu-1", gpu_uuid="gpu-uuid-1", host_id="host-1", model="RTX-1",
                memory_total=24564, gpu_index=0, status=GpuStatus.AVAILABLE.value,
            )
        )
        db.commit()
        orchestrator = WorkspaceOrchestrator(Factory, MockProvider("http://127.0.0.1:8000"), Path(tmp_path))
        workspace = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = workspace.id
        gpu = db.get(Gpu, "gpu-1")
        gpu.status = GpuStatus.ALLOCATED.value
        gpu.workspace_id = wid
        ws = db.get(Workspace, wid)
        ws.status = WorkspaceStatus.PROVISIONING.value
        ws.gpu_id = gpu.id
        ws.gpu_index = gpu.gpu_index
        ws.gpu_name = gpu.model
        db.commit()

    def _boom(db, workspace_id):
        raise RuntimeError("simulated release failure")

    monkeypatch.setattr(orchestrator.scheduler, "release", _boom)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        # command_succeeded=True ⇒ mock 说 UNKNOWN ⇒ 不再是 ALIVE ⇒ 准入放行，走到 release 抛错那一档
        orchestrator._fail(db, ws, "boom", command_succeeded=True)
        db.commit()

    with Factory() as db:
        ws = db.get(Workspace, wid)
        card = db.get(Gpu, "gpu-1")
        assert ws.status == WorkspaceStatus.FAILED.value, ws.status
        assert (ws.gpu_id, ws.gpu_index, ws.gpu_name) == (None, None, None), "格子侧没清列，坐标系不对"
        assert card.workspace_id == wid and card.status == GpuStatus.ALLOCATED.value, (
            f"卡侧没留下冲突，这一轮量不到东西：{card.status}/{card.workspace_id}"
        )
    return wid


def _add_operation(op_id: str, wid: str, status: str) -> None:
    with Factory() as db:
        db.add(
            WorkspaceOperation(
                id=op_id, workspace_id=wid, operation_type=OperationType.PROVISION.value,
                status=status, attempts=2, created_at=utcnow() - timedelta(seconds=30),
            )
        )
        db.commit()


def _card() -> tuple[str, str | None, str]:
    with Factory() as db:
        gpu = db.get(Gpu, "gpu-1")
        return gpu.id, gpu.workspace_id, gpu.status


def test_the_release_failure_leaves_a_named_cross_table_conflict(tmp_path, monkeypatch) -> None:
    """T1 抛错之后：冲突必须被量具点名，且卡不许已经被读成空闲。"""
    wid = _fail_with_release_raising(tmp_path, monkeypatch)
    with Factory() as db:
        offenders = gpu_ownership_disagreements(db)
    assert [(o["kind"], o["workspace_id"]) for o in offenders] == [("card-points-at-holder", wid)], offenders
    _, _, status = _card()
    assert status == GpuStatus.ALLOCATED.value, "卡被偷偷标成空闲：分配器会以为可以发给别人"


def test_the_driver_does_not_yank_a_card_while_an_operation_is_active(tmp_path, monkeypatch) -> None:
    """T2 队列还在做这一格（RUNNING operation）⇒ 驱动者一躺也不许动这张卡。"""
    wid = _fail_with_release_raising(tmp_path, monkeypatch)
    _add_operation("op-active", wid, OperationStatus.RUNNING.value)

    orchestrator = WorkspaceOrchestrator(Factory, MockProvider("http://127.0.0.1:8000"), Path(tmp_path))
    orchestrator.reconcile_all()

    assert _card()[1] == wid and _card()[2] == GpuStatus.ALLOCATED.value, "活跃 operation 还挂着就把卡放走"
    with Factory() as db:
        assert [o["kind"] for o in gpu_ownership_disagreements(db)] == ["card-points-at-holder"]


def test_the_periodic_driver_picks_the_card_back_up_once_the_operation_is_terminal(
    tmp_path, monkeypatch
) -> None:
    """T3 operation 走成终态之后，`reconcile_all()`（N-99 的入口）必须把这张卡收回池子。"""
    wid = _fail_with_release_raising(tmp_path, monkeypatch)
    _add_operation("op-done", wid, OperationStatus.FAILED.value)

    orchestrator = WorkspaceOrchestrator(Factory, MockProvider("http://127.0.0.1:8000"), Path(tmp_path))
    first = orchestrator.reconcile_all()
    mid = _card()
    second = orchestrator.reconcile_all()

    assert mid[1] is None and mid[2] == GpuStatus.AVAILABLE.value, f"驱动者没把卡收回来：{mid}"
    assert first == second, f"第二趟改变了事实（幂等塌了）：{first} → {second}"
    with Factory() as db:
        assert gpu_ownership_disagreements(db) == [], "收完之后两表还在互相指认"
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.FAILED.value, "补做放卡时顺手动了格子的状态"


def test_the_heal_actually_needs_the_driver(tmp_path, monkeypatch) -> None:
    """T4 反证：不调驱动者的话，那一格永远停在冲突态（否则 T3 是在量一件自动发生的事）。"""
    wid = _fail_with_release_raising(tmp_path, monkeypatch)
    _add_operation("op-done2", wid, OperationStatus.FAILED.value)

    _, owner, status = _card()

    assert owner == wid and status == GpuStatus.ALLOCATED.value, (
        "没人驱动它也已经恢复了 ⇒ T3 那条'驱动者收回来'是假的，本轮前提不成立"
    )

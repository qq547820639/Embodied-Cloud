"""STOP 那一侧的 release 自抛：卡在谁手里、由谁补做（N-102）。

形状（读 `app/services/orchestrator.py:471-474`）：`_stop_cleanup` 的准入已经放行，
`_finalize_stop` 先结算再 `scheduler.release`，而 release 自己抛错了 ——
处理是 `return f"stop finalize failed: {exc}"`，注释写「留在 STOPPING，由 reconcile/重试补做」。
`stop()` 收到这个原因串后只把它记到 `error_message` 上，状态停在 STOPPING。

这条档此前**零覆盖**：全仓 `stop finalize failed` 只出现在生产侧那一行（本轮现算 grep：
`app/services/orchestrator.py:474` 一处，`tests/` 零处）。相邻的两支各量别的轴 ——
`tests/test_stop_release_admission.py` 量「不放行」那一档（provider 还说 ALIVE），
`tests/test_gpu_seconds_metric_delta.py:482` 量「同一段不得计两次」。

N-101 已为 provision 那一侧立过四档；这一侧的形状**不一样**，不能照搬：
`_fail` 会自己清格上三列，于是留下跨表冲突；STOP 这一侧什么都不清，两张表仍然互相认账，
残留的是「一张被还活着队列占着的卡」。所以这里的判据按 provider 能不能答话分成两极：

1. S1 抛错之后：状态必须是 STOPPING（不是 STOPPED、更不是 FAILED），原因留在
   `error_message` 上，卡还 ALLOCATED 且还指着这格，且两表一致（`gpu_drift` 量具零冲突）。
2. S2 provider 答不上来（mock 的 `reconcile` 恒为 UNKNOWN）时，驱动者**不许**动这一格：
   它既不把卡放掉、也不把状态写成 STOPPED ——「问不到」不等于「没人吃卡」（ADR 0008）。
3. S3 provider 亲口说 MISSING 时，同一个驱动者必须把这一格收完：STOPPED、卡回池、冲突为零。

S2 与 S3 是同一个夹具、同一个驱动者入口、只有一个变量——provider 自述的事实——两极都钉住，
"由 reconcile 补做" 这句话才不是恒真：它说的是"能问到 MISSING 就补做，问不到就不许假补做"。
账本侧的"补做不得重复入账"由 `tests/test_gpu_seconds_metric_delta.py` 那一支守着，本轮不复述。

牙齿（六臂变异电池实测，2026-09-28，各臂恢复后 `cmp` 逐字节相同、末跑 3 passed、`git diff app/` 为空）：
基线与 M0（在 `_reconcile_one` 头上插一行无关注释）都 0 红。
- A1 把 STOPPING 档的 UNKNOWN 也当成"没了" ⇒ 只有 S2 红（驱动者开始假补做）。
- A2 删掉 STOPPING/MISSING 档的 `_finalize_stop` ⇒ 只有 S3 红（承诺的补做没人做）。
- A5 在 release 抛错那一档顺手清掉格上三列 ⇒ 只有 S1 红（两表当场开始互相打脸）。
- A3 让 `stop()` 在没达成目标时直接写 FAILED ⇒ 三支全红：它们共用同一个前提断言，
  这一臂说的是"把可重试的失败写成终态"（ADR 0002 修订的那条），不是某一档单独的牙。
"""

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
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.base import RuntimeState
from app.services.providers.mock import MockProvider
from app.services.streaming import StreamingSessionService
from tests.dbfiles import db_url
from tests.gpu_drift import gpu_ownership_disagreements

ENGINE = create_engine(db_url("stop-self-heal"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)
TMP = Path("/tmp/test-stop-self-heal")  # noqa: S108 测试隔离目录


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield


class _GoneProvider(MockProvider):
    """唯一差别：provider 亲口说 runtime 不在了。S2/S3 之间就动这一个变量。"""

    def reconcile(self, workspace: Workspace) -> RuntimeState:
        return RuntimeState.MISSING


def _release_raises(db, workspace_id):
    raise RuntimeError("simulated release failure")


def _stop_with_release_raising(provider: MockProvider) -> tuple[WorkspaceOrchestrator, str]:
    """真走 provision 到 RUNNING，再把 release 换成抛错，然后叫一次 `stop()`。

    抛错只覆盖这一次调用：`stop()` 返回后立刻还原 `release`，S3 的补做才会走真的释放。
    前置四件在这里逐条断言——它们是后面三档的坐标系：卡由 scheduler 原子分配、
    格上三列已绑、运行段真实存在（结算才有东西可算）、`stop()` 走完抛错那一档。
    """
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, TMP, streaming=StreamingSessionService(Factory)
    )
    with Factory() as db:
        db.add(
            User(id="u1", email="u1@x", username="u1", password_hash="x", role=Role.STUDENT.value)  # noqa: S106
        )
        db.add(GpuHost(id="host-1", name="h1", address="127.0.0.1", provider="mock"))
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
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = ws.id

    orchestrator._start(wid)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value, ws.status
        assert ws.gpu_id == "gpu-1" and (ws.gpu_index, ws.gpu_name) == (0, "RTX-1"), "分配没落齐三列"
        card = db.get(Gpu, "gpu-1")
        assert card.status == GpuStatus.ALLOCATED.value and card.workspace_id == wid
        # 运行段真实存在：30 秒前的 started_at，结算与配额才有可核对的前提
        ws.started_at = datetime.now(UTC) - timedelta(seconds=30)
        db.commit()

    scheduler = orchestrator.scheduler
    real_release = scheduler.release
    scheduler.release = _release_raises
    try:
        with Factory() as db:
            orchestrator.stop(db, db.get(Workspace, wid))
    finally:
        scheduler.release = real_release
    return orchestrator, wid


def _state(wid: str) -> dict:
    with Factory() as db:
        ws = db.get(Workspace, wid)
        card = db.get(Gpu, "gpu-1")
        allocs = list(
            db.scalars(select(GpuAllocation).where(GpuAllocation.workspace_id == wid))
        )
        return {
            "status": ws.status,
            "error_message": ws.error_message,
            "columns": (ws.gpu_id, ws.gpu_index, ws.gpu_name),
            "card": (card.status, card.workspace_id),
            "open_allocations": sum(1 for a in allocs if a.released_at is None),
            "disagreements": [o["kind"] for o in gpu_ownership_disagreements(db)],
        }


def test_release_raising_during_stop_leaves_the_card_held_and_the_pair_consistent() -> None:
    """S1 抛错之后：停在 STOPPING、原因可读、卡还在原主手里，且两张表没有互相打脸。"""
    _, wid = _stop_with_release_raising(MockProvider("http://127.0.0.1:8000"))
    snap = _state(wid)

    assert snap["status"] == WorkspaceStatus.STOPPING.value, (
        f"没达成目标却被写成了别的状态：{snap['status']}"
    )
    assert snap["error_message"] and snap["error_message"].startswith("stop finalize failed"), (
        snap["error_message"]
    )
    assert snap["card"] == (GpuStatus.ALLOCATED.value, wid), "卡被放走或改主：分配器会二次分配"
    assert snap["columns"][0] == "gpu-1", "格子侧被偷偷清列：那就不再是 N-101 那形状，本档前提塌了"
    assert snap["open_allocations"] == 1, f"分配行没被留下 ⇒ 卡并不是真的被占着：{snap}"
    assert snap["disagreements"] == [], f"两表此时应当仍互相认账：{snap['disagreements']}"


def test_the_driver_leaves_a_stopping_cell_alone_when_the_provider_cannot_answer() -> None:
    """S2 provider 答不上来（UNKNOWN）⇒ 驱动者既不放卡也不写 STOPPED：问不到≠没人吃卡。"""
    orchestrator, wid = _stop_with_release_raising(MockProvider("http://127.0.0.1:8000"))
    before = _state(wid)

    orchestrator.reconcile_all()
    after = _state(wid)

    assert after == before, f"UNKNOWN 档被动了：{before} → {after}"
    assert after["status"] == WorkspaceStatus.STOPPING.value
    assert after["card"] == (GpuStatus.ALLOCATED.value, wid)


def test_the_driver_completes_the_stop_once_the_provider_reports_the_runtime_gone() -> None:
    """S3 provider 亲口说 MISSING ⇒ 同一个驱动者把这一格收完：STOPPED + 卡回池 + 冲突为零。"""
    orchestrator, wid = _stop_with_release_raising(_GoneProvider("http://127.0.0.1:8000"))
    before = _state(wid)
    assert before["status"] == WorkspaceStatus.STOPPING.value, "S3 的起点应与 S2 完全相同"

    stats = orchestrator.reconcile_all()
    after = _state(wid)

    assert after["status"] == WorkspaceStatus.STOPPED.value, f"驱动者没把 STOP 补做完：{after}"
    assert after["card"] == (GpuStatus.AVAILABLE.value, None), after["card"]
    assert after["columns"] == (None, None, None), "卡回池而格上还绑着 ⇒ N-86 那条成对性又漏了一处"
    assert after["open_allocations"] == 0, after
    assert after["disagreements"] == []
    assert stats["stopped"] >= 1, stats

    # 幂等：再跑一趟不再产生第二次变化（STOPPED 已在 reconcile 的跳过集里，补做不是每趟重来）
    orchestrator.reconcile_all()
    assert _state(wid) == after

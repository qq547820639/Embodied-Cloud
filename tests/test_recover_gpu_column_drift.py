"""孤儿回收必须让两张表一起改口（N-82）：放掉卡的那一格，`gpu_id/gpu_index/gpu_name` 一并清。

缺陷形状（改前的 `recover_stuck_gpu_allocations`，读自 `git show 79e4402:app/services/scheduler.py`）：
它按"非占用"判定孤儿之后，只做两件事 —— (a) 删 `gpu_allocations` 行（改前 :301-304）、
(b) 把 `gpus.status/workspace_id` 归零（改前 :306-317 两条分支，`occupied_ids` 为空那条
清空所有绑定），从不回写 `workspaces` 那一行。于是强制放卡之后：卡说"我没主了、可再分配"，
格子说"我还持有卡 X"。

为什么这不是账面难看（生产侧读者，逐条带出处）：
- `WorkspaceOrchestrator.reconcile_all` 的 K8s 节点不一致分支按 `w.gpu_id` 反查那张卡的
  host，再拿 pod 实际节点去比（本树 `app/services/orchestrator.py:533-554`，承重两读是
  :536 的 `w.gpu_id is not None` 与 :538 的 `db.get(Gpu, w.gpu_id)`）。卡若已转授他人，
  这一格比的就是**别人**的节点。
- HTTP 面与前端把这三列当"这一格在哪张卡上"对外说（`app/schemas.py:87-104` 的
  `WorkspaceOut`、`app/static/app.js:419` 的 `w.gpu_name || "等待 GPU"`）。
- 预热池容量把 `gpu_id IS NULL` 当"这一格还会去占卡"的判据（`app/services/warmpool.py:565`）。

本轮设计（只做这一件，不顺手重构别的）：回收器是漂移的收口者 —— 它判定为孤儿并删掉分配行
的每一格，都在**同一事务**里把那格的三列一起清掉；受保护/占用（状态 ∈ {provisioning,
running, stopping}，或有 active operation）的格一列都不动。`Workspace` 模型没有 `updated_at`
列（`app/models.py:289-296` 只有 created_at/started_at/stopped_at/deleted_at），所以"顺手刷
时间戳"在这个模型上无从做起 —— 这条前提按交来的措辞核实为"没有这一列"，不是"忘了做"。

前提更正（已写进收尾报告，本轮不动手）：交来的设计说"`_fail`/`_finalize_stop`/warm pool
在各自准入路径上已经清自己那三列"。核实结果：只有 `_fail` 成立（`app/services/orchestrator.py:315-317`）；
`_finalize_stop`（:441-456）与 warm pool 认领撤销的放卡档（`app/services/warmpool.py:400-405`）
都只叫 `scheduler.release`、不清列。也就是说"卡放了、格子还指着它"另有两处产地，而回收器
看不见它们产出的格（那些格既没有分配行、卡也不再指向任何格，本函数无从记名）。因此本文件的
跨表不变量判据作用域是"**回收器自己放掉的那些格**"，另配注入反证证明这把尺子真能点名。
"""

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Base,
    Gpu,
    GpuAllocation,
    GpuStatus,
    OperationStatus,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from app.services.scheduler import GpuInfo, GpuScheduler, recover_stuck_gpu_allocations
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("recover-drift"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)
Scheduler = GpuScheduler(Factory)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_SOURCE = (REPO_ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")

GPU_COLUMNS = ("gpu_id", "gpu_index", "gpu_name")

# 占用判据的规格（与实现无关的一份"应然"）：这三态在生命周期中间，回收器不得动它们。
PROTECTED_STATUSES = {
    WorkspaceStatus.PROVISIONING.value,
    WorkspaceStatus.RUNNING.value,
    WorkspaceStatus.STOPPING.value,
}
# active operation 那一半占用判据的规格。
ACTIVE_OPERATIONS = {
    OperationStatus.PENDING.value,
    OperationStatus.RUNNING.value,
    OperationStatus.RETRYING.value,
}

# 状态矩阵的分母**由模型枚举现取**，不是手抄的清单：新增一个 WorkspaceStatus 成员，
# 参数化用例自动多一格（不会静默跳过），下面的完整性判据也自动核对它。
STATUS_CASES = [s.value for s in WorkspaceStatus]
OPERATION_CASES = [s.value for s in OperationStatus]


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


# ---------------------------------------------------------------------------
# 夹具与权威表读数
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """一次"真分配"留下的事实：哪张卡、第几号、什么型号（清列判据的期望值由它们算）。"""

    workspace_id: str
    gpu_id: str
    gpu_index: int | None
    gpu_name: str | None


def _seed(db: Session, gpus: int = 3) -> None:
    Scheduler.sync_host(
        db,
        host_id="h1",
        name="host-1",
        address="127.0.0.1",
        provider="mock",
        gpus=[
            GpuInfo(gpu_uuid=f"gpu-{i + 1}", model=f"RTX-{i}", memory_total=16384 * (i + 1), index=i)
            for i in range(gpus)
        ],
    )


def _claim(db: Session, wid: str, status: str) -> Claim:
    """造一个"持有卡"的格：workspace 行 + 真 `scheduler.allocate` + provision 落的三列。

    三列是 provision 亲手写的（本树 `app/services/orchestrator.py:206-208` 同形），
    夹具不自己编值：这样"清列"才有可核对的期望。
    """
    db.add(
        Workspace(id=wid, name="ws", template_id="cartpole", provider="mock", status=status)
    )
    db.commit()
    gpu = Scheduler.allocate(db, wid, gpu_requirement_gb=8)
    row = db.get(Workspace, wid)
    assert row is not None
    row.gpu_id = gpu.id
    row.gpu_index = gpu.gpu_index
    row.gpu_name = gpu.model
    db.commit()
    return Claim(workspace_id=wid, gpu_id=gpu.id, gpu_index=gpu.gpu_index, gpu_name=gpu.model)


def _add_operation(db: Session, wid: str, op_status: str, op_id: str = "op-1") -> None:
    db.add(
        WorkspaceOperation(
            id=op_id,
            workspace_id=wid,
            operation_type="destroy",
            status=op_status,
        )
    )
    db.commit()


def _active_operation_count(db: Session, wid: str) -> int:
    return int(
        db.scalar(
            select(func.count(WorkspaceOperation.id)).where(
                WorkspaceOperation.workspace_id == wid,
                WorkspaceOperation.status.in_(sorted(ACTIVE_OPERATIONS)),
            )
        )
        or 0
    )


def _occupied_ids(db: Session) -> set[str]:
    """按实现的同一判据现算占用集 —— 用来证"这条分支真是空/非空那一档"。"""
    ids = set(
        db.scalars(select(Workspace.id).where(Workspace.status.in_(sorted(PROTECTED_STATUSES))))
    )
    ids |= set(
        db.scalars(
            select(WorkspaceOperation.workspace_id).where(
                WorkspaceOperation.status.in_(sorted(ACTIVE_OPERATIONS))
            )
        )
    )
    return ids


def _read(db: Session, claim: Claim) -> dict:
    """从权威表（不是替身）读这一格与这张卡的全部相关事实。"""
    gpu = db.get(Gpu, claim.gpu_id)
    assert gpu is not None, f"卡行不见了，判据无从判起：{claim.gpu_id}"
    ws = db.get(Workspace, claim.workspace_id)
    assert ws is not None, f"格行不见了，判据无从判起：{claim.workspace_id}"
    alloc = db.scalar(select(GpuAllocation).where(GpuAllocation.workspace_id == claim.workspace_id))
    return {
        "alloc_rows": 1 if alloc is not None else 0,
        "alloc_released": (alloc is not None and alloc.released_at is not None),
        "gpu_status": gpu.status,
        "gpu_holder": gpu.workspace_id,
        "ws_gpu_id": ws.gpu_id,
        "ws_gpu_index": ws.gpu_index,
        "ws_gpu_name": ws.gpu_name,
    }


def _held(claim: Claim) -> dict:
    return {
        "alloc_rows": 1,
        "alloc_released": False,
        "gpu_status": GpuStatus.ALLOCATED.value,
        "gpu_holder": claim.workspace_id,
        "ws_gpu_id": claim.gpu_id,
        "ws_gpu_index": claim.gpu_index,
        "ws_gpu_name": claim.gpu_name,
    }


def _released_and_cleared() -> dict:
    return {
        "alloc_rows": 0,
        "alloc_released": False,  # 分配行整体不存在，"released_at 有没有值"这一问没有对象
        "gpu_status": GpuStatus.AVAILABLE.value,
        "gpu_holder": None,
        "ws_gpu_id": None,
        "ws_gpu_index": None,
        "ws_gpu_name": None,
    }


# ---------------------------------------------------------------------------
# 判据：枚举完整性（分母由模型现取，缺一格就点名）
# ---------------------------------------------------------------------------


def status_case_gaps(expected: Sequence[str], covered: Iterable[str]) -> list[str]:
    """分母里没被用例覆盖到的成员（空 = 状态矩阵完整）。

    存在的理由：参数化清单若被后人换成手抄的列表，新增状态会静默跳过释放/保留判据。
    分母一律由 `[s.value for s in WorkspaceStatus]` 现取，判据本身不写死任何成员数。
    """
    covered_set = set(covered)
    return [value for value in expected if value not in covered_set]


def test_the_status_matrix_covers_every_workspace_status_member() -> None:
    """状态判据的分母就是模型里的枚举本身，一个不多一个不少。"""
    assert status_case_gaps(STATUS_CASES, STATUS_CASES) == []
    assert set(STATUS_CASES) == {s.value for s in WorkspaceStatus}
    # 规格侧的三态必须真的在枚举里：改名或删项会让"保留"那一半变成空转，这里先塌掉。
    assert set(STATUS_CASES).issuperset(PROTECTED_STATUSES), sorted(
        PROTECTED_STATUSES - set(STATUS_CASES)
    )
    assert set(OPERATION_CASES).issuperset(ACTIVE_OPERATIONS), sorted(
        ACTIVE_OPERATIONS - set(OPERATION_CASES)
    )


def test_the_gap_checker_can_see_a_missing_case() -> None:
    """上一条的反证：从真分母里抽掉一格，判据必须只点名那一格。

    没有这一支，"覆盖完整"这条断言在参数化清单被手抄列表替换后会一直绿着。
    """
    missing = set(STATUS_CASES) - {WorkspaceStatus.CREATED.value}
    assert status_case_gaps(STATUS_CASES, missing) == [WorkspaceStatus.CREATED.value]
    assert status_case_gaps(STATUS_CASES, set(STATUS_CASES)) == []


# ---------------------------------------------------------------------------
# 判据：跨表一致性（两个方向的冲突都能点名）
# ---------------------------------------------------------------------------


def gpu_ownership_disagreements(db: Session) -> list[dict[str, str]]:
    """`gpus` 与 `workspaces` 互相指认的冲突清单；空表 = 两张权威表一致。

    - `card-points-at-holder`：`gpus.workspace_id = w.id` 而 `w.gpu_id != gpus.id`
      —— 卡被一个不声称持有它的格占着：谁也抢不走，持有它的人也不会来放（卡被钉死）。
    - `holder-points-at-card`：`w.gpu_id = g.id` 而 `g.workspace_id != w.id`
      —— 正是本轮缺陷的形状：强制放卡之后格子还声称持有那张卡。
    """
    out: list[dict[str, str]] = []
    for gpu in db.scalars(select(Gpu).order_by(Gpu.id)):
        if gpu.workspace_id is None:
            continue
        holder = db.get(Workspace, gpu.workspace_id)
        if holder is None or holder.gpu_id != gpu.id:
            out.append(
                {
                    "kind": "card-points-at-holder",
                    "gpu_id": gpu.id,
                    "workspace_id": gpu.workspace_id,
                }
            )
    for ws in db.scalars(select(Workspace).order_by(Workspace.id)):
        if ws.gpu_id is None:
            continue
        card = db.get(Gpu, ws.gpu_id)
        if card is None or card.workspace_id != ws.id:
            out.append(
                {"kind": "holder-points-at-card", "gpu_id": ws.gpu_id, "workspace_id": ws.id}
            )
    return out


# ---------------------------------------------------------------------------
# 必开火档：每一种非占用状态 ⇒ 分配行没了 + 卡回池 + 三列清空
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", STATUS_CASES)
def test_reclaim_either_protects_the_status_or_clears_the_columns_too(status: str) -> None:
    """逐一过 `WorkspaceStatus` 全部成员：占用态三样全留，非占用态三样全清。

    读的是权威表（`gpus` / `workspaces` / `gpu_allocations`），不是替身；期望值由
    provision 真写下的 `Claim` 算出，所以"清了但清错值"也过不去。
    前提由种子差分证明：调回收器之前这一格确实是"卡与格互相指认"的持有态。
    """
    with Factory() as db:
        _seed(db)
        claim = _claim(db, "w1", status)
        assert _read(db, claim) == _held(claim), "前提塌了：夹具没造出持卡的格"

    with Factory() as db:
        # 保护只能来自状态这一半 —— 有 active operation 兜底的话这条就不是状态判据
        assert _active_operation_count(db, "w1") == 0
        recover_stuck_gpu_allocations(db)

    with Factory() as db:
        expected = _held(claim) if status in PROTECTED_STATUSES else _released_and_cleared()
        actual = _read(db, claim)
        assert actual == expected, (
            f"status={status}：期望 {'保留' if status in PROTECTED_STATUSES else '放卡并清列'}，"
            f"实际 {actual}"
        )
        # 同一判据的跨表面：无论哪一极，两张表都不许互相指认不上
        assert gpu_ownership_disagreements(db) == [], f"status={status} 之后两表漂移"


def test_a_non_protected_status_with_an_active_operation_keeps_everything() -> None:
    """逐一过 `OperationStatus` 全部成员：active 那三态即使配上非占用状态也不许放卡。

    工作区状态固定为非占用集里的一格（FAILED），变量只有 operation 状态 —— 于是"保留"
    只能来自 active operation 那一半判据，不能是状态判据顺手挡下的。
    """
    assert WorkspaceStatus.FAILED.value not in PROTECTED_STATUSES
    for op_status in OPERATION_CASES:
        # 逐档复位：autouse 夹具每个用例只清一次库，循环里第二档会撞上同一张格/同一张卡
        Base.metadata.drop_all(ENGINE)
        Base.metadata.create_all(ENGINE)
        with Factory() as db:
            _seed(db)
            claim = _claim(db, "w-op", WorkspaceStatus.FAILED.value)
            _add_operation(db, "w-op", op_status)
            assert _read(db, claim) == _held(claim), (op_status, "前提塌了：夹具没造出持卡的格")
        with Factory() as db:
            recover_stuck_gpu_allocations(db)
        with Factory() as db:
            expected = _held(claim) if op_status in ACTIVE_OPERATIONS else _released_and_cleared()
            assert _read(db, claim) == expected, op_status
            assert gpu_ownership_disagreements(db) == [], op_status


def test_only_orphan_columns_are_cleared_in_a_mixed_database() -> None:
    """混合库：受保护的格（状态档 + operation 档）列一个不许动，孤儿的格三样全清。

    这条钉的是"清列的作用域"。若把清列写成全局谓词（例如 `WHERE gpu_id IS NOT NULL`），
    RUNNING 那格的列会被抹掉，而它的卡还指着它 —— 那就是把一种漂移换成另一种更坏的。
    """
    with Factory() as db:
        _seed(db)
        keep_status = _claim(db, "w-running", WorkspaceStatus.RUNNING.value)
        keep_op = _claim(db, "w-retrying", WorkspaceStatus.FAILED.value)
        _add_operation(db, "w-retrying", OperationStatus.RETRYING.value, op_id="op-retry")
        orphan = _claim(db, "w-failed", WorkspaceStatus.FAILED.value)
        assert _occupied_ids(db) == {"w-running", "w-retrying"}

    with Factory() as db:
        recover_stuck_gpu_allocations(db)

    with Factory() as db:
        assert _read(db, keep_status) == _held(keep_status), "RUNNING 的列被回收器动了"
        assert _read(db, keep_op) == _held(keep_op), "有 active operation 的列被回收器动了"
        assert _read(db, orphan) == _released_and_cleared(), "孤儿格没被清列"
        assert gpu_ownership_disagreements(db) == []


def test_the_empty_occupied_branch_frees_every_card_and_clears_every_claim() -> None:
    """`occupied_ids` 为空那一支（改前 scheduler.py:312-317"清空所有绑定"）的专门档。

    前提由判据现算：占用集真的为空（没有任何受保护状态、没有任何 active operation），
    所以走的必然是 else 那一支。结论：每张卡都回池、每格的三列都清空，不留任何残claim。
    """
    with Factory() as db:
        _seed(db)
        claims = [
            _claim(db, "w-failed", WorkspaceStatus.FAILED.value),
            _claim(db, "w-queued", WorkspaceStatus.QUEUED.value),
            _claim(db, "w-stopped", WorkspaceStatus.STOPPED.value),
        ]
        for claim in claims:
            assert _read(db, claim) == _held(claim)

    with Factory() as db:
        assert _occupied_ids(db) == set(), "前提塌了：占用集非空，走的就不是 else 那一支"
        recover_stuck_gpu_allocations(db)

    with Factory() as db:
        cards = db.scalars(select(Gpu)).all()
        assert len(cards) == 3
        assert all(g.status == GpuStatus.AVAILABLE.value for g in cards), [g.status for g in cards]
        assert all(g.workspace_id is None for g in cards), [g.workspace_id for g in cards]
        assert db.scalar(select(func.count(GpuAllocation.id))) == 0
        rows = db.scalars(select(Workspace)).all()
        assert len(rows) == 3
        assert all(
            (r.gpu_id, r.gpu_index, r.gpu_name) == (None, None, None) for r in rows
        ), [(r.id, r.gpu_id, r.gpu_index, r.gpu_name) for r in rows]
        assert gpu_ownership_disagreements(db) == []


def test_an_allocation_whose_workspace_row_is_gone_releases_without_inventing_a_claim() -> None:
    """没有 workspace 行的孤儿分配（崩溃残留）：分配行删掉，且不因清列语句凭空造行。

    这一档卡本身仍不是回收器能归还的（`gpus.workspace_id` 是 NULL，两条 UPDATE 都碰不到
    它），本轮不扩权去改它；这里只钉回收器新加的那半边的边界：UPDATE 命中 0 行、不报错、
    不留下任何"格指着卡"的新冲突。
    """
    with Factory() as db:
        _seed(db)
        gpu = db.scalar(select(Gpu).where(Gpu.gpu_uuid == "gpu-1"))
        assert gpu is not None
        db.add(
            GpuAllocation(id="alloc-ghost", gpu_id=gpu.id, workspace_id="ghost-ws", host_id=gpu.host_id)
        )
        db.commit()

    with Factory() as db:
        recover_stuck_gpu_allocations(db)

    with Factory() as db:
        assert db.scalar(select(GpuAllocation).where(GpuAllocation.id == "alloc-ghost")) is None
        assert db.get(Workspace, "ghost-ws") is None, "清列语句把不存在的格『造』出来了"
        assert gpu_ownership_disagreements(db) == []


def test_reclaim_is_idempotent_and_still_agrees_on_the_second_pass() -> None:
    """连跑两次：第二遍既不放受保护的卡，也不把已经清过的格改出别的形状。"""
    with Factory() as db:
        _seed(db)
        keep = _claim(db, "w-keep", WorkspaceStatus.RUNNING.value)
        orphan = _claim(db, "w-gone", WorkspaceStatus.DELETED.value)

    for _ in range(2):
        with Factory() as db:
            recover_stuck_gpu_allocations(db)

    with Factory() as db:
        assert _read(db, keep) == _held(keep)
        assert _read(db, orphan) == _released_and_cleared()
        assert gpu_ownership_disagreements(db) == []


# ---------------------------------------------------------------------------
# 注入反证：跨表判据真能点名（不是恒真）
# ---------------------------------------------------------------------------


def test_the_disagreement_checker_names_both_injected_splits() -> None:
    """两个方向各注入一次漂移，判据必须各自点名，且撤掉注入后回到空表。

    没有这一支，`gpu_ownership_disagreements(db) == []` 可能只是因为这把尺子量不到东西。
    """
    with Factory() as db:
        _seed(db)
        holder = _claim(db, "w-holder", WorkspaceStatus.STOPPED.value)
        card = db.get(Gpu, holder.gpu_id)
        assert card is not None

        # 方向 B（本轮缺陷的形状）：卡已放（workspace_id=None），格还声称持有它
        card.workspace_id = None
        card.status = GpuStatus.AVAILABLE.value
        db.commit()
        injected_b = gpu_ownership_disagreements(db)
        assert [d["kind"] for d in injected_b] == ["holder-points-at-card"], injected_b
        assert injected_b[0]["workspace_id"] == "w-holder", injected_b
        assert injected_b[0]["gpu_id"] == holder.gpu_id, injected_b

        # 撤掉注入：同一把尺子在一致的两张表上不开火
        card.workspace_id = "w-holder"
        card.status = GpuStatus.ALLOCATED.value
        db.commit()
        assert gpu_ownership_disagreements(db) == []

        # 方向 A：卡指着格，而格的 gpu_id 指向另一张卡（这里直接置空）
        row = db.get(Workspace, "w-holder")
        assert row is not None
        row.gpu_id = None
        db.commit()
        injected_a = gpu_ownership_disagreements(db)
        assert [d["kind"] for d in injected_a] == ["card-points-at-holder"], injected_a
        assert injected_a[0]["gpu_id"] == holder.gpu_id, injected_a

        # 另一极：另一张没人占的卡不该被点名（判据不看卡的状态，只看指认关系）
        spare = db.scalar(select(Gpu).where(Gpu.gpu_uuid == "gpu-3"))
        assert spare is not None and spare.workspace_id is None
        assert all(d["gpu_id"] != spare.id for d in injected_a), injected_a


def test_the_column_clear_lands_in_a_session_that_already_holds_the_rows() -> None:
    """唯一的生产调用方在**同一个会话**里先把全部 Workspace 载入身份图再调回收器
    （`reconcile_all` 末尾那一句，本树 `app/services/orchestrator.py:622`）。

    本轮新加的是 ORM 实体的批量 UPDATE，不是逐对象赋值：它必须让身份图里那些活对象
    一起改口，否则同一轮 reconcile 后面读到的还是旧的 `gpu_id`（对外谎报持卡），
    而下一轮又看得到新值 —— 一个只在"同一会话内"漂移的缺陷。
    """
    with Factory() as db:
        _seed(db)
        keep = _claim(db, "w-running", WorkspaceStatus.RUNNING.value)
        orphan = _claim(db, "w-orphan", WorkspaceStatus.STOPPED.value)

    with Factory() as db:
        loaded = {w.id: w for w in db.scalars(select(Workspace).order_by(Workspace.id))}
        assert set(loaded) == {"w-orphan", "w-running"}, sorted(loaded)
        assert loaded["w-orphan"].gpu_id == orphan.gpu_id, "前提塌了：会话里拿到的不是持卡态"
        recover_stuck_gpu_allocations(db)
        # 就地对象（未经 refresh）也得改口
        assert (loaded["w-orphan"].gpu_id, loaded["w-orphan"].gpu_index, loaded["w-orphan"].gpu_name) == (
            None,
            None,
            None,
        ), "批量清列没同步进身份图：同一轮里读到的还是旧值"
        assert (loaded["w-running"].gpu_id, loaded["w-running"].gpu_name) == (
            keep.gpu_id,
            keep.gpu_name,
        ), "受保护那格的列被会话内改口了"

    with Factory() as db:
        assert _read(db, orphan) == _released_and_cleared()
        assert _read(db, keep) == _held(keep)
        assert gpu_ownership_disagreements(db) == []


# ---------------------------------------------------------------------------
# 结构判据：本函数没有"放卡不清列"的那条路
# ---------------------------------------------------------------------------


def release_column_pairing(source: str) -> dict[str, int]:
    """AST 读 `recover_stuck_gpu_allocations` 的"放卡 ↔ 清列"配对形状。

    返回各格含义：
    - `release_sites` —— 放掉卡归属的位点数：`db.delete(分配行)` 与
      `update(Gpu)...values(workspace_id=None)` 各算一处（本轮实现 = 1 + 2）。
    - `clear_sites` —— `update(Workspace)` 把 GPU 列写成 None 的语句数。
    - `columns_cleared` —— 被清成 None 的 GPU 列数（gpu_id / gpu_index / gpu_name，应为 3）。
    - `record_sites` —— 往"清列语句的 WHERE 消费的那份 id 名单"里记名的位点数；保护集
      （出现在 `notin_` 里的那个名字）自己的写入不计入，见函数体内的说明。
    - `unpaired_release_paths` —— `release_sites - record_sites`：有放卡位点没记名 ⇒ 那条路
      放卡却不会清列（本轮缺陷的另一种藏法：只补一个分支）。
    - `clear_scope_is_recorded_list` —— 清列的 WHERE 是不是"按那份名单点名"（1）还是全局
      谓词/别的集合（0）。
    - `clear_scope_leaks_protection_set` —— 名单里混进了"保护集"（`notin_` 的那个集合）吗。
    - `clear_before_commit` —— 清列在唯一一次 `db.commit()` 之前（同一事务）吗。

    为什么按 AST 而不是按文本：注释里出现 `gpu_id=None` 不算清列；把清列挪到 commit 之后
    文本上还在、事务上已是两回事；`update(Gpu)` 少一个分支只有按语句数才看得见。
    """
    tree = ast.parse(source)
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "recover_stuck_gpu_allocations"),
        None,
    )
    if fn is None:
        # 靶没了 ≠ 判据通过：与"存在但没接线"分开报
        raise AssertionError("`recover_stuck_gpu_allocations` 不存在：尺子的靶没了，不是『没有漂移』")

    def is_none(node: ast.expr | None) -> bool:
        return isinstance(node, ast.Constant) and node.value is None

    def attr_name(func_node: ast.AST) -> str | None:
        if isinstance(func_node, ast.Attribute):
            return func_node.attr
        return None

    def values_kwargs(expr: ast.expr) -> dict[str, ast.expr]:
        out: dict[str, ast.expr] = {}
        for node in ast.walk(expr):
            if isinstance(node, ast.Call) and attr_name(node.func) == "values":
                for kw in node.keywords:
                    if kw.arg is not None:
                        out.setdefault(kw.arg, kw.value)
        return out

    def update_of(expr: ast.expr, model: str) -> bool:
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "update"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == model
            for node in ast.walk(expr)
        )

    # 只看"执行语句"级别的调用链（Expr 语句），复合语句（if/for）会重复包住同一条链
    exprs = [n.value for n in ast.walk(fn) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]

    release_sites = 0
    clear_exprs: list[ast.expr] = []
    for expr in exprs:
        if attr_name(expr.func) == "delete":  # type: ignore[union-attr]
            release_sites += 1
        if update_of(expr, "Gpu") and is_none(values_kwargs(expr).get("workspace_id")):
            release_sites += 1
        if update_of(expr, "Workspace") and any(
            is_none(values_kwargs(expr).get(col)) for col in GPU_COLUMNS
        ):
            clear_exprs.append(expr)

    columns_cleared: set[str] = set()
    cleared_names: set[str] = set()
    for expr in clear_exprs:
        kwargs = values_kwargs(expr)
        columns_cleared |= {col for col in GPU_COLUMNS if is_none(kwargs.get(col))}
        for node in ast.walk(expr):
            if isinstance(node, ast.Call) and attr_name(node.func) == "in_" and node.args:
                cleared_names |= {n.id for n in ast.walk(node.args[0]) if isinstance(n, ast.Name)}

    protected_names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and attr_name(node.func) == "notin_" and node.args:
            protected_names |= {n.id for n in ast.walk(node.args[0]) if isinstance(n, ast.Name)}

    # 记名位点只数"被放卡的那组 id"：保护集那个名字自己的写入（`occupied_ids |= …`）不算，
    # 否则"名单里混进保护集"那种变异会把差值打成负数，两格纠缠在一起、说不清谁在点名。
    recorded_names = cleared_names - protected_names
    record_sites = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and attr_name(node.func) in {"add", "update"}:
            target = node.func.value  # type: ignore[union-attr]
            if isinstance(target, ast.Name) and target.id in recorded_names:
                record_sites += 1
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            if node.target.id in recorded_names:
                record_sites += 1

    commit_lines = sorted(
        {n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call) and attr_name(n.func) == "commit"}
    )
    clear_line = min((expr.lineno for expr in clear_exprs), default=-1)
    clear_before_commit = 1 if clear_exprs and commit_lines and clear_line < commit_lines[0] else 0

    return {
        "release_sites": release_sites,
        "clear_sites": len(clear_exprs),
        "columns_cleared": len(columns_cleared),
        "record_sites": record_sites,
        "unpaired_release_paths": release_sites - record_sites,
        "clear_scope_is_recorded_list": 1 if clear_exprs and cleared_names else 0,
        "clear_scope_leaks_protection_set": 1 if cleared_names & protected_names else 0,
        "clear_before_commit": clear_before_commit,
    }


# 改前的真实形状（逐字转抄自 `git show 79e4402:app/services/scheduler.py` 的
# recover_stuck_gpu_allocations）：放卡位点已经在那里，清列一位都没有。
# 它的用处不是"复刻一份旧代码"，而是证明 `release_sites` 数的确实是放卡那几处 ——
# 而不是本轮新加的行；否则配对判据可能只是在数自己的实现。
PRE_FIX_RECOVER_SOURCE = '''
def recover_stuck_gpu_allocations(db):
    from ..models import OperationStatus, Workspace, WorkspaceOperation

    occupied_ids = set(
        db.scalars(
            select(Workspace.id).where(
                Workspace.status.in_(
                    [
                        WorkspaceStatus.PROVISIONING.value,
                        WorkspaceStatus.RUNNING.value,
                        WorkspaceStatus.STOPPING.value,
                    ]
                )
            )
        )
    )
    occupied_ids |= set(
        db.scalars(
            select(WorkspaceOperation.workspace_id).where(
                WorkspaceOperation.status.in_(
                    [
                        OperationStatus.PENDING.value,
                        OperationStatus.RUNNING.value,
                        OperationStatus.RETRYING.value,
                    ]
                )
            )
        )
    )
    allocs = db.scalars(
        select(GpuAllocation).where(GpuAllocation.released_at.is_(None))
    ).all()
    for alloc in allocs:
        if alloc.workspace_id not in occupied_ids:
            alloc.released_at = utcnow()
            db.delete(alloc)
    if occupied_ids:
        db.execute(
            update(Gpu)
            .where(Gpu.workspace_id.notin_(occupied_ids))
            .values(status=GpuStatus.AVAILABLE.value, workspace_id=None, updated_at=utcnow())
        )
    else:
        db.execute(
            update(Gpu)
            .where(Gpu.workspace_id.is_not(None))
            .values(status=GpuStatus.AVAILABLE.value, workspace_id=None, updated_at=utcnow())
        )
    db.commit()
'''

CLEAR_BLOCK = """    if orphan_ids:
        db.execute(
            update(Workspace)
            .where(Workspace.id.in_(sorted(orphan_ids)))
            .values(gpu_id=None, gpu_index=None, gpu_name=None)
        )
"""
RECORD_LINE = "        orphan_ids.update(bid for bid in bound_ids if bid is not None)\n"


def _require_anchor(source: str, anchor: str, expected: int) -> None:
    """锚点命中数不是预期值 ⇒ 说的是"源码与判据假设的形状不同"，不是"尺子有牙"。"""
    assert source.count(anchor) == expected, (
        f"锚点命中 {source.count(anchor)} 处、预期 {expected} 处：源码形状与本判据的假设不一致，"
        f"这一支变异没有落地（不能当作开火证据）"
    )


def test_every_release_path_in_the_reclaimer_pairs_with_the_column_clear() -> None:
    """真实源码：3 个放卡位点 ↔ 3 个记名位点，清列一处、三列齐全、同一事务、只点名孤儿。"""
    assert release_column_pairing(SCHEDULER_SOURCE) == {
        "release_sites": 3,
        "clear_sites": 1,
        "columns_cleared": 3,
        "record_sites": 3,
        "unpaired_release_paths": 0,
        "clear_scope_is_recorded_list": 1,
        "clear_scope_leaks_protection_set": 0,
        "clear_before_commit": 1,
    }, release_column_pairing(SCHEDULER_SOURCE)


def test_the_pairing_checker_fires_on_the_pre_fix_shape() -> None:
    """反证一（改前真形状）：放卡位点照旧 3 处，清列一处都没有 ⇒ 三条格一起翻。

    `release_sites` 在改前后都是 3 —— 这一格不是"数本轮新加的行"，所以配对上
    `unpaired_release_paths=3` 说的确实是"三个放卡路径都没人记名"。
    """
    shape = release_column_pairing(PRE_FIX_RECOVER_SOURCE)
    assert shape["release_sites"] == 3, shape
    assert shape == {
        "release_sites": 3,
        "clear_sites": 0,
        "columns_cleared": 0,
        "record_sites": 0,
        "unpaired_release_paths": 3,
        "clear_scope_is_recorded_list": 0,
        "clear_scope_leaks_protection_set": 0,
        "clear_before_commit": 0,
    }, shape


def test_the_pairing_checker_fires_on_a_branch_that_forgets_to_record() -> None:
    """反证二（真源码变异）：`occupied_ids` 为空那一支只放卡、不记名 ⇒ 只翻配对格。

    这是"只补了一个分支"的藏法：清列语句、三列、事务都还在，缺的是那一条路没人记名。
    """
    _require_anchor(SCHEDULER_SOURCE, RECORD_LINE, 2)
    mutant = SCHEDULER_SOURCE.replace(RECORD_LINE, "", 1)
    shape = release_column_pairing(mutant)
    assert shape["release_sites"] == 3, f"变异把夹具改坏了，不算开火：{shape}"
    assert shape["clear_sites"] == 1 and shape["columns_cleared"] == 3, shape
    assert shape["record_sites"] == 2, shape
    assert shape["unpaired_release_paths"] == 1, f"少一条记名看不见：{shape}"
    # 另一极：同一把尺子跑真实源码不开火
    assert release_column_pairing(SCHEDULER_SOURCE)["unpaired_release_paths"] == 0


def test_the_pairing_checker_fires_when_a_column_is_left_behind() -> None:
    """反证三（真源码变异）：清列只写 gpu_id/gpu_index，漏掉 gpu_name ⇒ 列数从 3 掉到 2。"""
    anchor = ".values(gpu_id=None, gpu_index=None, gpu_name=None)"
    _require_anchor(SCHEDULER_SOURCE, anchor, 1)
    mutant = SCHEDULER_SOURCE.replace(anchor, ".values(gpu_id=None, gpu_index=None)", 1)
    shape = release_column_pairing(mutant)
    assert shape["clear_sites"] == 1 and shape["unpaired_release_paths"] == 0, shape
    assert shape["columns_cleared"] == 2, f"漏清一列看不见：{shape}"


def test_the_pairing_checker_fires_on_a_global_column_clear() -> None:
    """反证四（真源码变异）：清列改成全局谓词（不点名）⇒ 范围格与记名格一起翻。

    这一支同时让 `record_sites` 归零、`unpaired_release_paths` 升到 3，因为
    "被放卡的格"那份名单根本不再被消费 —— 这正是全局清的语义：它谁都不问。
    """
    _require_anchor(SCHEDULER_SOURCE, CLEAR_BLOCK, 1)
    global_block = CLEAR_BLOCK.replace(
        ".where(Workspace.id.in_(sorted(orphan_ids)))",
        ".where(Workspace.gpu_id.is_not(None))",
    )
    shape = release_column_pairing(SCHEDULER_SOURCE.replace(CLEAR_BLOCK, global_block, 1))
    assert shape["clear_sites"] == 1, shape
    assert shape["clear_scope_is_recorded_list"] == 0, f"全局清列看不见：{shape}"
    assert shape["unpaired_release_paths"] == 3, shape


def test_the_pairing_checker_fires_when_the_protection_set_leaks_into_the_clear() -> None:
    """反证五（真源码变异）：把受保护的格也一起清列 ⇒ 泄漏格必须单独翻。

    这一支刻意只动名单、不动其他形状：`record_sites` 与事务位置都保持，所以它是
    "清列作用域越界"这一条**单独**的改前证据（其余格在真实源码上不开火）。
    """
    _require_anchor(SCHEDULER_SOURCE, "update(Workspace)\n            .where(Workspace.id.in_(sorted(orphan_ids)))", 1)
    mutant = SCHEDULER_SOURCE.replace(
        "update(Workspace)\n            .where(Workspace.id.in_(sorted(orphan_ids)))",
        "update(Workspace)\n            .where(Workspace.id.in_(sorted(orphan_ids | occupied_ids)))",
        1,
    )
    shape = release_column_pairing(mutant)
    assert shape["unpaired_release_paths"] == 0 and shape["clear_before_commit"] == 1, shape
    assert shape["clear_scope_leaks_protection_set"] == 1, f"名单混进保护集看不见：{shape}"


def test_the_pairing_checker_fires_when_the_clear_commits_separately() -> None:
    """反证六（真源码变异）：清列挪到 `db.commit()` 之后 ⇒ 事务格必须单独翻。

    "同一事务"这条要求不是修辞：卡与列分两次提交之间有一个"卡已放、列还指着它"的
    崩溃窗口，落在那儿就是本轮要消灭的那个漂移。
    """
    _require_anchor(SCHEDULER_SOURCE, CLEAR_BLOCK + "    db.commit()", 1)
    mutant = SCHEDULER_SOURCE.replace(CLEAR_BLOCK + "    db.commit()", "    db.commit()\n" + CLEAR_BLOCK, 1)
    shape = release_column_pairing(mutant)
    assert shape["clear_sites"] == 1 and shape["columns_cleared"] == 3, f"变异把夹具改坏了：{shape}"
    assert shape["unpaired_release_paths"] == 0, shape
    assert shape["clear_before_commit"] == 0, f"清列跑到事务外看不见：{shape}"
    # 另一极：真实源码上这一格是 1
    assert release_column_pairing(SCHEDULER_SOURCE)["clear_before_commit"] == 1

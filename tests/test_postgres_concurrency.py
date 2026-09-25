"""PostgreSQL 真并发语义用例（pg_integration）。

为什么必须有这一档：`GpuScheduler.allocate` 的
`SELECT ... FOR UPDATE SKIP LOCKED` 在 SQLite 下被方言直接丢弃（no-op，见
tests/test_scheduler.py::test_sqlite_dialect_drops_for_update），worker lease 与
warm pool claim 的 CAS 也只是靠 SQLite 单写者"顺带"成立。本文件在真实 PG 服务器
上把这几件事逐个读到定案：行锁真的存在、SKIP LOCKED 真的绕开被锁行、CAS 并发下
只有一个赢家、幂等 key 与部分唯一索引由数据库拒绝重复。

运行：`make test-pg`（需 docker daemon + 本地缓存 postgres 镜像）。
缺件时整档干净跳过，skip 文案带 POSTGRES_VALIDATION_PENDING 供 release gate 登记。
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pg_server
import pytest
import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models import (
    BillingAccount,
    CreditHold,
    CreditLedger,
    Gpu,
    GpuAllocation,
    GpuHost,
    GpuStatus,
    HoldStatus,
    LedgerType,
    OperationStatus,
    OperationType,
    User,
    Workspace,
    WorkspaceOperation,
)
from app.services.ledger import CreditLedgerService
from app.services.scheduler import GpuScheduler
from app.services.worker import OperationWorker, enqueue_operation

pytestmark = pytest.mark.pg_integration

LOCK_TIMEOUT = "250ms"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def seed_gpus(factory, count: int) -> list[str]:
    """建一个 host + count 张 AVAILABLE GPU；memory_total 互异 → 候选顺序确定。

    host 先单独提交：同一 flush 批次里 parent/child 的插入顺序在 PG 上会被
    FK 拦下（SQLite 因 FK 未启用而静默放过 —— 见
    test_sqlite_fk_enforcement_is_off_today 的 as-is 记录）。
    """
    with factory() as db:
        host = GpuHost(
            id=str(uuid.uuid4()),
            name=f"host-{uuid.uuid4().hex[:6]}",
            address="127.0.0.1",
            provider="docker",
        )
        db.add(host)
        db.commit()
        ids = []
        for i in range(count):
            gpu = Gpu(
                id=str(uuid.uuid4()),
                gpu_uuid=f"GPU-{uuid.uuid4().hex}",
                host_id=host.id,
                model="test-gpu",
                memory_total=24576 + i,
                gpu_index=i,
                status=GpuStatus.AVAILABLE.value,
            )
            db.add(gpu)
            ids.append(gpu.id)
        db.commit()
        return ids


def seed_user(factory) -> str:
    """账本 FK 指向 users.id，PG 下必须先有用户。"""
    from app.models import Role, User

    user_id = str(uuid.uuid4())
    with factory() as db:
        db.add(
            User(
                id=user_id,
                email=f"pg-{user_id[:8]}@example.invalid",
                username=user_id[:8],
                password_hash="x",  # noqa: S106  非真实口令：FK 用例只需要一行用户记录
                role=Role.USER.value,
            )
        )
        db.commit()
    return user_id


def ordered_gpu_ids(factory) -> list[str]:
    """按 allocate() 的候选顺序（memory_total 升序）列出 AVAILABLE 的 GPU id。"""
    with factory() as db:
        return [
            g.id
            for g in db.scalars(
                select(Gpu).where(Gpu.status == GpuStatus.AVAILABLE.value).order_by(Gpu.memory_total.asc())
            )
        ]


def host_of(factory, gpu_id: str) -> str:
    with factory() as db:
        gpu = db.get(Gpu, gpu_id)
        assert gpu is not None
        return gpu.host_id


def set_lock_timeout(db) -> None:
    db.execute(sa.text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))


def assert_lock_timeout(exc: BaseException, label: str) -> None:
    """撞墙判据必须是"等锁超时"，不是别的偶发错误。"""
    msg = str(exc).lower()
    assert "lock timeout" in msg, f"{label}：未按预期因行锁超时，实际={msg}"


def ensure_workspace(factory, ws_id: str) -> str:
    """建一条指定 id 的 workspace 行。

    `gpu_allocations.workspace_id` 现在是真的外键：分配表只接受存在的 workspace。
    用例里那些 "ws-keep" / "ws-c" 之类的字面量因此必须有父行——这正是生产路径
    （provision 先落 workspace 再分配）的形状。
    """
    with factory() as db:
        if db.get(Workspace, ws_id) is None:
            db.add(
                Workspace(
                    id=ws_id,
                    name="ws",
                    template_id="cartpole",
                    provider="mock",
                    status="queued",
                )
            )
            db.commit()
    return ws_id


def new_workspace(factory, status: str = "queued", warm_pool_state: str | None = None) -> str:
    ws_id = f"ws-{uuid.uuid4().hex[:8]}"
    with factory() as db:
        db.add(
            Workspace(
                id=ws_id,
                name="ws",
                template_id="cartpole",
                provider="mock",
                status=status,
                warm_pool_state=warm_pool_state,
            )
        )
        db.commit()
    return ws_id


# ---------------------------------------------------------------------------
# 1. 迁移链与 schema 落地（PG 方言）
# ---------------------------------------------------------------------------


def test_alembic_chain_schema_landing_on_postgres(pg_url):
    engine = sa.create_engine(pg_url, future=True)
    try:
        with engine.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    sa.text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
                    )
                )
            }
            assert {
                "users",
                "organizations",
                "workspaces",
                "templates",
                "template_versions",
                "gpus",
                "gpu_allocations",
                "credit_ledger",
                "workspace_operations",
                "streaming_sessions",
                "courses",
                "deployments",
                "edge_agents",
            } <= tables, f"PG 上缺表：{sorted(tables)}"

            # §13 部分唯一索引必须带 active-status 谓词（PG 侧真语义）
            indexdef = conn.execute(
                sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_ops_active_per_workspace'")
            ).scalar()
            assert indexdef is not None, "uq_ops_active_per_workspace 未在 PG 创建"
            low = indexdef.lower()
            assert "unique" in low and "where" in low, f"部分唯一索引形状不符：{indexdef}"
            for status in ("pending", "running", "retrying"):
                assert status in low, f"索引谓词缺 {status}：{indexdef}"

            # §30：外键在 PG 是数据库级强制（SQLite 默认不启用）。逐名点名，
            # 不用"数量 ≥ N"这种会被悄悄削弱的判据。
            fk_names = {
                row[0]
                for row in conn.execute(
                    sa.text(
                        "SELECT constraint_name FROM information_schema.table_constraints "
                        "WHERE constraint_schema='public' AND constraint_type='FOREIGN KEY'"
                    )
                )
            }
            assert {
                # 本轮补齐的"真指针"外键（b7e4c1a09f52）：迁移必须真的在 PG 上落地
                "fk_gpuallocation_workspace_id",
                "fk_gpuallocation_host_id",
                "fk_gpu_workspace_id",
                "fk_credithold_workspace_id",
                "fk_deploymentrecord_workspace_id",
                "fk_deploymentrecord_edge_agent_id",
                "fk_telemetryevent_edge_agent_id",
                "fk_workspace_template_version_id",
                "fk_artifact_workspace_id",
                "fk_streamingsession_workspace_id",
                "fk_submission_workspace_id",
                "gpus_host_id_fkey",
                "gpu_allocations_gpu_id_fkey",
                "credit_ledger_user_id_fkey",
                "workspace_operations_workspace_id_fkey",
                "template_versions_template_id_fkey",
                "course_members_course_id_fkey",
                "users_organization_id_fkey",
                # §6 租户归属：这两条曾长期只存在于模型声明里（3f0c9a51b7e2 才补进迁移）
                "fk_edge_agents_owner_user_id",
                "fk_edge_agents_organization_id",
                # §17/§18 新表
                "fk_credit_holds_account",
                "fk_billing_accounts_owner_user",
            } <= fk_names, f"PG 缺关键外键：{sorted(fk_names)}"
    finally:
        engine.dispose()


def test_downgrade_to_base_on_postgres_drops_everything(pg_server_url):
    """整链可上可下（PG 方言），不是只有 SQLite 走得通。"""
    dbname = f"ec_dg_{uuid.uuid4().hex[:8]}"
    pg_server.create_database(pg_server_url.server_url(), dbname)
    url = pg_server_url.database_url(dbname)
    try:
        pg_server.migrate(url)
        pg_server.migrate(url, "base")
        engine = sa.create_engine(url, future=True)
        try:
            with engine.connect() as conn:
                left = {
                    row[0]
                    for row in conn.execute(
                        sa.text(
                            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
                        )
                    )
                }
        finally:
            engine.dispose()
        assert left == {"alembic_version"}, f"downgrade base 后残留表：{sorted(left)}"
    finally:
        pg_server.drop_database(pg_server_url.server_url(), dbname)


def test_orphan_row_rejected_by_postgres_foreign_keys(pg_factory):
    """FK 在 PG 真拦：给不存在的 host 挂 GPU 必须被数据库拒绝。"""
    with pg_factory() as db:
        db.add(
            Gpu(
                id=str(uuid.uuid4()),
                gpu_uuid=f"GPU-{uuid.uuid4().hex}",
                host_id="no-such-host",
                model="m",
                memory_total=1000,
                status=GpuStatus.AVAILABLE.value,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


# ---------------------------------------------------------------------------
# 2. FOR UPDATE / SKIP LOCKED 的效力（成对：必须撞墙 + 必须绕开）
# ---------------------------------------------------------------------------


def test_for_update_blocks_and_skip_locked_bypasses(pg_factory):
    """同一交错的两档判据：普通 FOR UPDATE 等锁到超时；SKIP LOCKED 立刻错开。"""
    seed_gpus(pg_factory, 2)
    ordered = ordered_gpu_ids(pg_factory)
    assert len(ordered) == 2
    first, second = ordered

    def lock_one(db, *, skip_locked: bool) -> list[str]:
        stmt = (
            select(Gpu)
            .where(Gpu.status == GpuStatus.AVAILABLE.value)
            .order_by(Gpu.memory_total.asc())
            .with_for_update(skip_locked=skip_locked)
            .limit(1)
        )
        return [g.id for g in db.scalars(stmt)]

    holder = pg_factory()
    holder.begin()
    assert lock_one(holder, skip_locked=False) == [first]
    try:
        # 必须撞墙
        with pg_factory() as blocked:
            blocked.begin()
            set_lock_timeout(blocked)
            with pytest.raises(DBAPIError) as exc:
                lock_one(blocked, skip_locked=False)
            blocked.rollback()
        assert_lock_timeout(exc.value, "普通 FOR UPDATE 未被行锁挡住（锁形同虚设？）")

        # 必须绕开（不等待，拿到的是另一张卡）
        with pg_factory() as skipper:
            skipper.begin()
            set_lock_timeout(skipper)
            assert lock_one(skipper, skip_locked=True) == [second]
            skipper.rollback()
    finally:
        holder.rollback()
        holder.close()


def test_skip_locked_is_what_prevents_the_collision(pg_factory):
    """差分对照：去掉 SKIP LOCKED，两路会读到同一张卡，后提交者撞唯一约束。"""
    seed_gpus(pg_factory, 2)
    ordered = ordered_gpu_ids(pg_factory)

    def read_locked(db) -> list[str]:
        stmt = (
            select(Gpu)
            .where(Gpu.status == GpuStatus.AVAILABLE.value)
            .order_by(Gpu.memory_total.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        return [g.id for g in db.scalars(stmt)]

    def read_unlocked(db) -> list[str]:
        stmt = (
            select(Gpu)
            .where(Gpu.status == GpuStatus.AVAILABLE.value)
            .order_by(Gpu.memory_total.asc())
        )
        return [g.id for g in db.scalars(stmt)]

    # 带锁：A 锁住第一候选后，B 的候选集与 A 不重叠
    a = pg_factory()
    a.begin()
    a_ids = read_locked(a)
    try:
        with pg_factory() as b:
            b.begin()
            set_lock_timeout(b)
            b_ids = read_locked(b)
            b.rollback()
        assert a_ids == [ordered[0]] and b_ids == [ordered[1]], f"SKIP LOCKED 未错开：{a_ids} / {b_ids}"
    finally:
        a.rollback()
        a.close()

    # 不带锁：两路读到同一张卡 → 真实交错下的双绑风险由唯一约束兜住
    with pg_factory() as c, pg_factory() as d:
        c.begin()
        d.begin()
        same_c = read_unlocked(c)
        same_d = read_unlocked(d)
        assert same_c == same_d, f"不带锁的读法本应重叠：{same_c} / {same_d}"
        ensure_workspace(pg_factory, "ws-c")
        ensure_workspace(pg_factory, "ws-d")
        c.add(
            GpuAllocation(
                id=str(uuid.uuid4()),
                gpu_id=same_c[0],
                workspace_id="ws-c",
                host_id=host_of(pg_factory, same_c[0]),
            )
        )
        c.commit()
        d.add(
            GpuAllocation(
                id=str(uuid.uuid4()),
                gpu_id=same_d[0],
                workspace_id="ws-d",
                host_id=host_of(pg_factory, same_d[0]),
            )
        )
        with pytest.raises(IntegrityError):
            d.commit()
        d.rollback()


def test_allocate_succeeds_while_one_candidate_is_locked(pg_factory):
    """现行读法（limit 1）：别人锁走一张，另一张仍可正常分配。"""
    seed_gpus(pg_factory, 2)
    ordered = ordered_gpu_ids(pg_factory)
    scheduler = GpuScheduler(pg_factory)
    lock_one = (
        select(Gpu)
        .where(Gpu.status == GpuStatus.AVAILABLE.value)
        .order_by(Gpu.memory_total.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )

    holder = pg_factory()
    holder.begin()
    locked = [g.id for g in holder.scalars(lock_one)]
    assert locked == [ordered[0]]
    try:
        with pg_factory() as db:
            ensure_workspace(pg_factory, "ws-while-locked")
            gpu = scheduler.allocate(db, "ws-while-locked", gpu_requirement_gb=0)
        assert gpu.id == ordered[1], f"应分到未被锁的那张：{gpu.id} vs {ordered}"
    finally:
        holder.rollback()
        holder.close()

    with pg_factory() as db:
        assert db.get(Gpu, ordered[0]).status == GpuStatus.AVAILABLE.value, "被锁的候选不该被改动"
        assert db.get(Gpu, ordered[1]).status == GpuStatus.ALLOCATED.value


def test_unbounded_candidate_read_starves_concurrent_allocate(pg_factory):
    """旧读法（不带 limit）复刻：一次锁走整批候选 → 并发 allocate 被误判「无卡可用」。

    这条用例钉的是"为什么必须 limit 1"，不是新功能：把它当旧行为的必须开火对照。
    """
    seed_gpus(pg_factory, 2)
    scheduler = GpuScheduler(pg_factory)
    lock_all = (
        select(Gpu)
        .where(Gpu.status == GpuStatus.AVAILABLE.value)
        .order_by(Gpu.memory_total.asc())
        .with_for_update(skip_locked=True)
    )

    starver = pg_factory()
    starver.begin()
    assert len([g.id for g in starver.scalars(lock_all)]) == 2, "无 limit 的读法本应锁走两张候选"
    try:
        with pg_factory() as db, pytest.raises(RuntimeError, match="No GPU available"):
            ensure_workspace(pg_factory, "ws-starved")
            scheduler.allocate(db, "ws-starved", gpu_requirement_gb=0)
    finally:
        starver.rollback()
        starver.close()

    # 放锁之后同一请求立即成功 → 证明刚才的失败是"被锁饿死"，不是真的没卡
    with pg_factory() as db:
        ensure_workspace(pg_factory, "ws-after-release")
        gpu = scheduler.allocate(db, "ws-after-release", gpu_requirement_gb=0)
    assert gpu.status == GpuStatus.ALLOCATED.value
    assert len(ordered_gpu_ids(pg_factory)) == 1, "放锁并分配后只该剩一张空闲卡"


def test_concurrent_allocate_never_double_books(pg_factory):
    n_gpus, n_threads = 3, 6
    seed_gpus(pg_factory, n_gpus)
    scheduler = GpuScheduler(pg_factory)
    barrier = threading.Barrier(n_threads)
    results: list[tuple[str, str | None, str | None]] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        workspace_id = f"ws-{idx}-{uuid.uuid4().hex[:8]}"
        ensure_workspace(pg_factory, workspace_id)
        barrier.wait()
        with pg_factory() as db:
            try:
                gpu = scheduler.allocate(db, workspace_id, gpu_requirement_gb=0)
                outcome: tuple[str, str | None, str | None] = (workspace_id, gpu.id, None)
            except RuntimeError as exc:  # 卡不够 → 明确失败，不抢占
                outcome = (workspace_id, None, str(exc))
        with lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(worker, range(n_threads)))

    won = [r for r in results if r[1] is not None]
    lost = [r for r in results if r[1] is None]
    assert len(won) == n_gpus, f"应有 {n_gpus} 个成功分配：{results}"
    assert len(lost) == n_threads - n_gpus
    gpu_ids = [r[1] for r in won]
    assert len(set(gpu_ids)) == n_gpus, f"同一张 GPU 被分配两次：{gpu_ids}"

    with pg_factory() as db:
        allocs = list(db.scalars(select(GpuAllocation)))
        assert len(allocs) == n_gpus, f"分配记录数异常：{len(allocs)}"
        assert len({a.gpu_id for a in allocs}) == n_gpus, "gpu_allocations.gpu_id 唯一约束未拦住重复"
        assert len(list(db.scalars(select(Gpu).where(Gpu.status == GpuStatus.ALLOCATED.value)))) == n_gpus


def test_allocate_is_crash_safe_on_postgres(pg_factory):
    """allocate 中途回滚不留半成品：GPU 不被占用、无孤儿分配行。"""
    seed_gpus(pg_factory, 1)
    scheduler = GpuScheduler(pg_factory)
    with pg_factory() as db:
        ensure_workspace(pg_factory, "ws-keep")
        gpu = scheduler.allocate(db, "ws-keep", gpu_requirement_gb=0)
        allocated_gpu_id = gpu.id
    scheduler = GpuScheduler(pg_factory)
    with pg_factory() as db, pytest.raises(RuntimeError):
        ensure_workspace(pg_factory, "ws-second")
        scheduler.allocate(db, "ws-second", gpu_requirement_gb=0)
    with pg_factory() as db:
        assert db.get(Gpu, allocated_gpu_id).status == GpuStatus.ALLOCATED.value
        assert [a.workspace_id for a in db.scalars(select(GpuAllocation))] == ["ws-keep"]


def test_allocate_locks_exactly_one_candidate_row(pg_factory):
    """锁范围实测：allocate 一条读法只该锁走一张候选。

    手法：在 allocate 的 FOR UPDATE 刚执行完、事务尚未提交时（SQLAlchemy
    after_cursor_execute 钩子里），让**另一个会话**用
    `FOR UPDATE SKIP LOCKED` 数还剩几张能被锁住 —— 被目标锁走的行会直接从结果
    里消失，所以"剩几张"就是"目标锁了几张"。读数来自数据库自己，不靠调度运气。
    （不用 pg_locks 的 locktype='tuple' 计数：实测普通 FOR UPDATE 的持有者在
    pg_locks 只留 relation 级 RowShareLock，行锁记在元组头上，无 waiter 不落锁表。）

    旧实现不带 `LIMIT 1`，一次锁走整批候选 → 并发启动的后来者扫到 0 行，在卡其实
    空闲时被误判「No GPU available」。
    """
    import sqlalchemy.event as sa_event

    total = 3
    seed_gpus(pg_factory, total)
    target = pg_factory()
    spy = pg_factory()
    remaining: list[int] = []

    def unlocked_candidate_count() -> int:
        value = spy.scalar(
            sa.text(
                "SELECT count(*) FROM ("
                "  SELECT id FROM gpus WHERE status = 'available'"
                "  ORDER BY memory_total FOR UPDATE SKIP LOCKED"
                ") locked_now"
            )
        )
        spy.rollback()
        return int(value or 0)

    def hook(conn, cursor, statement, parameters, context, executemany):
        upper = statement.upper().lstrip()
        # 只认被测路径自己发出的 ORM 读法（SELECT gpus.<col>, ... FROM gpus ...）；
        # 探针自身的 count 语句必须跳过，否则钩子递归打自己
        if upper.startswith("SELECT GPUS.") and "FOR UPDATE" in upper:
            remaining.append(unlocked_candidate_count())

    sa_event.listen(target.get_bind(), "after_cursor_execute", hook)
    try:
        ensure_workspace(pg_factory, "ws-lock-scope")
        gpu = GpuScheduler(pg_factory).allocate(target, "ws-lock-scope", gpu_requirement_gb=0)
        target.commit()
    finally:
        sa_event.remove(target.get_bind(), "after_cursor_execute", hook)
        target.close()
        spy.close()

    assert remaining, "没有捕获到 allocate 的 FOR UPDATE 语句（判据未落到被测路径上）"
    assert remaining[0] == total - 1, (
        f"allocate 一条读法锁走了 {total - remaining[0]} 张候选（应只锁 1 张；"
        "整批锁定会饿死并发启动）"
    )
    assert gpu.status == GpuStatus.ALLOCATED.value


# ---------------------------------------------------------------------------
# 4. worker lease：CAS 在真锁下只有一个赢家
# ---------------------------------------------------------------------------


class _NullExecutor:
    def execute_operation(self, db, op):  # pragma: no cover - 本文件不驱动执行
        raise AssertionError("不该被执行")


def test_concurrent_enqueue_serialized_by_partial_unique_index(pg_factory):
    workspace_id = new_workspace(pg_factory)
    n = 8
    barrier = threading.Barrier(n)
    outcomes: list[object] = []
    lock = threading.Lock()

    def one(_: int) -> None:
        barrier.wait()
        try:
            op = enqueue_operation(pg_factory, workspace_id, OperationType.START)
        except Exception as exc:
            with lock:
                outcomes.append(exc)
            return
        with lock:
            outcomes.append(op)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(one, range(n)))

    accepted = [o for o in outcomes if isinstance(o, WorkspaceOperation)]
    rejected = [o for o in outcomes if o is None]
    leaked = [o for o in outcomes if isinstance(o, Exception)]
    assert not leaked, f"并发 enqueue 泄漏了未吸收的异常：{leaked}"
    assert len(accepted) == 1 and len(rejected) == n - 1, f"部分唯一索引未串行化：{len(accepted)}/{n}"
    with pg_factory() as db:
        assert len(list(db.scalars(select(WorkspaceOperation)))) == 1


def test_lease_cas_blocks_then_loses_under_real_row_lock(pg_factory):
    workspace_id = new_workspace(pg_factory)
    with pg_factory() as db:
        op = enqueue_operation(db, workspace_id, OperationType.PROVISION)
        assert op is not None
        op_id = op.id

    worker_a = OperationWorker(pg_factory, _NullExecutor())
    worker_b = OperationWorker(pg_factory, _NullExecutor())
    from app.utils import utcnow

    now = utcnow()
    with pg_factory() as db:
        loaded = db.get(WorkspaceOperation, op_id)
        assert loaded is not None and loaded.status == OperationStatus.PENDING.value
        assert worker_a._try_claim(db, loaded, now) is True

    with pg_factory() as db:
        stale = db.get(WorkspaceOperation, op_id)
        assert stale is not None
        db.expunge(stale)  # 脱离会话：模拟"B 在 A 之前读到的旧快照"，不能被 autoflush 写回
        stale.status = OperationStatus.PENDING.value
        assert worker_b._try_claim(db, stale, now) is False, "CAS 拿着旧状态却抢到了"
        db.rollback()

    # 真锁证据：A 持有未提交写时，B 对同一行的 UPDATE 必须排队（超时可见）
    holder = pg_factory()
    holder.begin()
    holder.execute(
        update(WorkspaceOperation)
        .where(WorkspaceOperation.id == op_id)
        .values(lease_owner="holder", last_error="holding")
    )
    try:
        with pg_factory() as blocked:
            blocked.begin()
            set_lock_timeout(blocked)
            with pytest.raises(DBAPIError) as exc:
                blocked.execute(
                    update(WorkspaceOperation)
                    .where(
                        WorkspaceOperation.id == op_id,
                        WorkspaceOperation.status == OperationStatus.RUNNING.value,
                    )
                    .values(lease_owner="challenger")
                )
            blocked.rollback()
        assert_lock_timeout(exc.value, "同一行的并发 UPDATE 未排队（行锁不生效？）")
    finally:
        holder.rollback()
        holder.close()

    with pg_factory() as db:
        check = db.get(WorkspaceOperation, op_id)
        assert check is not None
        assert check.status == OperationStatus.RUNNING.value
        assert check.lease_owner == worker_a.worker_id, "输家把赢家的 lease 覆盖了"


# ---------------------------------------------------------------------------
# 5. 账本幂等：并发同 key 只入账一次
# ---------------------------------------------------------------------------


def test_concurrent_ledger_record_same_key_is_idempotent(pg_factory):
    ledger = CreditLedgerService(pg_factory)
    user_id = seed_user(pg_factory)
    key = f"usage:{uuid.uuid4().hex}"
    n = 8
    barrier = threading.Barrier(n)
    lock = threading.Lock()
    amounts: list[int] = []

    def one(_: int) -> None:
        barrier.wait()
        with pg_factory() as db:
            entry = ledger.record(
                db,
                type=LedgerType.USAGE,
                amount=-100,
                user_id=user_id,
                idempotency_key=key,
            )
            with lock:
                amounts.append(entry.amount)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(one, range(n)))

    assert len(amounts) == n
    with pg_factory() as db:
        rows = list(db.scalars(select(CreditLedger).where(CreditLedger.user_id == user_id)))
        assert len(rows) == 1, f"幂等 key 被并发突破：{len(rows)} 行"
        assert ledger.balance(db, user_id) == -100, "重复扣款（余额应为 -100，而非 -100×N）"


# ---------------------------------------------------------------------------
# 6. warm pool claim CAS：READY → CLAIMING 只有一个赢家
# ---------------------------------------------------------------------------


def test_warm_pool_claim_cas_has_single_winner(pg_factory):
    ws_id = new_workspace(pg_factory, status="running", warm_pool_state="ready")

    def claim(db, owner: str) -> int:
        result = db.execute(
            update(Workspace)
            .where(Workspace.id == ws_id, Workspace.warm_pool_state == "ready")
            .values(warm_pool_state="claiming", container_name=owner)
        )
        return int(result.rowcount or 0)

    holder = pg_factory()
    holder.begin()
    assert claim(holder, "A") == 1
    try:
        with pg_factory() as challenger:
            challenger.begin()
            set_lock_timeout(challenger)
            with pytest.raises(DBAPIError) as exc:
                claim(challenger, "B")
            challenger.rollback()
        assert_lock_timeout(exc.value, "warm pool claim 未串行化")
    finally:
        holder.commit()
        holder.close()

    # A 提交后，迟到的 B 重读谓词必须落空（同一 runtime 不能交给两个用户）
    with pg_factory() as late:
        assert claim(late, "C") == 0
        late.commit()
    with pg_factory() as db:
        ws = db.get(Workspace, ws_id)
        assert ws is not None
        assert ws.warm_pool_state == "claiming" and ws.container_name == "A"


# ---------------------------------------------------------------------------
# 7. §18 预授权：只有真行锁才证得出"同一余额不被花两次"
# ---------------------------------------------------------------------------


def make_hold_policy(factory):
    from app.services.billing import BillingPolicy
    from app.services.ledger import CreditLedgerService

    return BillingPolicy(
        factory,
        CreditLedgerService(factory),
        minimum_launch_minutes=5,
        enforce_preauthorization=True,
        hold_ttl_minutes=30,
    )


def seed_hold_user(factory, credits: int) -> str:
    from app.models import LedgerType, Role, User

    user_id = str(uuid.uuid4())
    with factory() as db:
        db.add(
            User(
                id=user_id,
                email=f"hold-{user_id[:8]}@example.org",
                username=user_id[:8],
                password_hash="x",  # noqa: S106  测试用假口令
                role=Role.USER.value,
            )
        )
        db.commit()
        CreditLedgerService(factory).record(
            db,
            type=LedgerType.RECHARGE,
            amount=credits,
            user_id=user_id,
            idempotency_key=f"recharge:{user_id}",
        )
    return user_id


def pending_hold_count(factory) -> int:
    with factory() as db:
        return len(
            list(db.scalars(select(CreditHold).where(CreditHold.status == HoldStatus.PENDING.value)))
        )


def test_concurrent_reserve_same_workspace_creates_one_hold(pg_factory):
    """同一 workspace 并发预授权：只能存在一条 pending hold（部分唯一索引）。"""
    policy = make_hold_policy(pg_factory)
    user_id = seed_hold_user(pg_factory, 10_000)
    ws = new_workspace(pg_factory)
    n = 8
    barrier = threading.Barrier(n)
    outcomes: list[object] = []
    lock = threading.Lock()

    def one(_: int) -> None:
        barrier.wait()
        with pg_factory() as db:
            try:
                hold = policy.reserve_launch(db, db.get(User, user_id), ws)
                result: object = hold.id if hold is not None else None
            except Exception as exc:  # 未吸收的异常同样要暴露
                result = exc
        with lock:
            outcomes.append(result)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(one, range(n)))

    leaked = [o for o in outcomes if isinstance(o, Exception)]
    assert not leaked, f"并发 reserve 泄漏未吸收异常：{leaked}"
    assert pending_hold_count(pg_factory) == 1, "同一 workspace 圈出了多条 pending hold"
    assert len(set(outcomes)) == 1, f"并发重试没收敛到同一条 hold：{set(outcomes)}"


def test_reserve_refuses_second_launch_once_balance_is_held(pg_factory):
    """400 credits、每笔圈 300：第二笔必须被拒，且不留 hold 行。"""
    from app.services.billing import BillingError

    policy = make_hold_policy(pg_factory)
    user_id = seed_hold_user(pg_factory, 400)
    with pg_factory() as db:
        user = db.get(User, user_id)
        assert policy.reserve_launch(db, user, new_workspace(pg_factory)) is not None
        with pytest.raises(BillingError, match="insufficient credits to reserve"):
            policy.reserve_launch(db, user, new_workspace(pg_factory))
    assert pending_hold_count(pg_factory) == 1
    with pg_factory() as db:
        assert len(list(db.scalars(select(CreditHold)))) == 1, "被拒的启动不得留下行"


def test_billing_account_row_is_a_real_exclusive_lock(pg_factory):
    """两档并排：不锁时两个事务看到同一份余额（竞态是真的）；锁上后第二路必须等待。

    这就是"为什么要 BillingAccount 这个锁根"的实证：SQLite 档证不了（方言丢掉
    FOR UPDATE），双花只能靠 PG 的行锁挡住。
    """
    from sqlalchemy import delete

    policy = make_hold_policy(pg_factory)
    user_id = seed_hold_user(pg_factory, 400)

    # 档一（前提）：两个都未加锁的读事务看到同样的可用额 → 说明检查会互相穿透
    with pg_factory() as a, pg_factory() as b:
        a.begin()
        b.begin()
        user = a.get(User, user_id)
        assert policy.available_credits(a, user) == policy.available_credits(b, user) == 400
        a.rollback()
        b.rollback()

    # 档二（必须撞墙）：A 锁住账户行不提交，B 的 lock_accounts 只能等到 lock_timeout
    holder = pg_factory()
    holder.begin()
    accounts = policy.user_accounts(holder, holder.get(User, user_id))
    policy.lock_accounts(holder, accounts)
    try:
        with pg_factory() as challenger:
            challenger.begin()
            set_lock_timeout(challenger)
            other_accounts = challenger.scalars(
                select(BillingAccount).where(BillingAccount.id.in_([a.id for a in accounts]))
            ).all()
            with pytest.raises(DBAPIError) as exc:
                policy.lock_accounts(challenger, other_accounts)
            challenger.rollback()
        assert_lock_timeout(exc.value, "计费主体行没有被真正串行化（预授权会双花）")
    finally:
        holder.rollback()
        holder.close()

    # 收尾：确认释放锁后可用额仍可读（不是把库锁坏了）
    with pg_factory() as after:
        assert policy.available_credits(after, after.get(User, user_id)) == 400
        after.execute(delete(CreditHold))
        after.commit()


def test_missing_workspace_is_not_reported_as_no_capacity(pg_factory):
    """分配表的外键冲突必须报成数据问题，不是"没有空闲卡"。

    allocate() 原先把任何 IntegrityError 都当成"卡被别人抢了"→ 回滚重试；
    给 gpu_allocations.workspace_id 加外键之后，这条会把"workspace 行不存在"
    一路误报成容量不足（本轮 PG 档实测就是这个路径）。反向对照：父行存在时
    同一请求成功（其余 allocate 用例已覆盖）。
    """
    seed_gpus(pg_factory, 1)
    with pg_factory() as db, pytest.raises(RuntimeError, match="非并发争用") as exc:
        GpuScheduler(pg_factory).allocate(db, "ws-does-not-exist", gpu_requirement_gb=0)
    assert "No GPU available" not in str(exc.value), "外键冲突又被误报成容量不足"

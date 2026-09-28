"""Warm pool claim 撤销路径：卡只放回「provider 亲口说 runtime 已退役」的那一格（N-63 家族第四处）。

缺陷形状（改前，`app/services/warmpool.py` 的 claim 撤销分支）：`provider.destroy` 抛错
→ 只留一行 error 日志 → 照样 `scheduler.release` → 照样写 FAILED，而容器可能还在吃
`--gpus device=N`。更糟的是 `container_name` 被一起清空：那一列是「这一格对应哪个容器」的事实
本体，抹掉之后 reconcile/inspect/logs/destroy 只能按约定名重新推断。（登记 N-69 时
`DockerProvider.stop`（现 `app/services/providers/docker.py:305-307`）还只按这一列找容器，
清列等于「永远停不掉」；N-78 把推导收成 `_name()`（`docker.py:60-68`）之后 stop 会回退到
约定名，所以这一半后果已降为「事实被抹掉、重试靠推断」，不放卡那一半不变。）三处失明让它长期无人看见：

- `_cleanup_failed` 的选择集只读 `warm_pool_state == FAILED`，DRAINING 行等不到补偿 DESTROY；
- `reconcile_all` 按 status 跳过 STOPPED/FAILED（app/services/orchestrator.py:524-527）；
- `recover_stuck_gpu_allocations`（app/services/scheduler.py:271-283）只保护非终态
  {PROVISIONING, RUNNING, STOPPING} —— 写成 FAILED 的那一行的卡会被它强制放掉。

释放准入判据**不在本模块重抄**：全场只有 `WorkspaceOrchestrator._release_admitted`
一份（app/services/orchestrator.py:385-403），本模块只消费它。两极缺一不可：

- 不放行档：destroy 报错而 provider 仍自述 ALIVE（或 UNKNOWN 到无法判定）⇒ 留卡、
  留 `container_name`、不写终态，交给 `_cleanup_failed` 的 DRAINING 档重试；
- 放行档：destroy 报错但 provider 亲口说 MISSING（K8s 对已删 Deployment 的 404 正是这一档，
  app/services/providers/k8s.py:563-570 的 404 → MISSING）⇒ 必须放卡，否则那张卡被
  永久钉在一个已经没有使用者的 runtime 上。

最后几支是文本面（AST）判据：`scheduler.release` 这个调用**存在于** claim() 里不证明任何事，
只有"它坐在一个以判据为条件的分支里"才算接上；`_cleanup_failed` 写了 DESTROY 入队也不证明
DRAINING 进得了选择集。两支各配自己的反向对照（含一份合成源码），不开火的尺子不算尺子。
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    Gpu,
    GpuAllocation,
    GpuHost,
    GpuStatus,
    OperationStatus,
    Template,
    User,
    WarmPoolState,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from app.security import WorkspaceCredentialCipher
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.base import RuntimeState
from app.services.providers.mock import MockProvider
from app.services.scheduler import recover_stuck_gpu_allocations
from app.services.warmpool import WarmPoolManager
from app.services.worker import OperationWorker
from tests.dbfiles import db_url

ENGINE = create_engine(
    db_url("warmpool-claim-admission"), connect_args={"check_same_thread": False}
)
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)

REPO_ROOT = Path(__file__).resolve().parents[1]
WARMPOOL_SOURCE = (REPO_ROOT / "app" / "services" / "warmpool.py").read_text(encoding="utf-8")

CIPHER = WorkspaceCredentialCipher("test-key-wp-admit-001")

# 撤销档里 provider 会说的三种回话
REPORTS = {
    "alive": RuntimeState.ALIVE,
    "missing": RuntimeState.MISSING,
    "unknown": RuntimeState.UNKNOWN,
}


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


# ---------------------------------------------------------------------------
# 夹具（形状抄 tests/test_warmpool_claim.py：模块级 engine + autouse 重建表）
# ---------------------------------------------------------------------------
def _make_template(db) -> Template:
    t = Template(
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
    db.add(t)
    db.commit()
    return t


def _seed_gpu(db) -> None:
    db.add(GpuHost(id="host-1", name="h1", address="127.0.0.1", provider="mock"))
    db.add(
        Gpu(
            id="gpu-1",
            gpu_uuid="gpu-1",
            host_id="host-1",
            model="Mock GPU",
            memory_total=32768,
            gpu_index=0,
        )
    )
    db.commit()


def _make_user(db, user_id: str) -> User:
    user = User(
        id=user_id,
        email=f"{user_id}@example.com",
        username=user_id,
        password_hash="x",  # noqa: S106 测试桩用户，非真实密码
    )
    db.add(user)
    db.commit()
    return user


class ClaimAbortProvider(MockProvider):
    """rotate 恒 False（触发撤销），runtime 事实与 destroy 成不成都由它自己声明。

    mock 只会说 UNKNOWN（providers/mock.py:72-74），它证不了"还活着"这件事，
    所以这里换一把自带 runtime 事实的替身（与 tests/test_stop_release_admission.py 的
    `StatefulProvider` 同一立场）。

    - `destroy_raises=True,  reports="alive"`   ⇒ 不放行档（卡必须留下）
    - `destroy_raises=True,  reports="unknown"` ⇒ 同样不放行（答不上来不算"亲口说没了"）
    - `destroy_raises=True,  reports="missing"` ⇒ 放行档（K8s 404 那一极）
    - `destroy_raises=False, reports="missing" / "unknown"` ⇒ 命令成功档（演示路径必须停得下来）
    - `retire()`：provider 改了答案（真退役了，destroy 也可以重试了）——收敛档用
    """

    def __init__(self, *, reports: str = "alive", destroy_raises: bool = True):
        super().__init__("http://127.0.0.1:8000")
        assert reports in REPORTS, reports
        self.reports = reports
        self.destroy_raises = destroy_raises
        self.destroy_calls = 0
        self.rotations = 0

    def rotate_credentials(self, workspace, credentials):
        self.rotations += 1
        return False

    def destroy(self, workspace):
        self.destroy_calls += 1
        if self.destroy_raises:
            raise RuntimeError("simulated: runtime destroy failed")
        # 真把 runtime 退役了才改口：一次成功的 destroy 不是"事实"，provider 的回话才是。
        if self.reports == "alive":
            self.reports = "missing"
        return None

    def reconcile(self, workspace: Workspace) -> RuntimeState:
        return REPORTS[self.reports]

    def retire(self) -> None:
        """provider 改答案：runtime 其实已经没了，destroy 也不再抛。"""
        self.destroy_raises = False
        self.reports = "missing"


def _make_manager(provider: ClaimAbortProvider) -> WarmPoolManager:
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-warm-claim-admission")  # noqa: S108 测试隔离目录
    )
    settings = SimpleNamespace(warm_pool_enabled=True, warm_pool_size=1, warm_pool_reserve_slots=0)
    return WarmPoolManager(Factory, orchestrator, settings)


def _drain(orchestrator) -> None:
    """驱动 worker 跑完 maintain 异步入队的 PROVISION operation。"""
    worker = OperationWorker(Factory, orchestrator)
    for _ in range(20):
        if worker.tick_once() == 0:
            return
    raise AssertionError("operations did not drain")


def _effects(db, workspace_id: str) -> dict:
    """从权威表读"卡到底放没放"：分配行、卡状态、卡的归属。"""
    gpu = db.scalar(select(Gpu))
    assert gpu is not None, "库里没有卡，判据无从判起"
    return {
        "alloc": db.scalar(
            select(func.count(GpuAllocation.id)).where(GpuAllocation.workspace_id == workspace_id)
        )
        == 1,
        "gpu_free": gpu.status == GpuStatus.AVAILABLE.value,
        "gpu_holder": gpu.workspace_id == workspace_id,
    }


HELD = {"alloc": True, "gpu_free": False, "gpu_holder": True}
RELEASED = {"alloc": False, "gpu_free": True, "gpu_holder": False}


def _warm_pool(db, manager) -> Workspace:
    """maintain → 异步 provision（worker 驱动）→ 再 maintain 收割 → READY warm runtime。"""
    manager.maintain(db)
    ws_id = db.scalars(select(Workspace.id)).one()
    _drain(manager.orchestrator)
    with Factory() as reap_db:
        manager.maintain(reap_db)
    db.rollback()  # 结束本会话事务，避免读到 drain/reap 之前的旧快照
    ws = db.get(Workspace, ws_id)
    assert ws is not None
    assert ws.warm_pool_state == WarmPoolState.READY.value
    # 撤销档要保护的东西必须先真实存在：非终态状态、可寻址的容器、占着的卡
    assert ws.status == WorkspaceStatus.RUNNING.value
    assert ws.user_id is None
    assert ws.container_name, ws.container_name
    assert _effects(db, ws.id) == HELD
    return ws


def _abort_claim(provider: ClaimAbortProvider):
    """建池 → claim（rotate 恒失败）→ 返回 (manager, workspace_id, 撤销前的 container_name)。"""
    manager = _make_manager(provider)
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        _make_user(db, "user-1")
        ws = _warm_pool(db, manager)
        wid, container = ws.id, ws.container_name
        delivered = manager.claim(
            db, "cartpole", db.get(User, "user-1"), credential_cipher=CIPHER
        )
        assert delivered is None, "轮换失败的 runtime 不得交付给用户"
    assert provider.rotations == 1, "撤销必须是被一次真的轮换尝试失败触发的"
    assert provider.destroy_calls == 1, f"claim 撤销应当只叫一次 provider.destroy：{provider.destroy_calls}"
    return manager, wid, container


# ---------------------------------------------------------------------------
# 必须开火档：destroy 报错 + provider 没说 runtime 没了 ⇒ 不放卡、不清 container_name、不写终态
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("reports", ["alive", "unknown"])
def test_claim_abort_keeps_the_card_until_the_provider_confirms_retirement(reports: str):
    """不放行档（provider 说 ALIVE / UNKNOWN）：卡留在原位、容器仍可寻址、状态非终态。

    `reports="alive"` 是"容器还在吃 --gpus device=N"那一极；`reports="unknown"` 是
    "provider 压根答不上来"那一极 —— 判据（app/services/orchestrator.py:400-403）在报错档
    只认 MISSING，所以两极都不许放卡。
    """
    provider = ClaimAbortProvider(reports=reports, destroy_raises=True)
    _manager, wid, container = _abort_claim(provider)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        # 1) 卡没回池：分配行还在、卡不是 AVAILABLE、卡的归属仍指向这一格
        assert _effects(db, wid) == HELD, (
            f"destroy 抛错、provider 说的是 {reports}，卡却被放回池子里了（一卡双跑）"
        )
        # 2) 退出池，但没有终态
        assert ws.warm_pool_state == WarmPoolState.DRAINING.value
        assert ws.status == WorkspaceStatus.RUNNING.value, (
            "撤销档写了终态：`recover_stuck_gpu_allocations` 会把它当孤儿把卡强制放掉"
        )
        assert ws.status not in {
            WorkspaceStatus.FAILED.value,
            WorkspaceStatus.STOPPED.value,
            WorkspaceStatus.DELETED.value,
        }
        # 3) 容器仍可寻址（container_name 是"这一格对应哪个容器"的事实本体，
        #    清了就只剩按约定名推断 —— N-78 之前更重：stop 只读这一列，清列等于停不掉）
        assert ws.container_name == container, "provider 还没认账就把容器句柄清了"
        # 4) 归属与凭据照样收回（这一半与放行档相同，旧密码不许滞留）
        assert ws.user_id is None
        assert ws.organization_id is None
        assert ws.password is None
        assert ws.ide_url is None
        # 5) 原因写清：触发点 + 不放行的理由（含 provider 自己的回话与 destroy 的错）
        msg = ws.error_message or ""
        assert "credential rotation" in msg, msg
        assert "GPU not released" in msg, msg
        assert f"runtime {reports}" in msg, msg
        assert "simulated: runtime destroy failed" in msg, msg

    # 6) 那把"强制放孤儿卡"的尺子真在场，而且这一格因为它保护得住：先证没有 active
    #    operation 兜底（保护只能来自状态本身），再真调一次。
    with Factory() as db:
        active = db.scalar(
            select(func.count(WorkspaceOperation.id)).where(
                WorkspaceOperation.workspace_id == wid,
                WorkspaceOperation.status.in_(
                    [
                        OperationStatus.PENDING.value,
                        OperationStatus.RUNNING.value,
                        OperationStatus.RETRYING.value,
                    ]
                ),
            )
        )
        assert active == 0, f"前提塌了：还有 {active} 个 active operation 在替这张卡挡收割"
        recover_stuck_gpu_allocations(db)
    with Factory() as db:
        assert _effects(db, wid) == HELD, "非终态没被 recover_stuck_gpu_allocations 保护"

        # 反证（同一行、同一把尺子）：只把状态改成 FAILED ⇒ 卡立刻被放掉。
        # 没有这一支，上一条"卡还在"可能只是那把尺子根本不干活。
        row = db.get(Workspace, wid)
        row.status = WorkspaceStatus.FAILED.value
        db.commit()
    with Factory() as db:
        recover_stuck_gpu_allocations(db)
    with Factory() as db:
        assert _effects(db, wid) == RELEASED, "终态行没被放卡 ⇒ 上一条反证不成立，尺子是假的"


# ---------------------------------------------------------------------------
# 不得误伤档：provider 认账退役 ⇒ 今天的全部行为保持（改前改后都必须绿）
# ---------------------------------------------------------------------------
def test_claim_abort_still_releases_when_the_provider_says_the_runtime_is_gone():
    """合规档（destroy 报错 + provider 亲口说 MISSING）：放卡 + FAILED。

    K8s 对已删 Deployment 的 404 走这一极。不放行就是"把 GPU 永久钉死在一张已经没有
    使用者的卡上"，所以这一极是必须保住的既有行为，不是要修的东西。
    """
    provider = ClaimAbortProvider(reports="missing", destroy_raises=True)
    _manager, wid, _container = _abort_claim(provider)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        assert _effects(db, wid) == RELEASED
        assert ws.warm_pool_state == WarmPoolState.DRAINING.value
        assert ws.status == WorkspaceStatus.FAILED.value
        assert ws.container_name is None
        assert ws.ide_port is None and ws.signal_port is None and ws.media_port is None
        assert ws.user_id is None and ws.password is None


def test_claim_abort_releases_when_the_destroy_command_succeeded():
    """不得误伤档 B：destroy 命令成功 ⇒ 与改前一样放卡 + FAILED（改前改后都必须绿）。"""
    provider = ClaimAbortProvider(reports="missing", destroy_raises=False)
    _manager, wid, _container = _abort_claim(provider)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        assert _effects(db, wid) == RELEASED
        assert ws.status == WorkspaceStatus.FAILED.value
        assert ws.container_name is None


def test_claim_abort_releases_the_mock_demo_slot_where_nothing_is_observable():
    """命令成功 + provider 只能说 UNKNOWN（mock 演示档）⇒ 照样放卡。

    这一支钉的是判据自己的 UNKNOWN 语义（app/services/orchestrator.py:388-390「演示路径
    必须停得下来」）：如果撤销档把 `command_succeeded` 一律写死成 False，mock 的 UNKNOWN
    就永远不放行，卡会被一直钉住 —— 那既不是设计意图，也会把
    tests/test_warmpool_claim.py 里两支既有断言翻掉（见该文件
    `test_warm_pool_rotation_failure_releases_gpu_only_when_admitted`）。
    """
    provider = ClaimAbortProvider(reports="unknown", destroy_raises=False)
    _manager, wid, _container = _abort_claim(provider)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        assert _effects(db, wid) == RELEASED
        assert ws.status == WorkspaceStatus.FAILED.value


# ---------------------------------------------------------------------------
# 收敛：DRAINING 必须再次被扫到，并且 provider 改答案之后真的把卡放掉
# ---------------------------------------------------------------------------
def test_a_draining_slot_is_swept_and_its_runtime_is_re_tried():
    """扫池子这一半单独判（与 claim 的修法解耦）：DRAINING 行必须被入队 DESTROY 并真叫 destroy。

    这一支不需要 claim 撤销也成立 —— 它钉的就是 `_cleanup_failed` 的选择集：改前只读
    FAILED，DRAINING 行连一次补偿 DESTROY 都等不到（读数 `stats["destroyed"] == 0`）。
    """
    provider = ClaimAbortProvider(reports="alive", destroy_raises=True)
    manager = _make_manager(provider)
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        ws = _warm_pool(db, manager)
        wid = ws.id
        # 手动摆出"不放行档撤销之后留下的那一格"：退出池、非终态、卡还占着、容器还能寻址
        ws.warm_pool_state = WarmPoolState.DRAINING.value
        db.commit()
    assert provider.destroy_calls == 0, "前提：这一支不该在 claim 里就叫过 destroy"

    provider.retire()  # provider 改答案
    manager.settings.warm_pool_size = 0  # 本轮不补位，免得新格子的 PROVISION 抢走刚回池的卡
    with Factory() as db:
        stats = manager.maintain(db)
    assert stats["destroyed"] >= 1, f"DRAINING 不在扫池子的选择集里（改前形状）：{stats}"

    worker = OperationWorker(Factory, manager.orchestrator)
    for _ in range(10):
        if worker.tick_once() == 0:
            break
    assert provider.destroy_calls >= 1, "DESTROY 入队了却没真再叫 provider.destroy（只留痕不收敛）"
    with Factory() as db:
        assert _effects(db, wid) == RELEASED
        assert db.get(Workspace, wid).status == WorkspaceStatus.DELETED.value


def test_draining_claim_abort_is_swept_and_converges_once_the_provider_changes_its_answer():
    """不放行档留下的 DRAINING 行：`_cleanup_failed` 再入队 DESTROY → 真再叫一次 destroy → 收敛。

    改前三重失明叠在一起：状态是 DRAINING（选择集只读 FAILED）→ 不入队；卡已经被 claim
    放掉了 → 没有任何东西负责"把 runtime 真正退役"这件事；docstring 承诺的
    `READY/其他 → DRAINING（退出池）→ FAILED` 那一跳因此没有驱动者。
    这一支只断"重试真的发生并收敛"，不断"日志里有一条 warning"——留痕不等于收敛。
    """
    provider = ClaimAbortProvider(reports="alive", destroy_raises=True)
    manager, wid, container = _abort_claim(provider)

    with Factory() as db:
        assert _effects(db, wid) == HELD, "前提塌了：撤销时卡已经放了，收敛档无从谈起"

    # provider 改答案（runtime 其实已经没了，destroy 也可以重试了）
    provider.retire()
    # 本轮不再补位：免得新格子的 PROVISION 抢走刚放回池的卡，把"卡回没回池"的读数搅浑
    manager.settings.warm_pool_size = 0
    with Factory() as db:
        stats = manager.maintain(db)
    assert stats["destroyed"] >= 1, f"DRAINING 行没被扫到（改前读数就是 0）：{stats}"

    worker = OperationWorker(Factory, manager.orchestrator)
    processed = 0
    for _ in range(10):
        n = worker.tick_once()
        if n == 0:
            break
        processed += n
    assert processed >= 1, "DESTROY operation 一次都没被执行"

    # 承重读数：provider.destroy 被**再叫了一次**（不是"没报错"，也不是只写了日志）
    assert provider.destroy_calls >= 2, f"重试没有真叫 provider.destroy：{provider.destroy_calls}"
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws is not None
        assert _effects(db, wid) == RELEASED, "provider 已认账退役，卡却没回池"
        assert ws.status == WorkspaceStatus.DELETED.value, f"收敛没到终态：{ws.status}"
        assert ws.deleted_at is not None
        assert ws.warm_pool_state == WarmPoolState.DRAINING.value
        assert container, "前提塌了：撤销时就没有 container_name 可留"


# ---------------------------------------------------------------------------
# 文本面判据 1：claim() 里的 release 调用必须坐在"以准入判据为条件"的分支里
# ---------------------------------------------------------------------------
def claim_release_admission_shape(source: str) -> dict[str, int]:
    """AST 读 claim() 里 `scheduler.release` 与 `_release_admitted` 的接线形状。

    返回 `{"claim": claim 定义数, "judgments": 判据调用数, "releases": release 调用数,
    "guarded_releases": 落在"测试表达式里调了判据"的 if 分支内的 release 数}`。

    为什么必须按 AST 判：release 这个调用存在于 claim() 里不证明任何事 —— 它可以在判据
    之外；反过来把分支条件换成 `if True:` 之后，release 与判据**都还在**，按名字数数的
    尺子会照样点头（本轮的合成反向对照就是为了抓这个形状）。
    """
    tree = ast.parse(source)
    claim = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "claim"),
        None,
    )
    if claim is None:
        return {"claim": 0, "judgments": 0, "releases": 0, "guarded_releases": 0}

    def calls_judgment(node: ast.AST) -> bool:
        return any(
            isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "_release_admitted"
            for c in ast.walk(node)
        )

    def is_gpu_release(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "release"
        )

    releases = [n for n in ast.walk(claim) if is_gpu_release(n)]
    guarded: list[ast.Call] = []

    def visit(node: ast.AST, guards: tuple[ast.If, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call) and is_gpu_release(child) and any(
                calls_judgment(g.test) for g in guards
            ):
                guarded.append(child)
            next_guards = (*guards, child) if isinstance(child, ast.If) else guards
            visit(child, next_guards)

    visit(claim, ())
    return {
        "claim": 1,
        "judgments": sum(1 for n in ast.walk(claim) if isinstance(n, ast.Call) and calls_judgment(n)),
        "releases": len(releases),
        "guarded_releases": len(guarded),
    }


# 合成反向对照：release 在 claim() 里**存在**、判据也**被调用**，但放卡在判据之外。
# 这一份源码是编的（仓里没有这个形状），存在的唯一目的是证明上面那把尺子会翻脸。
UNGUARDED_CLAIM_SOURCE = '''
class WarmPoolManager:
    def claim(self, db, template_id, user, credential_cipher=None):
        rotated = self.orchestrator.provider.rotate_credentials(workspace, {})
        if not rotated:
            self.orchestrator._release_admitted(workspace, command_succeeded=False)
            self.orchestrator.scheduler.release(db, workspace.id)
            workspace.status = "failed"
            db.commit()
            return None
'''


def test_the_gpu_release_in_the_abort_path_sits_behind_the_admission_judgment():
    """真实源码：claim() 里的 release 恰好一处，且它在判据为真的那一支里。"""
    shape = claim_release_admission_shape(WARMPOOL_SOURCE)
    assert shape == {
        "claim": 1,
        "judgments": 1,
        "releases": 1,
        "guarded_releases": 1,
    }, shape


def test_the_release_guard_names_an_unguarded_release():
    """反向对照一（合成源码 `UNGUARDED_CLAIM_SOURCE`）：判据被算了但没当分支条件 ⇒ 必须报 0。"""
    shape = claim_release_admission_shape(UNGUARDED_CLAIM_SOURCE)
    assert shape["releases"] == 1, "合成夹具本身没造出 release 调用，等于没测"
    assert shape["judgments"] == 1, "合成夹具没保留判据调用，就抓不到「算了判决但不消费」"
    assert shape["guarded_releases"] == 0, f"尺子不开火：{shape}"


def test_the_release_guard_fires_on_a_real_source_mutation():
    """反向对照二（真源码变异）：把 if 的条件换成恒真，判据与 release 都在，接线却断了。"""
    anchor = (
        "            if self.orchestrator._release_admitted(\n"
        "                workspace, command_succeeded=destroy_error is None\n"
        "            ):\n"
    )
    assert WARMPOOL_SOURCE.count(anchor) == 1, f"锚点不是恰好一处（{WARMPOOL_SOURCE.count(anchor)}）"
    mutant = WARMPOOL_SOURCE.replace(
        anchor,
        "            self.orchestrator._release_admitted(\n"
        "                workspace, command_succeeded=destroy_error is None\n"
        "            )\n"
        "            if True:  # 变异：判据被算了，却没被用来决定放不放卡\n",
    )
    assert claim_release_admission_shape(mutant)["guarded_releases"] == 0, "变异体没被抓到"
    # 另一极：同一把尺子跑真实源码不开火
    assert claim_release_admission_shape(WARMPOOL_SOURCE)["guarded_releases"] == 1


# ---------------------------------------------------------------------------
# 文本面判据 2：扫池子的那句 SELECT 必须真的把 DRAINING 读进选择集
# ---------------------------------------------------------------------------
def warm_state_selection(source: str, func_name: str) -> set[str]:
    """AST 读「某个函数体里对 `Workspace.warm_pool_state` 的谓词」涉及哪些 WarmPoolState 成员。

    `==`/`!=`/`in` 比较与 `.in_([...])` 两种写法都算；只数**谓词位置**上的成员 ——
    赋值位（`workspace.warm_pool_state = WarmPoolState.DRAINING.value`）不计，
    否则"写了 DRAINING"会被读成"扫了 DRAINING"。
    """
    tree = ast.parse(source)
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func_name),
        None,
    )
    assert fn is not None, f"函数 {func_name} 不存在，选择集无从判起"

    def is_pool_state(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "warm_pool_state"
            and isinstance(node.value, ast.Name)
            and node.value.id == "Workspace"
        )

    def members(node: ast.AST) -> set[str]:
        return {
            n.attr
            for n in ast.walk(node)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id == "WarmPoolState"
        }

    found: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare) and is_pool_state(node.left):
            for comp in node.comparators:
                found |= members(comp)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"in_", "not_in"}
            and is_pool_state(node.func.value)
        ):
            for arg in node.args:
                found |= members(arg)
    return found


def test_the_pool_sweeper_selects_draining_rows_as_well_as_failed_ones():
    """`_cleanup_failed` 的选择集必须同时读 FAILED 与 DRAINING，且它真被 maintain 驱动。"""
    selected = warm_state_selection(WARMPOOL_SOURCE, "_cleanup_failed")
    assert {"FAILED", "DRAINING"} <= selected, f"DRAINING 不在扫池子的选择集里：{sorted(selected)}"
    # 驱动位：不是"函数存在就算"，maintain 必须真的叫它（否则 DRAINING 照样没人扫）
    maintain = next(
        n for n in ast.walk(ast.parse(WARMPOOL_SOURCE))
        if isinstance(n, ast.FunctionDef) and n.name == "maintain"
    )
    drivers = sum(
        1
        for c in ast.walk(maintain)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and c.func.attr == "_cleanup_failed"
    )
    assert drivers == 1, f"maintain 没有驱动 _cleanup_failed：{drivers}"


def test_the_sweeper_selector_checker_can_report_the_failed_only_shape():
    """非开火对照：把选择集改回"只读 FAILED"（改前形状），尺子必须只报出 FAILED。"""
    anchor = (
        "                Workspace.warm_pool_state.in_(\n"
        "                    [WarmPoolState.FAILED.value, WarmPoolState.DRAINING.value]\n"
        "                ),\n"
    )
    assert WARMPOOL_SOURCE.count(anchor) == 1, f"锚点不是恰好一处（{WARMPOOL_SOURCE.count(anchor)}）"
    mutant = WARMPOOL_SOURCE.replace(
        anchor,
        "                Workspace.warm_pool_state == WarmPoolState.FAILED.value,\n",
    )
    assert warm_state_selection(mutant, "_cleanup_failed") == {"FAILED"}, "变异体没回到改前形状"
    assert warm_state_selection(WARMPOOL_SOURCE, "_cleanup_failed") == {"FAILED", "DRAINING"}
    # 另一面（防"把赋值位也数进来"的假阳）：`_reap_prewarming` 里**写入** READY/FAILED、
    # 只**谓词** PREWARMING。读出来必须只有 PREWARMING，否则"写了 DRAINING"会被读成"扫了 DRAINING"。
    assert warm_state_selection(WARMPOOL_SOURCE, "_reap_prewarming") == {"PREWARMING"}

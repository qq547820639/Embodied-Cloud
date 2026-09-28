"""`DRAINING` 与 `DRAINED` 是两个判决，不是同一件事的两种写法（N-123，闭 N-114 的 (b) 半边）。

改前的形状（逐条对码，一手行号）：

- 自动判决与人工判决共用一个值：`sync_host` 的缺席降级写 `GpuStatus.DRAINING`，管理员的
  `POST /api/gpus/{id}/drain` 经 `mark_draining` 也写它。
- 于是没人敢实现归位：`sync_host` 对重新上报的卡只更新 model/memory/index，不改 status
  ⇒ 一次抖动缺席就等于永久掉容量。N-112 当时把这一点写在"不改状态列"的理由里
  （「管理员的 DRAINING/UNHEALTHY 有各自的生命周期」，`tests/test_gpu_allocation_visibility.py`）。
- `mark_draining` 的前置是 `status == AVAILABLE`：一张已经被自动降级的卡，管理员下的判决
  静默 no-op，路由照样回 204 —— 「没报错即成功」那一族（N-109／N-113 同族）。
- `release` 与 `recover_stuck_gpu_allocations` 无条件把带绑定的卡写成 AVAILABLE：
  管理员先 `/unhealthy` 一张占用中的卡，工作区一停，判决就被自动路径抹掉、卡回池子。

改法借 SLURM 的三分法（`sinfo`：DRAIN 是“per system administrator request”，DOWN 是
“Slurm can automatically place nodes in this state if some failure occurs”，`*` 是不响应），
把"谁下的判决"提到值本身：`DRAINED` 只由管理员写、只有管理员能解除；`DRAINING` 从此只表示
"上一次上报里没有它"，这一次有了就归位。占用中的卡不参与（`allocated` 是本仓的占用权威，
`app/main.py:36` 的 stuck 保护读它），所以 `/drain` 撞上占用的卡改为 409 并点名当前状态。

失联前提一律由真实生产者（`sync_host` 少报）制造，不手写 `UPDATE gpus SET status=…`；
库用 tmp_path 下的文件库：判的是"另一个会话读得到"，不是同一连接里的脏读。
"""

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import deps
from app.db import Base
from app.deps import SessionFactory, scheduler
from app.main import app
from app.models import Gpu, GpuStatus, Role, User, Workspace, WorkspaceStatus
from app.services.scheduler import GpuInfo, GpuScheduler
from tests.gpu_pool import ensure_free_gpus
from tests.test_demo_workspace import _auth, _register
from tests.workspace_progress import wait_status

DEPS_SRC = Path(deps.__file__).resolve()
SCHEDULER_SRC = DEPS_SRC.parent / "services" / "scheduler.py"
APP_JS = DEPS_SRC.parent / "static" / "app.js"
STYLES_CSS = DEPS_SRC.parent / "static" / "styles.css"


@pytest.fixture()
def factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'drain.db'}", future=True)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, future=True)
    engine.dispose()


def _sync(sf, gpus: tuple[str, ...], host_id: str = "h1") -> None:
    """真实生产者：节点"这次上报这几张卡"。缺席与归位两个方向都由它制造。"""
    with sf() as db:
        GpuScheduler(sf).sync_host(
            db,
            host_id=host_id,
            name=f"name-{host_id}",
            address="10.0.0.1",
            provider="docker",
            gpus=[
                GpuInfo(gpu_uuid=u, model="m", memory_total=24564, index=i)
                for i, u in enumerate(gpus)
            ],
        )


def _row(sf, uuid: str) -> tuple[str, str | None]:
    """从另一个会话读 `(status, workspace_id)`：只看得见已提交的事实。"""
    with sf() as db:
        gpu = db.scalar(select(Gpu).where(Gpu.gpu_uuid == uuid))
        assert gpu is not None, f"清单里根本没有 {uuid}，本档前提没造出来"
        return gpu.status, gpu.workspace_id


def _hold(sf, workspace_id: str) -> Gpu:
    """占一张卡：先造真的 workspace 行，再走分配器（占用事实由生产代码造，不手写列值）。"""
    with sf() as db:
        db.add(
            Workspace(
                id=workspace_id,
                name=workspace_id,
                template_id="cartpole",
                provider="mock",
                status=WorkspaceStatus.QUEUED.value,
            )
        )
        db.commit()
        return GpuScheduler(sf).allocate(db, workspace_id)


# ---------------------------------------------------------------------------
# 行为面：机器判决可逆
# ---------------------------------------------------------------------------
def test_an_absent_card_comes_back_when_the_node_reports_it_again(factory) -> None:
    """少报 ⇒ DRAINING；同一张卡重新出现在成功上报里 ⇒ 回 AVAILABLE，全程无人工介入。"""
    _sync(factory, ("gpu-a", "gpu-b"))
    _sync(factory, ("gpu-a",))
    assert _row(factory, "gpu-b")[0] == GpuStatus.DRAINING.value, "缺席降级本身没生效，归位档就没有前提"

    _sync(factory, ("gpu-a", "gpu-b"))
    assert _row(factory, "gpu-b")[0] == GpuStatus.AVAILABLE.value, (
        "一次抖动缺席就永久掉容量 —— 这正是 N-114 (b) 那句话的字面意思"
    )


def test_the_restored_card_is_actually_allocatable(factory) -> None:
    """归位不是把列改好看：分配器必须真能把这张卡派出去。"""
    _sync(factory, ("gpu-a", "gpu-b"))
    _sync(factory, ("gpu-a",))  # gpu-b 被机器判成缺席
    _sync(factory, ("gpu-a", "gpu-b"))

    first = _hold(factory, "ws-1")
    second = _hold(factory, "ws-2")
    assert {first.gpu_uuid, second.gpu_uuid} == {"gpu-a", "gpu-b"}, (
        f"两张里有一张开不出去：{first.gpu_uuid}／{second.gpu_uuid}"
    )


def test_a_card_that_never_went_missing_is_untouched(factory) -> None:
    """不开火对照：一直在清单里的卡不该被"归位"这段逻辑碰。"""
    _sync(factory, ("gpu-a", "gpu-b"))
    _sync(factory, ("gpu-a", "gpu-b"))
    assert (_row(factory, "gpu-a")[0], _row(factory, "gpu-b")[0]) == (
        GpuStatus.AVAILABLE.value,
        GpuStatus.AVAILABLE.value,
    )


# ---------------------------------------------------------------------------
# 行为面：管理员判决不可逆
# ---------------------------------------------------------------------------
def test_an_admin_drain_survives_absence_and_reappearance(factory) -> None:
    """`DRAINED` 是人的判决：缺席不覆盖它，重报也不放它回池子。"""
    _sync(factory, ("gpu-a", "gpu-b"))
    with factory() as db:
        gpu = db.scalar(select(Gpu).where(Gpu.gpu_uuid == "gpu-b"))
        assert GpuScheduler(factory).mark_drained(db, gpu.id) is True
    assert _row(factory, "gpu-b")[0] == GpuStatus.DRAINED.value

    _sync(factory, ("gpu-a",))
    assert _row(factory, "gpu-b")[0] == GpuStatus.DRAINED.value, "缺席降级把人工判决顶掉了"
    _sync(factory, ("gpu-a", "gpu-b"))
    assert _row(factory, "gpu-b")[0] == GpuStatus.DRAINED.value, (
        "重报把手工 drain 抬回池子 ⇒ 管理员判决活不过两分钟（N-112 当年的顾虑）"
    )


def test_the_admin_verdict_escalates_an_auto_drain_and_then_stops_returning(factory) -> None:
    """已经被自动降级的卡，管理员随后下的判决必须落地（改前这里是 no-op ＋ 204）。"""
    _sync(factory, ("gpu-a", "gpu-b"))
    _sync(factory, ("gpu-a",))
    assert _row(factory, "gpu-b")[0] == GpuStatus.DRAINING.value

    with factory() as db:
        gpu = db.scalar(select(Gpu).where(Gpu.gpu_uuid == "gpu-b"))
        assert GpuScheduler(factory).mark_drained(db, gpu.id) is True, (
            "改前的前置是 status == AVAILABLE ⇒ 这一档什么都不做"
        )
    assert _row(factory, "gpu-b")[0] == GpuStatus.DRAINED.value

    _sync(factory, ("gpu-a", "gpu-b"))
    assert _row(factory, "gpu-b")[0] == GpuStatus.DRAINED.value


def test_an_allocated_card_is_neither_demoted_nor_restored(factory) -> None:
    """占用中的卡两边都不动：缺席不许抹掉占用事实，重报也不许把它抬回池子。"""
    _sync(factory, ("gpu-a", "gpu-b"))
    held = _hold(factory, "ws-held")
    busy = held.gpu_uuid

    _sync(factory, ())  # 整个节点一张卡都不报
    assert _row(factory, busy) == (GpuStatus.ALLOCATED.value, "ws-held"), (
        "占用事实被抹成 draining＝两张表互相打脸"
    )
    _sync(factory, ("gpu-a", "gpu-b"))
    assert _row(factory, busy) == (GpuStatus.ALLOCATED.value, "ws-held")


def test_an_unhealthy_card_is_never_auto_restored(factory) -> None:
    """UNHEALTHY 也是判决：重报不许把它抬回池子（G0.103 那条"不许改写"的镜像面）。"""
    _sync(factory, ("gpu-a", "gpu-b"))
    with factory() as db:
        gpu = db.scalar(select(Gpu).where(Gpu.gpu_uuid == "gpu-b"))
        GpuScheduler(factory).mark_unhealthy(db, gpu.id)

    _sync(factory, ("gpu-a",))
    assert _row(factory, "gpu-b")[0] == GpuStatus.UNHEALTHY.value
    _sync(factory, ("gpu-a", "gpu-b"))
    assert _row(factory, "gpu-b")[0] == GpuStatus.UNHEALTHY.value


def test_releasing_a_quarantined_card_clears_the_binding_but_keeps_the_verdict(factory) -> None:
    """放卡那一脚只该结束占用，不该替管理员撤判决。

    形状：管理员先给一张占用中的卡判 unhealthy（`mark_unhealthy` 没有状态前置，这是它
    本来就支持的用法），随后工作区停止走 `release`。改前 `release` 无条件写 AVAILABLE，
    于是"这张卡被隔离了"这件事随停止一起消失；现在绑定清空、判决留下。
    """
    _sync(factory, ("gpu-a", "gpu-b"))
    held = _hold(factory, "ws-quad")
    with factory() as db:
        GpuScheduler(factory).mark_unhealthy(db, held.id)
    assert _row(factory, held.gpu_uuid) == (GpuStatus.UNHEALTHY.value, "ws-quad")

    with factory() as db:
        GpuScheduler(factory).release(db, "ws-quad")

    status, bound = _row(factory, held.gpu_uuid)
    assert bound is None, "释放没清绑定（N-82 那一族）"
    assert status == GpuStatus.UNHEALTHY.value, (
        f"自动路径把管理员判决抹回 available，卡会被重新派出去：{status}"
    )


# ---------------------------------------------------------------------------
# HTTP 面：204 必须意味着"这件事现在为真"
# ---------------------------------------------------------------------------
def _promote(email: str) -> None:
    with SessionFactory() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = Role.ADMIN.value
        db.commit()


def _pool_card() -> str:
    with SessionFactory() as db:
        ensure_free_gpus(db, scheduler)
        gpu = db.scalar(select(Gpu).where(Gpu.status == GpuStatus.AVAILABLE.value))
        assert gpu is not None, "mock 池子里一张空闲卡都没有，本档前提没造出来"
        return gpu.id


def _card(gpu_id: str) -> tuple[str, str | None]:
    with SessionFactory() as db:
        gpu = db.get(Gpu, gpu_id)
        return gpu.status, gpu.workspace_id


def _unwrite(gpu_id: str) -> None:
    """把测试借走的那张卡还给共享池：DRAINED 不会被任何自动路径抬回来，不收就会永久少一张。"""
    with SessionFactory() as db:
        gpu = db.get(Gpu, gpu_id)
        gpu.status = GpuStatus.AVAILABLE.value
        db.commit()


def test_the_drain_endpoint_lands_the_verdict_and_is_idempotent() -> None:
    """第一次 204 ⇒ 状态确实是 drained（读端也这么吐）；重复 204 ⇒ 仍是 drained。"""
    with TestClient(app) as client:
        token = _register(client, "n123-drain@example.com", "n123-drain")
        _promote("n123-drain@example.com")
        gid = _pool_card()
        try:
            assert client.post(f"/api/gpus/{gid}/drain", headers=_auth(token)).status_code == 204
            assert _card(gid)[0] == GpuStatus.DRAINED.value
            listed = {g["id"]: g for g in client.get("/api/gpus", headers=_auth(token)).json()}
            assert listed[gid]["status"] == GpuStatus.DRAINED.value

            assert client.post(f"/api/gpus/{gid}/drain", headers=_auth(token)).status_code == 204
            assert _card(gid)[0] == GpuStatus.DRAINED.value, "幂等这一档也必须为真，不能只靠退码"
        finally:
            _unwrite(gid)


def test_the_drain_endpoint_refuses_a_busy_card_and_names_why() -> None:
    """占用中的卡：改前回 204 而什么都没发生；现在 409，并把当前状态说给客户。

    占用前提走真实产品路径（建工作区→auto_start→等 RUNNING），不手写 `status=allocated`：
    这张卡随后要经过真的 stop→release，才能同时证到"被拒的请求不改任何东西"与
    "正常放卡仍然回池"（`case` 的 WHEN 那一支）。
    """
    with TestClient(app) as client:
        token = _register(client, "n123-busy@example.com", "n123-busy")
        _promote("n123-busy@example.com")
        with SessionFactory() as db:
            ensure_free_gpus(db, scheduler)
        created = client.post(
            "/api/workspaces",
            json={"template_id": "cartpole", "auto_start": True},
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        wid = created.json()["id"]
        try:
            wait_status(client, token, wid, "running")
            gid = client.get(f"/api/workspaces/{wid}", headers=_auth(token)).json()["gpu_id"]
            assert gid, "RUNNING 的工作区必须绑一张卡"
            assert _card(gid) == (GpuStatus.ALLOCATED.value, wid)

            resp = client.post(f"/api/gpus/{gid}/drain", headers=_auth(token))
            assert resp.status_code == 409, resp.text
            assert GpuStatus.ALLOCATED.value in resp.text, f"409 要点名拒绝的理由：{resp.text}"
            assert _card(gid) == (GpuStatus.ALLOCATED.value, wid), "被拒绝的请求不该改任何东西"

            assert client.post(f"/api/workspaces/{wid}/stop", headers=_auth(token)).status_code == 200
            wait_status(client, token, wid, "stopped")
            assert _card(gid) == (GpuStatus.AVAILABLE.value, None), (
                "占用中的卡放卡后仍要回池：case 的 WHEN 那一支不能被改坏"
            )
        finally:
            with SessionFactory() as db:
                GpuScheduler(SessionFactory).release(db, wid)


# ---------------------------------------------------------------------------
# 结构面：provenance 登记册（谁有权写哪个判决）
# ---------------------------------------------------------------------------
def _defs(tree: ast.AST) -> list[ast.FunctionDef]:
    """模块里所有函数定义，**含类方法**（`GpuScheduler` 的方法不在模块顶层 body 里）。"""
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _iter_own(node: ast.AST):
    """本节点的子树，遇到嵌套 def/lambda 就停（那一层由它自己的档位负责），每个节点只给一次。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _iter_own(child)


def _own_nodes(fn: ast.FunctionDef):
    """本函数自己写的语句与表达式（`_iter_own` 的具名包装，读起来短一点）。"""
    return _iter_own(fn)


def _members_written(value: ast.expr) -> set[str]:
    """这条表达式会写出哪些 `GpuStatus` 成员：直接成员、`.value`、以及 `case(...)` 的分支值。

    枚举名判别：`WarmPoolState.DRAINING`／`WorkspaceStatus.STOPPED` 一律不进册（本仓三个枚举
    都有叫 draining／failed 的成员，N-122 的分母就栽过一次）。
    """
    found: set[str] = set()

    def one(node: ast.expr) -> None:
        target = node
        if isinstance(target, ast.Attribute) and target.attr == "value":
            target = target.value
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "GpuStatus"
        ):
            found.add(target.attr)

    one(value)
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "case"
    ):
        for arg in value.args:
            if isinstance(arg, ast.Tuple) and len(arg.elts) == 2:
                one(arg.elts[1])
        for kw in value.keywords:
            if kw.arg == "else_":
                one(kw.value)
    return found


def _writes_in(fn: ast.FunctionDef) -> set[str]:
    """函数体内所有"把某个 GpuStatus 成员写到 status 上"涉及的成员名。"""
    members: set[str] = set()
    for node in _own_nodes(fn):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Attribute) and t.attr == "status" for t in node.targets
        ):
            members |= _members_written(node.value)
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "status":
                    members |= _members_written(kw.value)
    return members


def status_writer_registry(source: str) -> dict[str, set[str]]:
    """`GpuStatus` 成员 → 会写它的函数名集合（这份源码里的全部写入者）。"""
    registry: dict[str, set[str]] = {}
    for fn in _defs(ast.parse(source)):
        for member in _writes_in(fn):
            registry.setdefault(member, set()).add(fn.name)
    return registry


def _guarded_writes(fn: ast.FunctionDef, written: str, guard: str) -> int:
    """本函数里"写在 `status == GpuStatus.<guard>` 判定之下"的 `<written>` 赋值有几处。"""
    guarded_tests: list[ast.If] = []
    for node in _own_nodes(fn):
        if not isinstance(node, ast.If):
            continue
        members: set[str] = set()
        for sub in ast.walk(node.test):
            members |= _members_written(sub)
        if guard in members:
            guarded_tests.append(node)
    hits = 0
    for node in _own_nodes(fn):
        if not (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == "status" for t in node.targets)
            and _members_written(node.value) == {written}
        ):
            continue
        if any(_encloses(parent, node) for parent in guarded_tests):
            hits += 1
    return hits


def _encloses(outer: ast.AST, inner: ast.AST) -> bool:
    return any(node is inner for node in ast.walk(outer))


def auto_restore_sites(source: str) -> int:
    """`sync_host` 里"排在 DRAINING 谓词之下、抬回 AVAILABLE"的赋值有几处。"""
    for fn in _defs(ast.parse(source)):
        if fn.name == "sync_host":
            return _guarded_writes(fn, "AVAILABLE", "DRAINING")
    raise AssertionError("源码里没有 sync_host，这把尺子读不到东西")


def scheduler_source() -> str:
    return SCHEDULER_SRC.read_text(encoding="utf-8")


def test_the_provenance_registry_names_exactly_one_writer_per_verdict():
    """`DRAINED` 只有管理员那一处、`DRAINING` 只有缺席降级那一处。

    多一个能写人工判决的自动路径，就是把 N-114 (b) 重新打开——这条判据是它的闸门。
    """
    registry = status_writer_registry(scheduler_source())
    assert registry == {
        "AVAILABLE": {"sync_host", "release", "recover_stuck_gpu_allocations"},
        "ALLOCATED": {"allocate"},
        "UNHEALTHY": {"mark_unhealthy"},
        "DRAINING": {"sync_host"},
        "DRAINED": {"mark_drained"},
    }, registry


def test_the_auto_restore_sits_under_the_machine_verdict_predicate():
    """归位必须排在 `status == DRAINING` 之下：没有谓词的"重报即回池"会把人工判决一起抬走。"""
    assert auto_restore_sites(scheduler_source()) == 1, (
        "sync_host 里那条归位找不到它的 DRAINING 谓词了（0＝没守卫或没了；≥2＝判据对象变宽，要重新定档）"
    )


CLEAN_SHAPE = """
def sync_host(db, host_id, gpus):
    for info in gpus:
        gpu = find(info)
        if gpu.status == GpuStatus.DRAINING.value:
            gpu.status = GpuStatus.AVAILABLE.value
"""

UNGUARDED_RESTORE = """
def sync_host(db, host_id, gpus):
    for info in gpus:
        gpu = find(info)
        gpu.status = GpuStatus.AVAILABLE.value
"""

WRONG_VERDICT_GUARD = """
def sync_host(db, host_id, gpus):
    for info in gpus:
        gpu = find(info)
        if gpu.status == GpuStatus.UNHEALTHY.value:
            gpu.status = GpuStatus.AVAILABLE.value
"""

ADMIN_VERDICT_FROM_AUTO_PATH = """
def sync_host(db, host_id, gpus):
    for info in gpus:
        gpu = find(info)
        gpu.status = GpuStatus.DRAINED.value
"""

CASE_BRANCH_WRITE = """
def release(db, workspace_id):
    db.execute(
        update(Gpu).where(Gpu.workspace_id == workspace_id).values(
            status=case(
                (Gpu.status == GpuStatus.ALLOCATED.value, GpuStatus.AVAILABLE.value),
                else_=Gpu.status,
            ),
            workspace_id=None,
        )
    )
"""

OTHER_ENUMS = """
def _claim(db, ws):
    ws.warm_pool_state = WarmPoolState.DRAINING.value
    ws.status = WorkspaceStatus.STOPPED.value
"""


def test_the_ruler_distinguishes_guarded_from_unguarded_restore():
    """这把尺子自己的四档：有守卫／没守卫／守卫不是 DRAINING／压根没有 sync_host。"""
    assert auto_restore_sites(CLEAN_SHAPE) == 1
    assert auto_restore_sites(UNGUARDED_RESTORE) == 0, "没谓词的归位＝把人工判决一起抬走"
    assert auto_restore_sites(WRONG_VERDICT_GUARD) == 0, "守卫写的是别的判决，不算守住"
    with pytest.raises(AssertionError):
        auto_restore_sites(OTHER_ENUMS)


def test_the_registry_sees_case_branches_and_ignores_other_enums():
    """判别力两向：`case` 分支里的 AVAILABLE 要进册（release 的真实形状），别的枚举不许进。"""
    assert status_writer_registry(CASE_BRANCH_WRITE) == {"AVAILABLE": {"release"}}
    assert status_writer_registry(OTHER_ENUMS) == {}, "WarmPoolState／WorkspaceStatus 不是 GpuStatus"
    assert status_writer_registry(ADMIN_VERDICT_FROM_AUTO_PATH) == {"DRAINED": {"sync_host"}}, (
        "自动路径写人工判决必须被登记册点名（它不在期望表里，所以主档会红）"
    )


# ---------------------------------------------------------------------------
# 前端面：状态值是给人读的
# ---------------------------------------------------------------------------
def _labelled_gpu_statuses(js: str) -> set[str]:
    """从 `GPU_STATUS_CN = { … }` 取被标签化的状态值（取不到表本身就要红，不许读成空集通过）。"""
    block = re.search(r"GPU_STATUS_CN\s*=\s*\{(.*?)\}", js, re.S)
    assert block, "app.js 里读不到 GPU_STATUS_CN 这张表，这条判据就没有分母"
    keys = {m.group(1) for m in re.finditer(r"([A-Za-z_][\w-]*)\s*:", block.group(1))}
    assert keys, "表体解析出来是空的——提取式坏了，不是真的一个标签都没有"
    return keys


def test_the_admin_ui_labels_every_gpu_status():
    """`GpuStatus` 每个成员都要有中文名：新增成员忘了配标签会红，而不是显示裸字符串。

    分母取自模型自己（`GpuStatus`），不是手抄的名单；`declared` 非空是这条判据的前提
    （`app/static/app.js` 的渲染式是 `GPU_STATUS_CN[g.status] || g.status`，少一个标签
    不会报错，只会把英文原值印给运维）。徽章那一半只钉本轮新增的人工判决：
    `available`／`allocated` 走的是 `.badge` 基础样式，没有专门规则，这不是缺陷。
    """
    declared = {member.value for member in GpuStatus}
    assert declared, "枚举为空，这条判据就是恒真"
    labelled = _labelled_gpu_statuses(APP_JS.read_text(encoding="utf-8"))
    assert labelled >= declared, f"前端少了这些状态的中文名：{sorted(declared - labelled)}"
    css = STYLES_CSS.read_text(encoding="utf-8")
    assert ".badge.drained" in css, (
        "人工下架的卡没有徽章样式，表格里它与未知状态长得一样——运维分不出哪张是判决"
    )


def test_the_inventory_sweep_is_still_driven_periodically() -> None:
    """归位这段收敛只在真有人重报时才发生：周期驱动者必须还在（N-110 的那根线）。

    没有这一档，"重报即归位"可以只存在于函数里，而进程活着时永远等不到一次重报。
    """
    source = DEPS_SRC.read_text(encoding="utf-8")
    assert "bootstrap_gpu_inventory" in source or "sync_host" in source, source[:400]
    tree = ast.parse(source)
    names = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"sync_host", "bootstrap_gpu_inventory"} & names, f"deps 里没人叫 inventory 重报：{sorted(names)}"

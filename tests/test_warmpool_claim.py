"""Warm Pool claim 模型（§18）：READY → atomic claim → CLAIMED/RUNNING。

- 并发 claim 同一 warm runtime：至多一个用户成功
- READY runtime 没有用户归属/用户数据
- claim 后 attach user + 轮换凭据（加密落库）
- 无 READY → fallback（None）
"""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Gpu, GpuHost, GpuStatus, Template, User, WarmPoolState, Workspace, WorkspaceStatus
from app.security import WorkspaceCredentialCipher
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.warmpool import WarmPoolManager
from app.services.worker import OperationWorker
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("warmpool-claim"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


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
    user = User(id=user_id, email=f"{user_id}@example.com", username=user_id, password_hash="x")  # noqa: S106
    db.add(user)
    db.commit()
    return user


CIPHER = WorkspaceCredentialCipher("test-key-claim-0001")


def _make_manager(db_factory, *, enabled: bool = True, size: int = 1) -> WarmPoolManager:
    provider = MockProvider("http://127.0.0.1:8000")
    orchestrator = WorkspaceOrchestrator(db_factory, provider, Path("/tmp/test-warm-claim"))  # noqa: S108
    settings = SimpleNamespace(warm_pool_enabled=enabled, warm_pool_size=size, warm_pool_reserve_slots=0)
    return WarmPoolManager(db_factory, orchestrator, settings)


def _claim(manager, db, template_id: str, user_id: str):
    """helper：带 cipher 的 claim（rotation 成功路径）。"""
    user = db.get(User, user_id)
    return manager.claim(db, template_id, user, credential_cipher=CIPHER)


def _drain(manager) -> None:
    """驱动 worker 执行完 maintain 异步入队的 PROVISION operation。"""
    worker = OperationWorker(Factory, manager.orchestrator)
    for _ in range(20):
        if worker.tick_once() == 0:
            return
    raise AssertionError("operations did not drain")


def _warm_pool(db, manager) -> Workspace:
    """maintain → 异步 provision（worker 驱动）→ 再 maintain 收割 → READY warm runtime。"""
    manager.maintain(db)
    ws_id = db.scalars(select(Workspace.id)).one()
    _drain(manager)
    with Factory() as reap_db:
        manager.maintain(reap_db)
    # 结束 db 会话事务，避免读到 drain/reap 之前的旧快照
    db.rollback()
    ws = db.get(Workspace, ws_id)
    assert ws is not None
    assert ws.warm_pool_state == WarmPoolState.READY.value
    assert ws.status == WorkspaceStatus.RUNNING.value
    assert ws.user_id is None  # READY 没有用户归属
    return ws


def test_maintain_builds_ready_runtime_without_owner():
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        manager = _make_manager(Factory)
        ws = _warm_pool(db, manager)
        assert ws.name.startswith("warm-")
        assert ws.user_id is None


def test_claim_attaches_user_and_rotates_credential():
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        manager = _make_manager(Factory)
        ws = _warm_pool(db, manager)
        old_password = ws.password
        user = _make_user(db, "user-1")

        claimed = manager.claim(db, "cartpole", user, credential_cipher=CIPHER)
        assert claimed is not None
        assert claimed.id == ws.id
        assert claimed.user_id == "user-1"
        assert claimed.warm_pool_state == WarmPoolState.CLAIMED.value
        assert claimed.status == WorkspaceStatus.RUNNING.value
        assert claimed.started_at is not None
        # 凭据轮换：新密文 ≠ 旧密文
        assert claimed.password != old_password

        # 池中 READY 已耗尽 → 再次 claim 返回 None（fallback）
        assert manager.claim(db, "cartpole", user, credential_cipher=CIPHER) is None


def test_concurrent_claim_at_most_one_wins():
    """并发 claim 同一个 READY warm runtime：至多一个用户成功。"""
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        manager = _make_manager(Factory)
        ws = _warm_pool(db, manager)
        _make_user(db, "user-a")
        _make_user(db, "user-b")

    results: list[str | None] = []
    lock = threading.Lock()

    def worker(user_id: str):
        with Factory() as db:
            user = db.get(User, user_id)
            claimed = manager.claim(db, "cartpole", user, credential_cipher=CIPHER)
            with lock:
                results.append(claimed.id if claimed else None)

    threads = [threading.Thread(target=worker, args=(u,)) for u in ("user-a", "user-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r is not None]
    assert len(winners) == 1  # 恰好一个用户 claim 成功
    assert winners[0] == ws.id
    # 另一个用户拿到 None（fallback normal provision）
    assert results.count(None) == 1


def test_claim_disabled_pool_returns_none():
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        manager = _make_manager(Factory, enabled=False)
        user = _make_user(db, "user-1")
        assert manager.claim(db, "cartpole", user, credential_cipher=CIPHER) is None


def test_claim_without_ready_falls_back():
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        manager = _make_manager(Factory)  # 不 maintain，池为空
        user = _make_user(db, "user-1")
        assert manager.claim(db, "cartpole", user, credential_cipher=CIPHER) is None


class NoRotateProvider(MockProvider):
    """rotate_credentials 返回 False（模拟 Docker 等不支持运行时轮换的 provider）。"""

    def rotate_credentials(self, workspace, credentials):
        return False


def test_claim_rotation_failure_not_delivered():
    """轮换失败：不得把 workspace 交给用户 → DRAINING + fallback（claim 返回 None）。"""
    provider = NoRotateProvider("http://127.0.0.1:8000")
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-warm-claim2")  # noqa: S108
    )
    settings = SimpleNamespace(warm_pool_enabled=True, warm_pool_size=1, warm_pool_reserve_slots=0)
    manager = WarmPoolManager(Factory, orchestrator, settings)
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        _make_user(db, "user-1")
        ws = _warm_pool(db, manager)

        result = manager.claim(db, "cartpole", db.get(User, "user-1"), credential_cipher=CIPHER)
        assert result is None  # 未交付
        db.refresh(ws)
        # 进入 DRAINING + 无 owner + 无凭据（旧密码不可滞留）
        assert ws.warm_pool_state == WarmPoolState.DRAINING.value
        assert ws.user_id is None
        assert ws.password is None


class RotatingNoopProvider(MockProvider):
    """记录 rotate 调用（证明 rotation 在交付前发生）。"""

    def __init__(self):
        super().__init__("http://127.0.0.1:8000")
        self.rotated: list[str] = []

    def rotate_credentials(self, workspace, credentials):
        self.rotated.append(credentials.get("password", ""))
        return True


def test_claim_rotates_runtime_credential_before_delivery():
    """交付前必须 rotate runtime credential（Mock 记录调用 + 新密码生效）。"""
    provider = RotatingNoopProvider()
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-warm-claim3")  # noqa: S108
    )
    settings = SimpleNamespace(warm_pool_enabled=True, warm_pool_size=1, warm_pool_reserve_slots=0)
    manager = WarmPoolManager(Factory, orchestrator, settings)
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        _make_user(db, "user-1")
        ws = _warm_pool(db, manager)
        old_password = ws.password

        claimed = manager.claim(db, "cartpole", db.get(User, "user-1"), credential_cipher=CIPHER)
        assert claimed is not None
        # provider 收到新密码（≠ 旧密码），且先于交付发生
        assert len(provider.rotated) == 1
        assert provider.rotated[0] != CIPHER.decrypt(old_password)
        assert CIPHER.decrypt(claimed.password) == provider.rotated[0]
        assert claimed.warm_pool_state == WarmPoolState.CLAIMED.value
        assert claimed.user_id == "user-1"


def test_api_launch_prefers_warm_pool_claim():
    """§6：POST /api/workspaces 走 WarmPool claim 真实产品路径（池 READY 时命中）。

    隔离：直接预置 READY warm runtime（不触发 PROVISION / 不驱动后台 worker），
    避免与全局 worker 并发建池、消耗 mock GPU，污染其它用例（保证全量任意顺序全绿）。
    """
    import uuid

    from fastapi.testclient import TestClient

    from app.deps import SessionFactory
    from app.deps import orchestrator as global_orch
    from app.deps import settings as deps_settings
    from app.main import app as fastapi_app

    old_enabled = deps_settings.warm_pool_enabled
    old_size = deps_settings.warm_pool_size
    deps_settings.warm_pool_enabled = True
    deps_settings.warm_pool_size = 1
    seeded_id = f"wp-ready-api-{uuid.uuid4().hex[:12]}"
    try:
        with TestClient(fastapi_app) as client:
            # lifespan bootstrap 已建表 + seed；直接预置一个 READY warm runtime
            # （无 GPU、无 PROVISION，纯 claim 路径）
            with SessionFactory() as db:
                db.add(
                    Workspace(
                        id=seeded_id,
                        name="warm-ready",
                        template_id="cartpole",
                        provider="mock",
                        status=WorkspaceStatus.RUNNING.value,
                        warm_pool_state=WarmPoolState.READY.value,
                        user_id=None,
                    )
                )
                db.commit()

            resp = client.post(
                "/api/auth/register",
                json={"email": "wp@example.com", "username": "wpu", "password": "password123"},
            )
            token = resp.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}

            created = client.post(
                "/api/workspaces", json={"template_id": "cartpole", "auto_start": True}, headers=headers
            )
            assert created.status_code == 201
            workspace_id = created.json()["id"]
            # claim 命中预置的 READY workspace（而非新建普通 workspace）
            assert workspace_id == seeded_id

            state = client.get(f"/api/workspaces/{workspace_id}", headers=headers).json()
            assert state["status"] == "running"
            with SessionFactory() as db:
                claimed = db.get(Workspace, workspace_id)
                assert claimed is not None
                assert claimed.user_id is not None
    finally:
        deps_settings.warm_pool_enabled = old_enabled
        deps_settings.warm_pool_size = old_size
        # 清理：销毁预置/claim 的 warm workspace（释放占位，不污染其它用例）
        with SessionFactory() as db:
            warm_ids = [
                w.id for w in db.scalars(select(Workspace).where(Workspace.warm_pool_state.is_not(None)))
            ]
        for wid in warm_ids:
            with SessionFactory() as db:
                w = db.get(Workspace, wid)
                if w is not None:
                    global_orch.destroy(db, w)


def test_pool_metrics_uses_real_counts():
    """§8：pool_metrics 返回 COUNT(*)，非存在性 0/1。"""
    provider = MockProvider("http://127.0.0.1:8000")
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-warm-claim4")  # noqa: S108
    )
    settings = SimpleNamespace(warm_pool_enabled=True, warm_pool_size=3, warm_pool_reserve_slots=0)
    manager = WarmPoolManager(Factory, orchestrator, settings)
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        # 直接构造同状态多个 workspace：验证 COUNT(*) 语义（同状态计数 >1，非 0/1 标志）
        for i, state in enumerate(
            [WarmPoolState.READY.value, WarmPoolState.READY.value, WarmPoolState.PREWARMING.value]
        ):
            db.add(
                Workspace(
                    id=f"ws-{i}", name=f"warm-{i}", template_id="cartpole", provider="mock",
                    status=WorkspaceStatus.RUNNING.value, warm_pool_state=state,
                )
            )
        db.commit()
        metrics = manager.pool_metrics(db)
        assert metrics["cartpole"][WarmPoolState.READY.value] == 2  # 2 个 READY，非 1
        assert metrics["cartpole"][WarmPoolState.PREWARMING.value] == 1
        assert metrics["cartpole"][WarmPoolState.FAILED.value] == 0


class CleanupTrackingProvider(MockProvider):
    """跟踪 destroy 调用（验证 rotation 失败补偿清理 runtime）。"""

    def __init__(self):
        super().__init__("http://127.0.0.1:8000")
        self.destroy_calls = 0

    def rotate_credentials(self, workspace, credentials):
        return False

    def destroy(self, workspace):
        self.destroy_calls += 1


def test_warm_pool_rotation_failure_cleans_runtime():
    """§7（P0）：rotation 失败必须销毁 runtime（0 orphan container/pod）。

    覆盖的是"destroy 命令成功"那一档（provider 认账退役 ⇒ 寻址字段可以清、状态可以写终态）；
    destroy 报错而 provider 仍说活着的那一档**不许**清 `container_name`，
    判据与两极见 tests/test_warmpool_claim_admission.py。
    """
    provider = CleanupTrackingProvider()
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-warm-leak1")  # noqa: S108
    )
    settings = SimpleNamespace(warm_pool_enabled=True, warm_pool_size=1, warm_pool_reserve_slots=0)
    manager = WarmPoolManager(Factory, orchestrator, settings)
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        _make_user(db, "user-1")
        ws = _warm_pool(db, manager)
        result = manager.claim(db, "cartpole", db.get(User, "user-1"), credential_cipher=CIPHER)
        assert result is None
        # provider.destroy 被调用（runtime 已清理）
        assert provider.destroy_calls >= 1
        db.refresh(ws)
        assert ws.container_name is None  # 容器引用清除
        assert ws.status == WorkspaceStatus.FAILED.value


def test_warm_pool_rotation_failure_releases_gpu_only_when_admitted():
    """§7（P0）：rotation 失败时 GPU 回不回池，看 provider 认不认账，不看 destroy 的返回码。

    **这一支改前钉的是缺陷形状**：它只断"没有 allocation、卡回 AVAILABLE"，而夹具
    `CleanupTrackingProvider.destroy` 从不失败、`reconcile` 只会说 UNKNOWN（mock），
    也就是"撤销必然放卡"被当成了期望 —— 放卡这件事在改前是无条件的，怎么改代码它都绿。
    现在把它收回到它真正覆盖的那一档：**destroy 命令成功**（`command_succeeded=True`）
    且 provider 不再自述 ALIVE（这里是 UNKNOWN，"没有可观测 runtime"那一档）⇒ 准入放行，
    放卡 + FAILED。"必须不交付"的原始意图由 `provider.rotate_credentials` 返回 False +
    上面的 DRAINING 断言继续钉住。
    另两档（destroy 报错但 provider 说 MISSING ⇒ 仍须放卡；报错且说 ALIVE/UNKNOWN ⇒ 不许放卡）
    连同 DRAINING 档的重试收敛，常驻在 tests/test_warmpool_claim_admission.py。
    """
    from app.models import GpuAllocation as GA

    provider = CleanupTrackingProvider()
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-warm-leak2")  # noqa: S108
    )
    settings = SimpleNamespace(warm_pool_enabled=True, warm_pool_size=1, warm_pool_reserve_slots=0)
    manager = WarmPoolManager(Factory, orchestrator, settings)
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        _make_user(db, "user-1")
        ws = _warm_pool(db, manager)
        manager.claim(db, "cartpole", db.get(User, "user-1"), credential_cipher=CIPHER)
        # 这一档的准入前提：destroy 命令本身成功了（改前对这一判据毫无线索）
        assert provider.destroy_calls == 1
        # GPU 释放：无 allocation、GPU 回 AVAILABLE
        assert db.scalar(select(GA).where(GA.workspace_id == ws.id)) is None
        gpu = db.scalar(select(Gpu))
        assert gpu.status == GpuStatus.AVAILABLE.value
        assert gpu.workspace_id is None


def test_failed_claim_can_not_be_reclaimed():
    """§7（P0）：DRAINING/FAILED 的 warm workspace 不能再次被 claim。"""
    provider = CleanupTrackingProvider()
    orchestrator = WorkspaceOrchestrator(
        Factory, provider, Path("/tmp/test-warm-leak3")  # noqa: S108
    )
    settings = SimpleNamespace(warm_pool_enabled=True, warm_pool_size=1, warm_pool_reserve_slots=0)
    manager = WarmPoolManager(Factory, orchestrator, settings)
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        _make_user(db, "user-1")
        ws = _warm_pool(db, manager)
        assert manager.claim(db, "cartpole", db.get(User, "user-1"), credential_cipher=CIPHER) is None
        # 再次 claim：不命中 DRAINING/FAILED 的 workspace → None（无 READY）
        assert manager.claim(db, "cartpole", db.get(User, "user-1"), credential_cipher=CIPHER) is None
        db.refresh(ws)
        assert ws.warm_pool_state == WarmPoolState.DRAINING.value


def test_docker_provider_reports_no_rotation_support():
    """Docker provider 必须声明不支持凭据轮换（warm pool 因此默认禁用）。"""
    from app.config import Settings as S
    from app.services.providers.docker import DockerProvider

    provider = DockerProvider(S(workspace_root=Path("/tmp/test-docker-rot")))  # noqa: S108
    assert provider.supports_credential_rotation is False
    assert provider.rotate_credentials(None, {"password": "x"}) is False


def test_successful_claim_records_claim_latency():
    """claim 的耗时必须先可观测，"P50<15s／P95<30s" 那句 SLA 才谈得上被回答。

    今天唯一的直方图 `workspace_launch_seconds` 只在 provision 路径上 observe
    （`app/services/orchestrator.py:263`），而 warm pool 的全部意义就是**不走** provision
    ——所以最快的那条路径反而没有任何耗时读数。反向对照同在一支里：池空（claim 返回 None）
    不得记一次样本，否则"什么都没抢到"会被统计成"claim 很快"。
    """
    from prometheus_client import REGISTRY

    def samples() -> float:
        return (
            REGISTRY.get_sample_value(
                "warm_pool_claim_seconds_count", {"template_id": "cartpole"}
            )
            or 0.0
        )

    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        manager = _make_manager(Factory)
        _warm_pool(db, manager)
        user = _make_user(db, "user-lat")
        before = samples()
        assert manager.claim(db, "cartpole", user, credential_cipher=CIPHER) is not None
        assert samples() == before + 1, "成功的 claim 没记耗时：SLA 无从测量"
        # 池已空 → None，且读数不动（"没抢到"不是一次快 claim）
        assert manager.claim(db, "cartpole", user, credential_cipher=CIPHER) is None
        assert samples() == before + 1, "池空的 None 返回被记成了一次 claim 样本"

def test_claim_refuses_up_front_when_the_provider_cannot_rotate():
    """§7 的 P0 规则要在 claim 入口就挡住，而不是"占了格 → 轮换失败 → 拆掉 runtime"再兜回来。

    用的是**真的 DockerProvider**（它的 `rotate_credentials` 是纯 `return False`，不碰守护进程），
    不是上面那批用例里的假 provider——要判的是生产上那个 provider 真在场时的形状。
    改造前的真实后果不是泄露凭据（补偿路径确实拆了 runtime、清了密码），而是：
    每次 claim 都占掉一格 READY、拆掉一个真 runtime、走一遍 billing 圈额/退额，
    再告诉调用方"没有 warm 可用"——而这件事在入口一句判断就能免掉。
    """
    from app.config import Settings as S
    from app.services.providers.docker import DockerProvider

    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        ws = _warm_pool(db, _make_manager(Factory))
        before = (ws.warm_pool_state, ws.status, ws.user_id, ws.container_name, ws.password)

        docker_orchestrator = WorkspaceOrchestrator(
            Factory,
            DockerProvider(S(workspace_root=Path("/tmp/test-warm-docker"))),  # noqa: S108
            Path("/tmp/test-warm-docker"),  # noqa: S108
        )
        docker_manager = WarmPoolManager(
            Factory,
            docker_orchestrator,
            SimpleNamespace(warm_pool_enabled=True, warm_pool_size=1, warm_pool_reserve_slots=0),
        )
        user = _make_user(db, "user-docker")
        assert docker_manager.claim(db, "cartpole", user, credential_cipher=CIPHER) is None
        db.expunge_all()
        row = db.get(Workspace, ws.id)
        after = (row.warm_pool_state, row.status, row.user_id, row.container_name, row.password)
        assert after == before, (
            f"claim 没在入口挡住：仍然占了格并拆掉 runtime\n  before={before}\n  after ={after}"
        )


def test_provider_constraint_is_testable_at_the_composition_root():
    """§7 那句"provider 不支持轮换 ⇒ warm pool 禁用"以前是 import 期的裸 if，没有任何常驻读者。

    挪进 `apply_provider_constraints` 之后三档各自可证：不支持轮换必须关掉；
    支持轮换的不许动；已经关着的保持关（这一档证明它不是"永远写 False"那种假合规）。
    """
    from app.deps import apply_provider_constraints

    class NoRotate:
        name = "docker-like"
        supports_credential_rotation = False

    class Rotates:
        name = "k8s-like"
        supports_credential_rotation = True

    off = SimpleNamespace(warm_pool_enabled=True)
    apply_provider_constraints(off, NoRotate())
    assert off.warm_pool_enabled is False, "不支持轮换的 provider 没能禁用 warm pool"

    keep = SimpleNamespace(warm_pool_enabled=True)
    apply_provider_constraints(keep, Rotates())
    assert keep.warm_pool_enabled is True, "支持轮换的 provider 被误关了"

    already = SimpleNamespace(warm_pool_enabled=False)
    apply_provider_constraints(already, NoRotate())
    assert already.warm_pool_enabled is False

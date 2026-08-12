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
from app.models import Gpu, GpuHost, Template, User, WarmPoolState, Workspace, WorkspaceStatus
from app.security import WorkspaceCredentialCipher
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.warmpool import WarmPoolManager

ENGINE = create_engine("sqlite:///./test-warmpool-claim.db", connect_args={"check_same_thread": False})
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
    settings = SimpleNamespace(warm_pool_enabled=enabled, warm_pool_size=size)
    return WarmPoolManager(db_factory, orchestrator, settings)


def _claim(manager, db, template_id: str, user_id: str):
    """helper：带 cipher 的 claim（rotation 成功路径）。"""
    user = db.get(User, user_id)
    return manager.claim(db, template_id, user, credential_cipher=CIPHER)


def _warm_pool(db, manager) -> Workspace:
    """maintain 出一个 READY warm runtime。"""
    manager.maintain(db)
    ws = db.scalars(select(Workspace)).one()
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
    settings = SimpleNamespace(warm_pool_enabled=True, warm_pool_size=1)
    manager = WarmPoolManager(Factory, orchestrator, settings)
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        _make_user(db, "user-1")
        manager.maintain(db)
        ws = db.scalars(select(Workspace)).one()
        assert ws.warm_pool_state == WarmPoolState.READY.value

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
    settings = SimpleNamespace(warm_pool_enabled=True, warm_pool_size=1)
    manager = WarmPoolManager(Factory, orchestrator, settings)
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        _make_user(db, "user-1")
        manager.maintain(db)
        ws = db.scalars(select(Workspace)).one()
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
    """§6：POST /api/workspaces 走 WarmPool claim 真实产品路径（mock 池 READY 时命中）。"""
    from fastapi.testclient import TestClient

    from app.deps import SessionFactory
    from app.deps import settings as deps_settings
    from app.main import app as fastapi_app

    # 打开 warm pool（设置全局开关；测试后还原）
    old = deps_settings.warm_pool_enabled
    deps_settings.warm_pool_enabled = True
    deps_settings.warm_pool_size = 2
    try:
        # 预热一个 READY runtime
        from app.services.orchestrator import WorkspaceOrchestrator as O
        from app.services.providers.mock import MockProvider as M
        from app.services.warmpool import WarmPoolManager

        wp_orch = O(SessionFactory, M("http://127.0.0.1:8000"), Path("/tmp/test-wp-api"))  # noqa: S108
        wp = WarmPoolManager(SessionFactory, wp_orch, deps_settings)

        with TestClient(fastapi_app) as client:
            # lifespan 已 bootstrap 全局 DB；先预热再请求
            with SessionFactory() as db:
                wp.maintain(db)
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

            # claim 命中：workspace 直接 RUNNING（无需等待 provision），且绑定当前用户
            state = client.get(f"/api/workspaces/{workspace_id}", headers=headers).json()
            assert state["status"] == "running"
            # warm pool 池已减少（该 workspace 不再是 READY）
            from app.models import Workspace as W

            with SessionFactory() as db:
                claimed = db.get(W, workspace_id)
                assert claimed is not None
                assert claimed.user_id is not None
    finally:
        deps_settings.warm_pool_enabled = old
        # 清理：释放 warm pool 占用的全局 GPU（tombstone warm workspaces）
        from sqlalchemy import select as _select

        from app.models import Workspace as W

        with SessionFactory() as db:
            warm_ids = [
                w.id for w in db.scalars(_select(W).where(W.warm_pool_state.is_not(None)))
            ]
        if warm_ids:
            orch = wp_orch
            for wid in warm_ids:
                with SessionFactory() as db:
                    w = db.get(W, wid)
                    if w is not None:
                        orch.destroy(db, w)

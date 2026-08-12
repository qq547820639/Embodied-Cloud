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


def _make_manager(db_factory, *, enabled: bool = True, size: int = 1) -> WarmPoolManager:
    provider = MockProvider("http://127.0.0.1:8000")
    orchestrator = WorkspaceOrchestrator(db_factory, provider, Path("/tmp/test-warm-claim"))  # noqa: S108
    settings = SimpleNamespace(warm_pool_enabled=enabled, warm_pool_size=size)
    return WarmPoolManager(db_factory, orchestrator, settings)


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
    cipher = WorkspaceCredentialCipher("test-key-claim-0001")
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        manager = _make_manager(Factory)
        ws = _warm_pool(db, manager)
        old_password = ws.password
        user = _make_user(db, "user-1")

        claimed = manager.claim(db, "cartpole", user, credential_cipher=cipher)
        assert claimed is not None
        assert claimed.id == ws.id
        assert claimed.user_id == "user-1"
        assert claimed.warm_pool_state == WarmPoolState.CLAIMED.value
        assert claimed.status == WorkspaceStatus.RUNNING.value
        assert claimed.started_at is not None
        # 凭据轮换：新密文 ≠ 旧密文
        assert claimed.password != old_password

        # 池中 READY 已耗尽 → 再次 claim 返回 None（fallback）
        assert manager.claim(db, "cartpole", user, credential_cipher=cipher) is None


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
            claimed = manager.claim(db, "cartpole", user)
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
        assert manager.claim(db, "cartpole", user) is None


def test_claim_without_ready_falls_back():
    with Factory() as db:
        _make_template(db)
        _seed_gpu(db)
        manager = _make_manager(Factory)  # 不 maintain，池为空
        user = _make_user(db, "user-1")
        assert manager.claim(db, "cartpole", user) is None

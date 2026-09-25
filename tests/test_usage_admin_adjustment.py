"""admin_adjustment 路由：无 target 记 admin、有 target 记目标用户、目标不存在 404。"""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.routers.usage as usage_router
from app.models import Base, LedgerType, Organization, Role, User
from app.services.ledger import CreditLedgerService
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("usage-admin"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    # 路由内部使用 app.deps 的单例 ledger；测试注入绑到测试 Factory 的服务
    monkeypatch.setattr(usage_router, "ledger", CreditLedgerService(Factory))
    yield
    Base.metadata.drop_all(ENGINE)


def _user(db, user_id: str, *, role: str = Role.USER.value, organization_id: str | None = None) -> User:
    user = User(
        id=user_id,
        email=f"{user_id}@example.com",
        username=user_id,
        password_hash="x",  # noqa: S106 测试数据
        role=role,
        organization_id=organization_id,
    )
    db.add(user)
    db.commit()
    return user


def test_admin_adjustment_without_target_records_to_admin():
    with Factory() as db:
        admin = _user(db, "admin-1", role=Role.ADMIN.value)
        entry = usage_router.admin_adjustment(
            amount=100, description="credit", user=admin, db=db,
        )
        assert entry.type == LedgerType.ADJUSTMENT.value
        assert entry.user_id == "admin-1"
        assert entry.organization_id is None  # 向后兼容：不写组织
        assert usage_router.ledger.balance(db, "admin-1") == 100


def test_admin_adjustment_with_target_records_to_target_and_org():
    with Factory() as db:
        admin = _user(db, "admin-1", role=Role.ADMIN.value)
        db.add(Organization(id="org-1", name="org"))
        db.commit()
        _user(db, "target-1", organization_id="org-1")

        entry = usage_router.admin_adjustment(
            amount=-50, description="deduct", user=admin, db=db, target_user_id="target-1",
        )
        assert entry.user_id == "target-1"
        assert entry.organization_id == "org-1"
        assert usage_router.ledger.balance(db, "target-1") == -50
        # admin 自身无入账
        assert usage_router.ledger.balance(db, "admin-1") == 0


def test_admin_adjustment_missing_target_returns_404():
    with Factory() as db:
        admin = _user(db, "admin-1", role=Role.ADMIN.value)
        with pytest.raises(HTTPException) as exc_info:
            usage_router.admin_adjustment(
                amount=1, description="x", user=admin, db=db, target_user_id="no-such-user",
            )
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# HTTP 层（此前 admin_adjustment 只测函数直调，403 门禁/参数绑定/序列化零覆盖）
# ---------------------------------------------------------------------------


@pytest.fixture
def http_app(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.deps import get_current_user, get_db

    app = FastAPI()
    app.include_router(usage_router.router)
    monkeypatch.setattr(usage_router, "ledger", CreditLedgerService(Factory))
    # 会话级 db：整个测试用同一个 session
    factory = Factory
    holder = {"db": None}

    def _db_override():
        if holder["db"] is None:
            holder["db"] = factory()
        return holder["db"]

    def _user_override():
        return holder["current_user"]

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_current_user] = _user_override
    client = TestClient(app)
    yield client, holder
    if holder["db"] is not None:
        holder["db"].close()


def test_admin_adjustment_http_non_admin_403(http_app):
    client, holder = http_app
    with Factory() as db:
        holder["current_user"] = _user(db, "plain-user", role=Role.USER.value)
    resp = client.post(
        "/admin/ledger/adjustment",
        params={"amount": 50, "description": "x"},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 403


def test_admin_adjustment_http_admin_200(http_app):
    client, holder = http_app
    with Factory() as db:
        holder["current_user"] = _user(db, "admin-h", role=Role.ADMIN.value)
    resp = client.post(
        "/admin/ledger/adjustment",
        params={"amount": 50, "description": "bonus"},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == LedgerType.ADJUSTMENT.value
    assert body["amount"] == 50
    assert body["user_id"] == "admin-h"

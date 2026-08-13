"""认证端点与安全原语测试（login/logout/me + verify_password 边界）。

此前 login/logout/me 与 verify_password 全仓库零覆盖（审计盲区），
本文件补齐：成功/失败路径、会话撤销、禁用账号、token 哈希语义。
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import User
from app.security import hash_password, hash_token, verify_password
from tests.test_demo_workspace import _auth, _register


def test_verify_password_roundtrip_and_pepper():
    stored = hash_password("correct-horse-battery", pepper="pep")
    assert verify_password("correct-horse-battery", stored, pepper="pep") is True
    assert verify_password("wrong-password", stored, pepper="pep") is False
    # pepper 变化 → 验证失败（生产 fail-closed 的关键语义）
    assert verify_password("correct-horse-battery", stored, pepper="other") is False


def test_verify_password_malformed_returns_false():
    assert verify_password("x", "not-a-valid-format") is False
    assert verify_password("x", "pbkdf2$notanumber$zz$zz") is False
    assert verify_password("x", "pbkdf2$1000$nothex$nothex") is False


def test_login_logout_me_flow():
    with TestClient(app) as client:
        email = "auth-flow@example.com"
        reg = client.post(
            "/api/auth/register",
            json={"email": email, "username": "authflow", "password": "password123"},
        )
        assert reg.status_code == 201, reg.text
        token = reg.json()["token"]

        # me 返回真实用户
        me_resp = client.get("/api/auth/me", headers=_auth(token))
        assert me_resp.status_code == 200
        assert me_resp.json()["email"] == email
        assert me_resp.json()["role"] == "user"

        # 错密码 → 401；正确密码 → 新 token
        bad = client.post("/api/auth/login", json={"email": email, "password": "wrong-pass-123"})
        assert bad.status_code == 401
        good = client.post("/api/auth/login", json={"email": email, "password": "password123"})
        assert good.status_code == 200

        # logout 撤销当前用户全部会话（含旧 token）
        assert client.post("/api/auth/logout", headers=_auth(token)).status_code == 204
        assert client.get("/api/auth/me", headers=_auth(token)).status_code == 401
        assert client.get("/api/auth/me", headers=_auth(good.json()["token"])).status_code == 401


def test_login_disabled_account_403():
    with TestClient(app) as client:
        email = "disabled@example.com"
        token = _register(client, email, "disabled")
        from app.deps import SessionFactory

        with SessionFactory() as db:
            user = db.scalar(select(User).where(User.email == email))
            assert user is not None
            user.is_active = False
            db.commit()
        # 禁用后旧会话立即失效（401）且无法再登录（403）
        assert client.get("/api/auth/me", headers=_auth(token)).status_code == 401
        resp = client.post("/api/auth/login", json={"email": email, "password": "password123"})
        assert resp.status_code == 403


def test_session_token_only_hash_stored():
    """服务端只存 SHA-256 哈希：DB 中不出现原始 token（SECURITY.md T2）。"""
    with TestClient(app) as client:
        reg = client.post(
            "/api/auth/register",
            json={"email": "hashcheck@example.com", "username": "hashcheck", "password": "password123"},
        )
        token = reg.json()["token"]
        from app.deps import SessionFactory
        from app.models import UserSession

        with SessionFactory() as db:
            rows = list(db.scalars(select(UserSession)))
            assert rows, "expected at least one session row"
            stored_hashes = {row.token_hash for row in rows}
            assert hash_token(token) in stored_hashes
            assert all(token not in h for h in stored_hashes)

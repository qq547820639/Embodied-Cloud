"""demo-workspace 演示端点鉴权测试（SECURITY.md T1）。

匿名 → 401；跨租户 → 404（不泄露存在性）；owner/admin → 200 且页面含 workspace.name。
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app


def _register(client: TestClient, email: str, username: str) -> str:
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "username": username, "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _promote_to_admin(email: str) -> None:
    from app.deps import SessionFactory
    from app.models import Role, User

    with SessionFactory() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = Role.ADMIN.value
        db.commit()


def test_demo_workspace_requires_auth_and_owner_scope():
    with TestClient(app) as client:
        token_a = _register(client, "demo-a@example.com", "demo-a")
        token_b = _register(client, "demo-b@example.com", "demo-b")

        created = client.post(
            "/api/workspaces",
            json={"template_id": "cartpole", "auto_start": False},
            headers=_auth(token_a),
        )
        assert created.status_code == 201
        wid = created.json()["id"]
        name = created.json()["name"]
        assert name

        # 匿名（无 cookie/token）→ 401
        assert client.get(f"/demo-workspace/{wid}").status_code == 401

        # 跨租户（非 owner 非 admin）→ 404，不泄露存在性
        assert client.get(f"/demo-workspace/{wid}", headers=_auth(token_b)).status_code == 404

        # owner → 200 且页面含 workspace.name
        resp = client.get(f"/demo-workspace/{wid}", headers=_auth(token_a))
        assert resp.status_code == 200
        assert name in resp.text

        # 不存在的 workspace → 404
        assert client.get("/demo-workspace/does-not-exist", headers=_auth(token_a)).status_code == 404

        # admin 可访问任意 workspace → 200
        _promote_to_admin("demo-b@example.com")
        assert client.get(f"/demo-workspace/{wid}", headers=_auth(token_b)).status_code == 200

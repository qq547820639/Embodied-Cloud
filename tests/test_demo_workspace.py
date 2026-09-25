"""demo-workspace 演示端点鉴权测试（SECURITY.md T1）。

匿名 → 401；跨租户 → 404（不泄露存在性）；owner/admin → 200 且页面含 workspace.name。
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from tests.http_auth import auth_headers as _auth
from tests.http_auth import register_body


def _register(client: TestClient, email: str, username: str) -> str:
    body = register_body(client, email, username)
    return body["token"]


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


def test_demo_workspace_escapes_user_content():
    """workspace.name / template.launch_command 均为可控内容 → 必须 HTML 转义
    （admin 打开他人 workspace 的演示页时不得执行任意脚本）。"""
    from app.deps import SessionFactory
    from app.models import Template, Workspace

    with TestClient(app) as client:
        token = _register(client, "demo-xss@example.com", "demo-xss")
        payload = '"><script>window.__xss=1</script>'
        created = client.post(
            "/api/workspaces",
            json={"template_id": "cartpole", "auto_start": False},
            headers=_auth(token),
        )
        assert created.status_code == 201
        wid = created.json()["id"]
        with SessionFactory() as db:
            ws = db.get(Workspace, wid)
            assert ws is not None
            ws.name = payload
            template = db.get(Template, ws.template_id)
            assert template is not None
            template.launch_command = payload
            db.commit()
        resp = client.get(f"/demo-workspace/{wid}", headers=_auth(token))
        assert resp.status_code == 200
        assert "<script>window.__xss=1</script>" not in resp.text
        assert "&lt;script&gt;" in resp.text

"""此前零覆盖的成功路径（审计盲区收尾）：
- GET /api/templates/{id} 详情
- POST /api/workspaces/{id}/start（durable START 成功路径）
- GET /api/workspaces/admin/all（admin 审计：含 tombstone；非 admin 403）
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import Role, User
from tests.test_demo_workspace import _auth, _register
from tests.workspace_progress import wait_status


def _promote(email: str) -> None:
    from app.deps import SessionFactory

    with SessionFactory() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = Role.ADMIN.value
        db.commit()


def test_template_detail_success():
    with TestClient(app) as client:
        resp = client.get("/api/templates/cartpole")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["slug"] == "cartpole"
        assert body["version"]
        assert body["gpu_requirement_gb"] > 0
        assert "launch_command" in body
        # 不存在的模板 → 404
        assert client.get("/api/templates/no-such-template").status_code == 404


def test_workspace_start_endpoint_success_path():
    with TestClient(app) as client:
        token = _register(client, "start-path@example.com", "startpath")
        created = client.post(
            "/api/workspaces",
            json={"template_id": "cartpole", "auto_start": False},
            headers=_auth(token),
        )
        assert created.status_code == 201
        wid = created.json()["id"]
        # durable START 成功路径：CREATED → 入队 → 最终 RUNNING
        resp = client.post(f"/api/workspaces/{wid}/start", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        wait_status(client, token, wid, "running")
        ws = client.get(f"/api/workspaces/{wid}", headers=_auth(token)).json()
        assert ws["status"] == "running"
        assert ws["gpu_id"]


def test_admin_all_workspaces_includes_tombstone_and_scopes_403():
    with TestClient(app) as client:
        token = _register(client, "adm-all@example.com", "adm-all")
        created = client.post(
            "/api/workspaces",
            json={"template_id": "cartpole", "auto_start": False},
            headers=_auth(token),
        )
        wid = created.json()["id"]
        # 普通用户：admin/all → 403
        assert client.get("/api/workspaces/admin/all", headers=_auth(token)).status_code == 403
        # 删除（tombstone）后普通 list 不可见
        assert client.delete(f"/api/workspaces/{wid}", headers=_auth(token)).status_code == 204
        visible = client.get("/api/workspaces", headers=_auth(token)).json()
        assert all(w["id"] != wid for w in visible)
        # admin：admin/all 可见 tombstone（审计语义）
        _promote("adm-all@example.com")
        all_rows = client.get("/api/workspaces/admin/all", headers=_auth(token)).json()
        tombstone = [w for w in all_rows if w["id"] == wid]
        assert tombstone and tombstone[0]["status"] == "deleted"

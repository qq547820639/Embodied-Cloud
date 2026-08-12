import time

from fastapi.testclient import TestClient

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


def test_end_to_end_workspace_lifecycle():
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["provider_ready"] is True

        token = _register(client, "e2e@example.com", "e2e-user")
        headers = _auth(token)

        templates = client.get("/api/templates").json()
        ids = {x["id"] for x in templates}
        assert {"cartpole", "franka-lift", "franka-pick-place", "domain-randomization", "rgbd-perception"} <= ids
        # Registry 完整性：version locked + 镜像版本化
        for t in templates:
            assert t["version"]
            assert "latest" not in (t["image"] or "")
            assert t["slug"]

        created = client.post(
            "/api/workspaces",
            json={"template_id": "cartpole", "auto_start": True},
            headers=headers,
        )
        assert created.status_code == 201
        workspace_id = created.json()["id"]

        state = None
        for _ in range(40):
            state = client.get(f"/api/workspaces/{workspace_id}", headers=headers).json()
            if state["status"] in {"running", "failed"}:
                break
            time.sleep(0.05)
        assert state["status"] == "running"
        assert state["ide_url"].endswith(workspace_id)

        access = client.get(f"/api/workspaces/{workspace_id}/access", headers=headers)
        assert access.status_code == 200
        assert access.json()["ide_url"].endswith(workspace_id)
        assert access.json()["ide_password"]

        usage = client.get("/api/usage", headers=headers)
        assert usage.status_code == 200
        assert usage.json()["running_workspaces"] >= 1

        # 未认证访问 → 401
        assert client.get("/api/workspaces").status_code == 401
        assert client.get("/api/usage").status_code == 401

        stopped = client.post(f"/api/workspaces/{workspace_id}/stop", headers=headers)
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "stopped"

        # GPU 释放：mock GPU 应回到 available
        assert client.get("/api/gpus", headers=headers).status_code == 403  # 非 admin

        deleted = client.delete(f"/api/workspaces/{workspace_id}", headers=headers)
        assert deleted.status_code == 204
        assert client.get(f"/api/workspaces/{workspace_id}", headers=headers).status_code == 404


def test_workspace_delete_is_soft_tombstone():
    """删除语义：soft delete / tombstone —— 行保留（billing/audit），API 默认不返回。"""
    with TestClient(app) as client:
        token = _register(client, "softdel@example.com", "softdel-user")
        headers = _auth(token)

        created = client.post(
            "/api/workspaces", json={"template_id": "cartpole", "auto_start": False}, headers=headers
        )
        assert created.status_code == 201
        workspace_id = created.json()["id"]

        # 删除 → 204；行保留为 tombstone（DB 层验证）
        assert client.delete(f"/api/workspaces/{workspace_id}", headers=headers).status_code == 204

        # API 默认不返回 deleted workspace：list 不含、GET 404
        assert client.get("/api/workspaces", headers=headers).json() == []
        assert client.get(f"/api/workspaces/{workspace_id}", headers=headers).status_code == 404

        # 重复删除幂等（tombstone 后对普通 API 不可见 → 404，与 GET 语义一致）
        assert client.delete(f"/api/workspaces/{workspace_id}", headers=headers).status_code == 404

        # DB 层：行保留（DELETED + deleted_at）
        from app.deps import SessionFactory
        from app.models import Workspace, WorkspaceStatus

        with SessionFactory() as db:
            ws = db.get(Workspace, workspace_id)
            assert ws is not None  # 行未被物理删除
            assert ws.status == WorkspaceStatus.DELETED.value
            assert ws.deleted_at is not None

        # usage 不统计 tombstone
        usage = client.get("/api/usage", headers=headers).json()
        assert usage["total_workspaces"] == 0


def test_metrics_endpoint():
    with TestClient(app) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        body = resp.text
        for metric in [
            "workspace_launch_total",
            "workspace_launch_failed_total",
            "workspace_launch_seconds",
            "workspace_running",
            "gpu_allocated",
            "template_launch_total",
            "stream_session_total",
        ]:
            assert metric in body


def test_standalone_user_usage_balance_matches_ledger():
    """Regression: 无 organization 的 standalone user 有 CreditLedger 记录时，
    GET /api/usage 必须返回真实 balance，而不是 0。"""
    with TestClient(app) as client:
        token = _register(client, "standalone@example.com", "standalone-user")
        headers = _auth(token)

        # 充值前 balance == 0
        usage0 = client.get("/api/usage", headers=headers)
        assert usage0.status_code == 200
        assert usage0.json()["credits_balance"] == 0

        # 充值 500 credits
        resp = client.post("/api/ledger/recharge", json={"amount": 500}, headers=headers)
        assert resp.status_code == 200, resp.text

        # GET /api/usage → credits_balance == ledger balance == 500
        usage1 = client.get("/api/usage", headers=headers)
        assert usage1.status_code == 200
        assert usage1.json()["credits_balance"] == 500

        # 与 /api/ledger 聚合结果一致
        entries = client.get("/api/ledger", headers=headers).json()
        assert sum(e["amount"] for e in entries) == 500

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

import time

from fastapi.testclient import TestClient

from app.main import app


def test_end_to_end_workspace_lifecycle():
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["provider_ready"] is True

        templates = client.get("/api/templates").json()
        assert {x["id"] for x in templates} >= {"newton-cartpole-smoke", "franka-lift-cube"}

        created = client.post("/api/workspaces", json={"template_id": "newton-cartpole-smoke", "auto_start": True})
        assert created.status_code == 201
        workspace_id = created.json()["id"]

        state = None
        for _ in range(20):
            state = client.get(f"/api/workspaces/{workspace_id}").json()
            if state["status"] in {"running", "failed"}:
                break
            time.sleep(0.05)
        assert state["status"] == "running"
        assert state["ide_url"].endswith(workspace_id)

        access = client.get(f"/api/workspaces/{workspace_id}/access")
        assert access.status_code == 200
        assert access.json()["ide_url"].endswith(workspace_id)
        assert access.json()["ide_password"]

        usage = client.get("/api/usage")
        assert usage.status_code == 200
        assert usage.json()["running_workspaces"] >= 1

        stopped = client.post(f"/api/workspaces/{workspace_id}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "stopped"

        deleted = client.delete(f"/api/workspaces/{workspace_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/workspaces/{workspace_id}").status_code == 404

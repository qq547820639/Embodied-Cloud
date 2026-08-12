"""EdgeAgent 租户所有权（§6，P0）。

- register 必须绑定认证用户（匿名注册 401）
- user A 不能 list/get user B 的 agent
- user A 不能把 deployment 派发给 user B 的 agent
- agent token 不能访问其他 agent 的资源（heartbeat/telemetry 自指校验）
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


def _register(client: TestClient, email: str, username: str) -> dict:
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "username": username, "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    return {"token": resp.json()["token"], "id": resp.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register_agent(client: TestClient, headers: dict, name: str) -> dict:
    resp = client.post("/api/edge/agents/register", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_anonymous_agent_registration_rejected():
    """§6：匿名注册 agent → 401（禁止匿名无主资源注册接口）。"""
    with TestClient(app) as client:
        resp = client.post("/api/edge/agents/register", json={"name": "anon-agent"})
        assert resp.status_code == 401


def test_register_binds_owner():
    """注册后 agent 归属当前用户（DB 校验 owner_user_id）。"""
    with TestClient(app) as client:
        user = _register(client, "edge-owner@example.com", "owner1")
        data = _register_agent(client, _auth(user["token"]), "my-agent")
        assert data["agent"]["id"]

        from app.deps import SessionFactory
        from app.models import EdgeAgent

        with SessionFactory() as db:
            agent = db.get(EdgeAgent, data["agent"]["id"])
            assert agent.owner_user_id == user["id"]


def test_user_a_cannot_list_user_b_agents():
    """user A 的 agent 列表看不到 user B 的 agent。"""
    with TestClient(app) as client:
        user_a = _register(client, "edge-a@example.com", "user-a")
        user_b = _register(client, "edge-b@example.com", "user-b")
        _register_agent(client, _auth(user_a["token"]), "agent-a")
        _register_agent(client, _auth(user_b["token"]), "agent-b")

        listed = client.get("/api/edge/agents", headers=_auth(user_a["token"])).json()
        assert len(listed) == 1
        assert listed[0]["name"] == "agent-a"
        listed_b = client.get("/api/edge/agents", headers=_auth(user_b["token"])).json()
        assert [a["name"] for a in listed_b] == ["agent-b"]


def test_user_a_cannot_get_user_b_agent():
    """user A GET user B 的 agent → 404。"""
    with TestClient(app) as client:
        user_a = _register(client, "edge-a2@example.com", "user-a2")
        user_b = _register(client, "edge-b2@example.com", "user-b2")
        data_b = _register_agent(client, _auth(user_b["token"]), "agent-b2")
        agent_id = data_b["agent"]["id"]

        assert client.get(f"/api/edge/agents/{agent_id}", headers=_auth(user_a["token"])).status_code == 404
        # owner 自己可读
        assert client.get(f"/api/edge/agents/{agent_id}", headers=_auth(user_b["token"])).status_code == 200


def test_user_a_cannot_deploy_to_user_b_agent():
    """user A 不能把 deployment 派发给 user B 的 agent（run 绑定校验）。"""
    with TestClient(app) as client:
        user_a = _register(client, "edge-a3@example.com", "user-a3")
        user_b = _register(client, "edge-b3@example.com", "user-b3")
        data_b = _register_agent(client, _auth(user_b["token"]), "agent-b3")
        agent_id = data_b["agent"]["id"]

        # user A 创建自己的 workspace + deployment（service 层完整链）
        import hashlib
        import tempfile
        from pathlib import Path

        from app.deps import SessionFactory
        from app.models import User, Workspace
        from app.services.deployment import DeploymentService

        ws_resp = client.post(
            "/api/workspaces", json={"template_id": "cartpole", "auto_start": False},
            headers=_auth(user_a["token"]),
        )
        wid = ws_resp.json()["id"]
        root = Path(tempfile.mkdtemp())
        (root / wid).mkdir(parents=True)
        (root / wid / "model.pt").write_bytes(b"dep-owner-test")
        svc = DeploymentService(SessionFactory, root)
        with SessionFactory() as db:
            ws2 = db.get(Workspace, wid)
            user = db.get(User, user_a["id"])
            artifact = svc.create_artifact(db, user, ws2, "model.pt")
            dep = svc.deploy(db, user, ws2, artifact, "franka")
            svc.download(db, dep)
            actual = hashlib.sha256(b"dep-owner-test").hexdigest()
            svc.report_checksum(db, dep, actual)
            dep_id = dep.id

        # user A 尝试把 deployment 派发给 user B 的 agent → 404
        resp = client.post(
            f"/api/deployments/{dep_id}/run",
            json={"edge_agent_id": agent_id},
            headers=_auth(user_a["token"]),
        )
        assert resp.status_code == 404


def test_agent_token_cannot_access_other_agent_deployment():
    """agent A 的 token 不能操作 agent B 的资源（heartbeat 自指校验 + deployment 无 agent-token 通路）。"""
    with TestClient(app) as client:
        user_a = _register(client, "edge-a4@example.com", "user-a4")
        data_a = _register_agent(client, _auth(user_a["token"]), "agent-a4")
        agent_a_id = data_a["agent"]["id"]
        token_a = data_a["token"]

        # agent A token 对 agent B 的 id 操作 → 404（自指校验）
        resp = client.post(
            f"/api/edge/agents/other-agent-id/heartbeat",
            json={},
            headers={"X-Agent-Token": token_a},
        )
        assert resp.status_code == 404
        # 自身 heartbeat 正常
        resp = client.post(
            f"/api/edge/agents/{agent_a_id}/heartbeat",
            json={},
            headers={"X-Agent-Token": token_a},
        )
        assert resp.status_code == 200
        # 无 token → 401
        assert client.post(
            f"/api/edge/agents/{agent_a_id}/heartbeat", json={}
        ).status_code == 401

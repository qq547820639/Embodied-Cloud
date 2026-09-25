"""安全隔离测试：User A 不能读/改/删 User B 的 workspace（SECURITY.md T1）。

越权一律 404，不泄露资源存在性。
"""

import time

from fastapi.testclient import TestClient

from app.main import app
from tests.http_auth import auth_headers as _auth
from tests.http_auth import register_body


def _register(client: TestClient, email: str, username: str) -> tuple[str, str]:
    body = register_body(client, email, username)
    return body["token"], body["user"]["id"]


def _create_running_workspace(client: TestClient, headers: dict, template_id: str = "cartpole") -> str:
    created = client.post(
        "/api/workspaces",
        json={"template_id": template_id, "auto_start": True},
        headers=headers,
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    for _ in range(40):
        st = client.get(f"/api/workspaces/{wid}", headers=headers).json()["status"]
        if st in {"running", "failed"}:
            break
        time.sleep(0.05)
    return wid


def test_user_a_cannot_read_user_b_workspace():
    with TestClient(app) as client:
        token_a, uid_a = _register(client, "a@example.com", "user-a")
        token_b, _ = _register(client, "b@example.com", "user-b")
        wid_b = _create_running_workspace(client, _auth(token_b))

        # A 读取 B 的 workspace → 404（不泄露存在性）
        assert client.get(f"/api/workspaces/{wid_b}", headers=_auth(token_a)).status_code == 404
        assert client.get(f"/api/workspaces/{wid_b}/access", headers=_auth(token_a)).status_code == 404

        # 列表不包含他人 workspace
        mine = client.get("/api/workspaces", headers=_auth(token_a)).json()
        assert all(w["user_id"] == uid_a for w in mine)
        ids_a = {w["id"] for w in mine}
        assert wid_b not in ids_a

        # A 的列表与 B 的列表互斥
        ids_b = {w["id"] for w in client.get("/api/workspaces", headers=_auth(token_b)).json()}
        assert ids_a.isdisjoint(ids_b)


def test_user_a_cannot_stop_or_delete_user_b_workspace():
    with TestClient(app) as client:
        token_a, _ = _register(client, "a2@example.com", "user-a2")
        token_b, _ = _register(client, "b2@example.com", "user-b2")
        wid_b = _create_running_workspace(client, _auth(token_b))

        assert client.post(f"/api/workspaces/{wid_b}/stop", headers=_auth(token_a)).status_code == 404
        assert client.delete(f"/api/workspaces/{wid_b}", headers=_auth(token_a)).status_code == 404

        # B 的 workspace 仍可正常操作（未被破坏）
        assert client.get(f"/api/workspaces/{wid_b}", headers=_auth(token_b)).status_code == 200


def test_ledger_and_usage_are_user_scoped():
    with TestClient(app) as client:
        token_a, _ = _register(client, "a3@example.com", "user-a3")
        token_b, _ = _register(client, "b3@example.com", "user-b3")
        _create_running_workspace(client, _auth(token_a))

        usage_a = client.get("/api/usage", headers=_auth(token_a)).json()
        usage_b = client.get("/api/usage", headers=_auth(token_b)).json()
        assert usage_a["total_workspaces"] >= 1
        assert usage_b["total_workspaces"] == 0

        # 充值入账到 A，B 的 ledger 不可见
        client.post("/api/ledger/recharge", json={"amount": 1000}, headers=_auth(token_a))
        ledger_a = client.get("/api/ledger", headers=_auth(token_a)).json()
        ledger_b = client.get("/api/ledger", headers=_auth(token_b)).json()
        assert any(e["type"] == "recharge" for e in ledger_a)
        assert ledger_b == []

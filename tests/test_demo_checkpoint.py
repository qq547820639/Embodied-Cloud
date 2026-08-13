"""demo-checkpoint（mock 演示模式的模拟训练产出）端点测试。

让 Sim2Real 部署流程（artifact → checksum → deploy → edge 校验）在无真实
GPU 训练的演示环境可端到端走通；非 mock provider 必须拒绝（禁止伪造真实产出）。
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app
from tests.test_demo_workspace import _auth, _register

WORKSPACE_ROOT = Path("/tmp/test-embodiedcloud-workspaces")  # noqa: S108 conftest 指定的测试隔离目录


def test_demo_checkpoint_creates_verifiable_file():
    with TestClient(app) as client:
        token = _register(client, "ckpt@example.com", "ckpt")
        created = client.post(
            "/api/workspaces",
            json={"template_id": "cartpole", "auto_start": False},
            headers=_auth(token),
        )
        assert created.status_code == 201
        wid = created.json()["id"]

        resp = client.post(f"/api/workspaces/{wid}/demo-checkpoint", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["path"] == "outputs/run_0/checkpoint.pt"
        file = WORKSPACE_ROOT / wid / payload["path"]
        assert file.is_file()
        data = file.read_bytes()
        assert len(data) == payload["size_bytes"]
        assert hashlib.sha256(data).hexdigest() == payload["sha256"]


def test_demo_checkpoint_owner_scope_404():
    with TestClient(app) as client:
        token_a = _register(client, "ckpt-a@example.com", "ckpt-a")
        token_b = _register(client, "ckpt-b@example.com", "ckpt-b")
        created = client.post(
            "/api/workspaces",
            json={"template_id": "cartpole", "auto_start": False},
            headers=_auth(token_a),
        )
        wid = created.json()["id"]
        resp = client.post(f"/api/workspaces/{wid}/demo-checkpoint", headers=_auth(token_b))
        assert resp.status_code == 404


def test_demo_checkpoint_rejected_on_non_mock_provider(monkeypatch):
    """真实 provider 下禁止伪造产出：400（必须用真实训练 checkpoint）。"""
    from app.deps import orchestrator

    with TestClient(app) as client:
        token = _register(client, "ckpt-d@example.com", "ckpt-d")
        created = client.post(
            "/api/workspaces",
            json={"template_id": "cartpole", "auto_start": False},
            headers=_auth(token),
        )
        wid = created.json()["id"]
        monkeypatch.setattr(orchestrator, "provider", SimpleNamespace(name="docker"))
        resp = client.post(f"/api/workspaces/{wid}/demo-checkpoint", headers=_auth(token))
        assert resp.status_code == 400

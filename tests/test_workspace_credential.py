"""Workspace 凭据安全（§20）：DB 不长期保存明文密码。

- provision 后 DB 中 password 为 Fernet 密文（enc: 前缀）
- access endpoint 解密返回明文（仅合法 owner）
- 明文不出现于 DB 存储值；日志脱敏已有 REDACT_KEYS 覆盖
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import app
from app.models import Template, Workspace, WorkspaceStatus
from app.security import WorkspaceCredentialCipher
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler

ENGINE = create_engine("sqlite:///./test-credential.db", connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)
KEY = "test-credential-key-0001"


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


def _seed(db) -> None:
    GpuScheduler(Factory).sync_host(
        db,
        host_id="host-1",
        name="h1",
        address="127.0.0.1",
        provider="mock",
        gpus=[GpuInfo(gpu_uuid="gpu-1", model="RTX", memory_total=24564, index=0)],
    )
    t = Template(
        id="cartpole",
        slug="cartpole",
        name="cartpole",
        description="test",
        category="test",
        runtime="mock",
        launch_command="echo ok",
        enabled=True,
        recommended_vram_gb=16,
        estimated_hourly_cost_cny=1.0,
    )
    db.add(t)
    db.commit()


def test_db_stores_encrypted_password_not_plaintext():
    """provision 后 DB 中的 password 是密文（enc: 前缀），不是明文。"""
    cipher = WorkspaceCredentialCipher(KEY)
    orchestrator = WorkspaceOrchestrator(
        Factory, MockProvider("http://127.0.0.1:8000"), Path("/tmp/test-cred-ws"),  # noqa: S108
        credential_cipher=cipher,
    )
    with Factory() as db:
        _seed(db)
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = ws.id
        orchestrator._start(wid)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value, ws.error_message
        assert ws.password is not None
        # 密文存储：不以明文形式出现，可解密回明文
        assert not ws.password.startswith("mock-")  # 不是 mock provider 原始明文
        assert ws.password.startswith("enc:")
        plaintext = cipher.decrypt(ws.password)
        assert plaintext is not None and len(plaintext) >= 8


def test_access_endpoint_returns_plaintext_password():
    """access endpoint 对合法 owner 返回解密后的明文密码。"""
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/register",
            json={"email": "cred@example.com", "username": "cred", "password": "password123"},
        )
        token = resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        created = client.post(
            "/api/workspaces", json={"template_id": "cartpole", "auto_start": True}, headers=headers
        )
        assert created.status_code == 201
        workspace_id = created.json()["id"]

        # 等待 RUNNING
        state = None
        for _ in range(40):
            state = client.get(f"/api/workspaces/{workspace_id}", headers=headers).json()
            if state["status"] in {"running", "failed"}:
                break
            import time

            time.sleep(0.05)
        assert state["status"] == "running"

        access = client.get(f"/api/workspaces/{workspace_id}/access", headers=headers)
        assert access.status_code == 200
        password = access.json()["ide_password"]
        # 返回明文（可登录密码），不是 enc: 密文
        assert password and not password.startswith("enc:")
        assert len(password) >= 8

        # 越权用户拿不到
        resp2 = client.post(
            "/api/auth/register",
            json={"email": "other@example.com", "username": "other", "password": "password123"},
        )
        headers2 = {"Authorization": f"Bearer {resp2.json()['token']}"}
        assert client.get(f"/api/workspaces/{workspace_id}/access", headers=headers2).status_code == 404


def test_legacy_plaintext_password_still_readable():
    """兼容迁移期：DB 中的旧明文密码仍可读取（解密失败回退明文）。"""
    cipher = WorkspaceCredentialCipher(KEY)
    with Factory() as db:
        _seed(db)
        ws = Workspace(
            id="ws-legacy",
            name="w",
            template_id="cartpole",
            provider="mock",
            user_id="u1",
            status=WorkspaceStatus.RUNNING.value,
            password="legacy-plaintext-password",  # noqa: S106 旧数据明文（测试）
        )
        db.add(ws)
        db.commit()
        stored = ws.password
        # 迁移期回退：decrypt 返回 None，调用方按明文处理
        assert cipher.decrypt(stored) is None

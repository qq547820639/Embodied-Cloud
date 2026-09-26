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
from app.security import (
    CredentialDecryptError,
    WorkspaceCredentialCipher,
    validate_credential_configuration,
)
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("credential"), connect_args={"check_same_thread": False})
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
    from app.deps import SessionFactory, scheduler
    from tests.gpu_pool import ensure_free_gpus

    with TestClient(app) as client:
        # 这一支要真起一个 workspace：空闲卡是前提，得自己达成并写明（见 tests/gpu_pool.py）
        with SessionFactory() as db:
            ensure_free_gpus(db, scheduler)
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
        assert state["status"] == "running", (
            f"provision 没收在 running：{state.get('error_message')}"
            "（'No GPU available' 说明共享库的 mock 卡池被上游用例占满，"
            "参见 tests/gpu_pool.py 与 tests/conftest.py 的守卫）"
        )

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
    """兼容迁移期：DB 中的旧明文密码仍可读取（resolve 原样返回）。"""
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
        # 非 enc: 前缀 = 迁移期旧明文 → resolve 原样返回（兼容读取）
        assert cipher.resolve(ws.password) == "legacy-plaintext-password"


def test_ciphertext_decrypt_failure_fails_closed():
    """§13 fail closed：enc: 密文解密失败必须抛错，禁止当明文返回。"""
    cipher = WorkspaceCredentialCipher(KEY)
    # 用另一个密钥加密 → 当前 cipher 无法解密
    other = WorkspaceCredentialCipher("different-key-0002")
    stored = other.encrypt("secret-password")
    assert stored.startswith("enc:")
    with pytest.raises(CredentialDecryptError):
        cipher.decrypt(stored)
    with pytest.raises(CredentialDecryptError):
        cipher.resolve(stored)


def test_production_provider_requires_explicit_credential_key():
    """§13：provider != mock 且未显式配置密钥 → 拒绝启动（禁止开发默认密钥）。"""
    with pytest.raises(RuntimeError, match="WORKSPACE_CREDENTIAL_KEY"):
        validate_credential_configuration(provider="docker", credential_key="")
    with pytest.raises(RuntimeError, match="WORKSPACE_CREDENTIAL_KEY"):
        validate_credential_configuration(provider="k8s", credential_key="")
    # mock 允许（开发/演示）
    validate_credential_configuration(provider="mock", credential_key="")
    # 生产 + 显式密钥 + 显式 pepper → 允许
    validate_credential_configuration(
        provider="docker", credential_key="explicit-key",
        password_pepper="pepper",  # noqa: S106 测试 pepper，非真实密码
    )


def test_production_requires_explicit_password_pepper():
    """§S-1：provider != mock 且 password_pepper 为空 → 拒绝启动（fail-closed）。"""
    with pytest.raises(RuntimeError, match="PASSWORD_PEPPER"):
        validate_credential_configuration(
            provider="docker", credential_key="explicit-key", password_pepper=""
        )
    with pytest.raises(RuntimeError, match="PASSWORD_PEPPER"):
        validate_credential_configuration(
            provider="k8s", credential_key="explicit-key", password_pepper=""
        )
    # mock + 空 pepper → 不拒绝（开发/演示）
    validate_credential_configuration(
        provider="mock", credential_key="", password_pepper=""
    )
    # 生产 + 显式 pepper → 允许
    validate_credential_configuration(
        provider="docker", credential_key="explicit-key",
        password_pepper="pepper",  # noqa: S106 测试 pepper，非真实密码
    )


def test_auto_create_tables_warns_but_does_not_reject(caplog):
    """§S-1：生产 auto_create_tables=true → 仅 logger.warning，不拒绝启动。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="embodiedcloud"):
        validate_credential_configuration(
            provider="docker",
            credential_key="explicit-key",
            password_pepper="pepper",  # noqa: S106 测试 pepper，非真实密码
            auto_create_tables=True,
        )
    assert any("auto_create_tables" in r.message for r in caplog.records)

    # auto_create_tables=false → 不产生告警
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="embodiedcloud"):
        validate_credential_configuration(
            provider="docker",
            credential_key="explicit-key",
            password_pepper="pepper",  # noqa: S106 测试 pepper，非真实密码
            auto_create_tables=False,
        )
    assert all("auto_create_tables" not in r.message for r in caplog.records)

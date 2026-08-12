"""Deployment checksum verification bypass 回归测试（§5，P0）。

唯一 VERIFIED 路径必须是：Edge 下载真实 bytes → 本地 SHA256 → 上报
actual_sha256 → server 比较 expected → MATCH → VERIFIED。
download 端点绝不能自动进入 VERIFIED。
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    DeploymentStatus,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceStatus,
)
from app.services.deployment import DeploymentService

ENGINE = create_engine("sqlite:///./test-deploy-bypass.db", connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


def _setup(db, tmp_path: Path) -> tuple[DeploymentService, User, Workspace]:
    owner = User(id="u1", email="u1@example.com", username="u1", password_hash="x", role=Role.USER.value)  # noqa: S106
    db.add(owner)
    db.flush()
    t = Template(
        id="cartpole", slug="cartpole", name="t", description="d", category="c",
        runtime="mock", launch_command="", enabled=True, version="0.1.0",
        recommended_vram_gb=16, estimated_hourly_cost_cny=1.0,
    )
    db.add(t)
    ws = Workspace(
        id="ws-1", name="w", template_id="cartpole", provider="mock",
        user_id="u1", status=WorkspaceStatus.RUNNING.value,
    )
    db.add(ws)
    db.commit()
    root = tmp_path / "root"
    (root / "ws-1").mkdir(parents=True)
    (root / "ws-1" / "model.pt").write_bytes(b"bypass-test-bytes")
    return DeploymentService(Factory, root), owner, ws


def _deploy_downloading(db, svc, owner, ws):
    artifact = svc.create_artifact(db, owner, ws, "model.pt")
    d = svc.deploy(db, owner, ws, artifact, "franka")
    svc.download(db, d)
    return d


def test_download_does_not_verify(tmp_path):
    """download 只能进入 DOWNLOADING，绝不能自动 VERIFIED（bypass 回归）。"""
    with Factory() as db:
        svc, owner, ws = _setup(db, tmp_path)
        d = _deploy_downloading(db, svc, owner, ws)
        # 再次 download（幂等）后仍必须是 DOWNLOADING
        svc.download(db, d)
        assert d.status == DeploymentStatus.DOWNLOADING.value
        assert d.status != DeploymentStatus.VERIFIED.value


def test_verify_requires_reported_checksum(tmp_path):
    """旧的无校验 verify()（DOWNLOADING→VERIFIED）不得再存在/不可达。"""
    with Factory() as db:
        svc, owner, ws = _setup(db, tmp_path)
        d = _deploy_downloading(db, svc, owner, ws)
        # 直接调旧 verify() 必须失败（方法已移除 → AttributeError）
        assert not hasattr(svc, "verify"), "verify() bypass 必须删除"
        assert d.status == DeploymentStatus.DOWNLOADING.value


def test_wrong_checksum_fails(tmp_path):
    """上报错误 checksum → FAILED。"""
    with Factory() as db:
        svc, owner, ws = _setup(db, tmp_path)
        d = _deploy_downloading(db, svc, owner, ws)
        result = svc.report_checksum(db, d, "0" * 64)
        assert result.status == DeploymentStatus.FAILED.value
        assert "edge reported" in result.error_message


def test_tampered_artifact_fails(tmp_path):
    """篡改 artifact：edge 算出的 sha256 ≠ expected → FAILED。"""
    import hashlib

    with Factory() as db:
        svc, owner, ws = _setup(db, tmp_path)
        d = _deploy_downloading(db, svc, owner, ws)
        tampered = hashlib.sha256(b"TAMPERED-BYTES").hexdigest()
        result = svc.report_checksum(db, d, tampered)
        assert result.status == DeploymentStatus.FAILED.value


def test_duplicate_checksum_report_is_idempotent(tmp_path):
    """VERIFIED 后重复上报（含错误值）→ 幂等不变。"""
    import hashlib

    with Factory() as db:
        svc, owner, ws = _setup(db, tmp_path)
        d = _deploy_downloading(db, svc, owner, ws)
        actual = hashlib.sha256(b"bypass-test-bytes").hexdigest()
        svc.report_checksum(db, d, actual)
        assert d.status == DeploymentStatus.VERIFIED.value
        again = svc.report_checksum(db, d, "0" * 64)
        assert again.status == DeploymentStatus.VERIFIED.value


def test_cross_deployment_checksum_is_rejected(tmp_path):
    """跨部署错误上报（无关 checksum）→ FAILED。"""
    with Factory() as db:
        svc, owner, ws = _setup(db, tmp_path)
        d = _deploy_downloading(db, svc, owner, ws)
        result = svc.report_checksum(db, d, "f" * 64)
        assert result.status == DeploymentStatus.FAILED.value


def test_api_download_endpoint_does_not_verify(tmp_path):
    """API 层：POST /deployments/{id}/download 后状态仍是 downloading（bypass 修复）。"""
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with Factory() as db:
        svc, owner, ws = _setup(db, tmp_path)
        d = _deploy_downloading(db, svc, owner, ws)
        d.status = DeploymentStatus.PENDING.value
        db.commit()

    with TestClient(fastapi_app) as client:
        resp = client.post(
            "/api/auth/register",
            json={"email": "bypass@example.com", "username": "bypass", "password": "password123"},
        )
        headers = {"Authorization": f"Bearer {resp.json()['token']}"}
        # 通过 API 创建属于该用户的 workspace + artifact + deployment
        # （owner 校验需要 workspace.user_id == 当前用户，重新走 API 建链）
        ws_resp = client.post(
            "/api/workspaces", json={"template_id": "cartpole", "auto_start": False}, headers=headers
        )
        wid = ws_resp.json()["id"]
        # 直接构造 artifact 文件（API 上传路径为后续；这里验证 download 语义）
        from app.deps import SessionFactory
        from app.models import Artifact, DeploymentRecord

        with SessionFactory() as db2:
            ws2 = db2.get(Workspace, wid)
            art = Artifact(
                id="art-api-1",
                workspace_id=wid,
                name="model.pt",
                path="model.pt",
                object_key=f"{wid}/model.pt",
                checksum="0" * 64,
                size_bytes=1,
            )
            db2.add(art)
            d2 = DeploymentRecord(
                id="dep-api-1",
                workspace_id=wid,
                artifact_id="art-api-1",
                model_version="0.1.0",
                template_version="0.1.0",
                robot_type="franka",
                checksum="0" * 64,
                status=DeploymentStatus.PENDING.value,
            )
            db2.add(d2)
            db2.commit()

        resp = client.post("/api/deployments/dep-api-1/download", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "downloading"
        assert resp.json()["status"] != "verified"

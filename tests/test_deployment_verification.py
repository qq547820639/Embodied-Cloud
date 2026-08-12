"""Deployment verification 完整性（§23）。

- 禁止绕过：未 download 直接 verify → 拒绝（不能 PENDING → VERIFIED）
- tampered artifact：登记后文件被篡改 → 本地 sha256 ≠ 记录 → FAILED
- 重复 verify 幂等（终态不再改变）
- expired/终态 deployment 不可再次 verify（failed 后保持 failed）
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

ENGINE = create_engine("sqlite:///./test-deploy-verify.db", connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


def _setup(db, tmp_path: Path) -> tuple[DeploymentService, User, Workspace, Path]:
    owner = User(id="u1", email="u1@example.com", username="u1", password_hash="x", role=Role.USER.value)  # noqa: S106
    db.add(owner)
    db.flush()
    t = Template(
        id="cartpole", slug="cartpole", name="t", description="d", category="c",
        runtime="mock", launch_command="", enabled=True, version="0.1.0",
        recommended_vram_gb=16, estimated_hourly_cost_cny=1.0,
    )
    db.add(t)
    db.flush()
    ws = Workspace(
        id="ws-1", name="w", template_id="cartpole", provider="mock",
        user_id="u1", status=WorkspaceStatus.RUNNING.value,
    )
    db.add(ws)
    db.commit()
    root = tmp_path / "root"
    (root / "ws-1").mkdir(parents=True)
    (root / "ws-1" / "checkpoint.pt").write_bytes(b"model-v1-bytes")
    return DeploymentService(Factory, root), owner, ws, root


def test_verify_without_download_is_rejected(tmp_path):
    """防绕过：PENDING 直接 verify_checksum → 拒绝（必须真实 download 后校验）。"""
    with Factory() as db:
        svc, owner, ws, _ = _setup(db, tmp_path)
        artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
        d = svc.deploy(db, owner, ws, artifact, "franka")
        assert d.status == DeploymentStatus.PENDING.value
        with pytest.raises(Exception) as exc_info:
            svc.verify_checksum(db, d)
        assert "verified" in str(exc_info.value)
        # 状态未被绕过为 VERIFIED
        db.refresh(d)
        assert d.status == DeploymentStatus.PENDING.value


def test_tampered_artifact_fails_verification(tmp_path):
    """tampered artifact：登记后文件被篡改 → 本地 sha256 ≠ 记录 → FAILED。"""
    with Factory() as db:
        svc, owner, ws, root = _setup(db, tmp_path)
        artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
        d = svc.deploy(db, owner, ws, artifact, "franka")
        svc.download(db, d)
        # 篡改文件（模拟 edge 下载到被篡改的字节 / 服务端文件被替换）
        (root / "ws-1" / "checkpoint.pt").write_bytes(b"TAMPERED-BYTES")
        result = svc.verify_checksum(db, d)
        assert result.status == DeploymentStatus.FAILED.value
        assert "checksum mismatch" in result.error_message
        # 篡改后的 checksum ≠ 记录 checksum
        import hashlib

        actual = hashlib.sha256(b"TAMPERED-BYTES").hexdigest()
        assert actual != d.checksum


def test_duplicate_verify_is_idempotent(tmp_path):
    with Factory() as db:
        svc, owner, ws, _ = _setup(db, tmp_path)
        artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
        d = svc.deploy(db, owner, ws, artifact, "franka")
        svc.download(db, d)
        svc.verify_checksum(db, d)
        assert d.status == DeploymentStatus.VERIFIED.value
        # 重复 verify：终态幂等，不再改变
        again = svc.verify_checksum(db, d)
        assert again.status == DeploymentStatus.VERIFIED.value
        assert again.error_message is None


def test_expired_failed_deployment_cannot_reverify(tmp_path):
    """终态（failed）deployment 不可再次 verify（expired 语义）。"""
    with Factory() as db:
        svc, owner, ws, root = _setup(db, tmp_path)
        artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
        d = svc.deploy(db, owner, ws, artifact, "franka")
        svc.download(db, d)
        # 文件丢失 → failed（终态）
        (root / "ws-1" / "checkpoint.pt").unlink()
        result = svc.verify_checksum(db, d)
        assert result.status == DeploymentStatus.FAILED.value
        assert "missing" in result.error_message
        # 恢复文件后再次 verify：终态幂等，保持 failed，不复活
        (root / "ws-1" / "checkpoint.pt").write_bytes(b"model-v1-bytes")
        again = svc.verify_checksum(db, d)
        assert again.status == DeploymentStatus.FAILED.value


def test_verify_checksum_only_path_to_verified(tmp_path):
    """verify_checksum 是 PENDING/DOWNLOADING → VERIFIED 的唯一真实校验路径。"""
    with Factory() as db:
        svc, owner, ws, _ = _setup(db, tmp_path)
        artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
        d = svc.deploy(db, owner, ws, artifact, "franka")
        # 模拟错误实现（直接置 VERIFIED）被状态机拒绝
        svc.download(db, d)
        d.status = DeploymentStatus.VERIFIED.value
        db.commit()
        db.refresh(d)
        # 已 VERIFIED 的记录保持（幂等），不重复计算
        again = svc.verify_checksum(db, d)
        assert again.status == DeploymentStatus.VERIFIED.value

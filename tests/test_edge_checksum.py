"""Edge 上报 checksum 协议（§10）。

- tampered download：edge 本地算出的 sha256 ≠ expected → FAILED
- wrong checksum：同（错误值上报）
- duplicate report：VERIFIED 后重复上报 → 幂等不变
- replay：FAILED 后上报正确值 → 不得复活
- cross-deployment checksum：把 A 的 checksum 上报给 B → FAILED
- 防绕过：未 download 直接上报 → 拒绝；客户端不能直接置 VERIFIED
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
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("edge-checksum"), connect_args={"check_same_thread": False})
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
    ws = Workspace(
        id="ws-1", name="w", template_id="cartpole", provider="mock",
        user_id="u1", status=WorkspaceStatus.RUNNING.value,
    )
    db.add(ws)
    db.commit()
    root = tmp_path / "root"
    (root / "ws-1").mkdir(parents=True)
    (root / "ws-1" / "model.pt").write_bytes(b"edge-model-bytes")
    return DeploymentService(Factory, root), owner, ws, root


def _deploy(db, svc, owner, ws, robot="franka"):
    artifact = svc.create_artifact(db, owner, ws, "model.pt")
    d = svc.deploy(db, owner, ws, artifact, robot)
    svc.download(db, d)
    return d


def test_edge_reports_matching_checksum_verifies(tmp_path):
    import hashlib

    with Factory() as db:
        svc, owner, ws, _ = _setup(db, tmp_path)
        d = _deploy(db, svc, owner, ws)
        actual = hashlib.sha256(b"edge-model-bytes").hexdigest()
        result = svc.report_checksum(db, d, actual)
        assert result.status == DeploymentStatus.VERIFIED.value


def test_tampered_download_fails(tmp_path):
    import hashlib

    with Factory() as db:
        svc, owner, ws, _ = _setup(db, tmp_path)
        d = _deploy(db, svc, owner, ws)
        tampered = hashlib.sha256(b"EDGE-TAMPERED-BYTES").hexdigest()  # edge 本地算出的错误值
        result = svc.report_checksum(db, d, tampered)
        assert result.status == DeploymentStatus.FAILED.value
        assert "edge reported" in result.error_message
        assert tampered != d.checksum


def test_wrong_checksum_fails(tmp_path):
    with Factory() as db:
        svc, owner, ws, _ = _setup(db, tmp_path)
        d = _deploy(db, svc, owner, ws)
        result = svc.report_checksum(db, d, "0" * 64)
        assert result.status == DeploymentStatus.FAILED.value


def test_duplicate_report_idempotent(tmp_path):
    import hashlib

    with Factory() as db:
        svc, owner, ws, _ = _setup(db, tmp_path)
        d = _deploy(db, svc, owner, ws)
        actual = hashlib.sha256(b"edge-model-bytes").hexdigest()
        svc.report_checksum(db, d, actual)
        assert d.status == DeploymentStatus.VERIFIED.value
        # 重复上报（即使错误值）→ 终态幂等不变
        again = svc.report_checksum(db, d, "0" * 64)
        assert again.status == DeploymentStatus.VERIFIED.value


def test_replay_after_failed_does_not_resurrect(tmp_path):
    import hashlib

    with Factory() as db:
        svc, owner, ws, _ = _setup(db, tmp_path)
        d = _deploy(db, svc, owner, ws)
        svc.report_checksum(db, d, "0" * 64)  # 先失败
        assert d.status == DeploymentStatus.FAILED.value
        # 之后即使上报正确 checksum → 不得复活（防 replay）
        actual = hashlib.sha256(b"edge-model-bytes").hexdigest()
        again = svc.report_checksum(db, d, actual)
        assert again.status == DeploymentStatus.FAILED.value


def test_cross_deployment_checksum_fails(tmp_path):
    """真正的跨部署误报：把 deployment B（不同 artifact/checksum）的 sha256
    上报给 deployment A → A 必须 FAILED（expected 属于 A 的 artifact）。"""
    import hashlib

    with Factory() as db:
        svc, owner, ws_a, root = _setup(db, tmp_path)
        # 第二个 workspace 持有不同内容的 artifact（不同 checksum）
        ws_b = Workspace(
            id="ws-2", name="w2", template_id="cartpole", provider="mock",
            user_id="u1", status=WorkspaceStatus.RUNNING.value,
        )
        db.add(ws_b)
        db.commit()
        (root / "ws-2").mkdir(parents=True)
        (root / "ws-2" / "model.pt").write_bytes(b"OTHER-MODEL-BYTES")

        d_a = _deploy(db, svc, owner, ws_a, robot="franka")
        d_b = _deploy(db, svc, owner, ws_b, robot="franka")
        assert d_a.checksum != d_b.checksum  # 两个 artifact 内容不同 → expected 不同

        # edge 把 B 的 artifact sha256 上报给 A（串台）→ A 校验失败
        wrong = hashlib.sha256(b"OTHER-MODEL-BYTES").hexdigest()
        result = svc.report_checksum(db, d_a, wrong)
        assert result.status == DeploymentStatus.FAILED.value
        assert "edge reported" in result.error_message
        # B 自己上报自己的值仍正常通过（互不影响）
        assert svc.report_checksum(db, d_b, wrong).status == DeploymentStatus.VERIFIED.value


def test_report_without_download_rejected(tmp_path):
    """防绕过：PENDING 直接上报 → 拒绝；客户端无法直接置 VERIFIED。"""
    with Factory() as db:
        svc, owner, ws, _ = _setup(db, tmp_path)
        artifact = svc.create_artifact(db, owner, ws, "model.pt")
        d = svc.deploy(db, owner, ws, artifact, "franka")  # 未 download
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            svc.report_checksum(db, d, d.checksum)  # 即使值正确也被拒绝
        db.refresh(d)
        assert d.status == DeploymentStatus.PENDING.value

"""DeploymentService 测试: artifact checksum / 幂等 deploy / 状态机 / 越权 (临时 SQLite)."""

import hashlib

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import DeploymentRecord, Template, User, Workspace
from app.services.deployment import DeploymentService
from app.services.edge import EdgeService
from app.services.providers.mock import MockProvider

CHECKPOINT_BYTES = b"model-bytes-v1"


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    yield sf
    engine.dispose()


@pytest.fixture()
def db(factory):
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _template(db, version="0.2.0"):
    tpl = Template(
        id="tpl-1",
        slug="franka-lift",
        name="Franka Lift",
        version=version,
        description="lift cube",
        category="manipulation",
        runtime="mock",
        entrypoint="",
        outputs=[],
        metadata_json={},
        launch_command="",
        recommended_vram_gb=16,
        estimated_hourly_cost_cny=1.0,
    )
    db.add(tpl)
    return tpl


def _user(db, user_id="u-1"):
    user = User(
        id=user_id,
        email=f"{user_id}@example.com",
        username=user_id,
        password_hash="x",  # noqa: S106 测试占位哈希
        role="user",
    )
    db.add(user)
    return user


def _workspace(db, user, ws_id="ws-1"):
    ws = Workspace(
        id=ws_id,
        name="ws",
        template_id="tpl-1",
        user_id=user.id,
        provider="mock",
        status="created",
    )
    db.add(ws)
    return ws


def _setup(db, tmp_path):
    _template(db)
    owner = _user(db)
    ws = _workspace(db, owner)
    db.commit()
    ws_dir = tmp_path / ws.id
    ws_dir.mkdir(parents=True)
    artifact_file = ws_dir / "checkpoint.pt"
    artifact_file.write_bytes(CHECKPOINT_BYTES)
    svc = DeploymentService(None, tmp_path)
    return svc, owner, ws, artifact_file


def test_create_artifact_computes_checksum(db, tmp_path):
    svc, owner, ws, _ = _setup(db, tmp_path)
    artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
    assert artifact.name == "checkpoint.pt"
    assert artifact.path == "checkpoint.pt"
    assert artifact.size_bytes == len(CHECKPOINT_BYTES)
    assert artifact.checksum == hashlib.sha256(CHECKPOINT_BYTES).hexdigest()
    assert artifact.model_version == "0.2.0"  # 来自 workspace 模板 version


def test_create_artifact_missing_file_404(db, tmp_path):
    svc, owner, ws, _ = _setup(db, tmp_path)
    with pytest.raises(HTTPException) as exc:
        svc.create_artifact(db, owner, ws, "missing.pt")
    assert exc.value.status_code == 404


def test_create_artifact_path_traversal_rejected(db, tmp_path):
    svc, owner, ws, _ = _setup(db, tmp_path)
    with pytest.raises(HTTPException) as exc:
        svc.create_artifact(db, owner, ws, "../outside.pt")
    assert exc.value.status_code == 400


def test_deploy_idempotent_for_same_artifact(db, tmp_path):
    svc, owner, ws, _ = _setup(db, tmp_path)
    artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
    d1 = svc.deploy(db, owner, ws, artifact, "franka")
    d2 = svc.deploy(db, owner, ws, artifact, "franka")
    assert d1.id == d2.id
    assert len(db.scalars(select(DeploymentRecord)).all()) == 1
    assert d1.status == "pending"
    assert d1.template_version == "0.2.0"
    assert d1.model_version == artifact.model_version
    assert d1.checksum == artifact.checksum


def test_deploy_different_robot_type_creates_new(db, tmp_path):
    svc, owner, ws, _ = _setup(db, tmp_path)
    artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
    d1 = svc.deploy(db, owner, ws, artifact, "franka")
    d2 = svc.deploy(db, owner, ws, artifact, "agility")
    assert d1.id != d2.id


def test_deployment_state_machine(db, tmp_path):
    svc, owner, ws, artifact_file = _setup(db, tmp_path)
    artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
    deployment = svc.deploy(db, owner, ws, artifact, "franka")

    svc.download(db, deployment)
    assert deployment.status == "downloading"

    # §5：唯一 VERIFIED 路径 = edge 上报 checksum（server 比较）
    import hashlib

    actual = hashlib.sha256(artifact_file.read_bytes()).hexdigest()
    svc.report_checksum(db, deployment, actual)
    assert deployment.status == "verified"

    edge_svc = EdgeService(None)
    agent, _ = edge_svc.register(db, "edge-01", {})
    svc.run_policy(db, deployment, agent)
    assert deployment.status == "running"
    assert deployment.edge_agent_id == agent.id

    svc.complete(db, deployment, success=True)
    assert deployment.status == "success"
    assert deployment.error_message is None


def test_complete_failure_records_error(db, tmp_path):
    svc, owner, ws, artifact_file = _setup(db, tmp_path)
    artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
    d = svc.deploy(db, owner, ws, artifact, "franka")
    svc.download(db, d)
    import hashlib

    actual = hashlib.sha256(artifact_file.read_bytes()).hexdigest()
    svc.report_checksum(db, d, actual)
    svc.run_policy(db, d, None)
    svc.complete(db, d, success=False, error="motor fault")
    assert d.status == "failed"
    assert d.error_message == "motor fault"


def test_verify_checksum_match_ok(db, tmp_path):
    svc, owner, ws, _ = _setup(db, tmp_path)
    artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
    d = svc.deploy(db, owner, ws, artifact, "franka")
    svc.download(db, d)
    svc.verify_checksum(db, d)
    assert d.status == "verified"
    assert d.error_message is None


def test_verify_checksum_mismatch_fails(db, tmp_path):
    svc, owner, ws, artifact_file = _setup(db, tmp_path)
    artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
    d = svc.deploy(db, owner, ws, artifact, "franka")
    svc.download(db, d)  # 必须先 download（防绕过，§23）
    artifact_file.write_bytes(b"tampered-bytes")
    svc.verify_checksum(db, d)
    assert d.status == "failed"
    assert d.error_message
    assert "mismatch" in d.error_message


def test_deploy_cross_owner_404(db, tmp_path):
    svc, owner, ws, _ = _setup(db, tmp_path)
    artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
    intruder = _user(db, user_id="u-2")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        svc.deploy(db, intruder, ws, artifact, "franka")
    assert exc.value.status_code == 404


def test_create_artifact_cross_owner_404(db, tmp_path):
    svc, _owner, ws, _ = _setup(db, tmp_path)
    intruder = _user(db, user_id="u-2")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        svc.create_artifact(db, intruder, ws, "checkpoint.pt")
    assert exc.value.status_code == 404


def test_list_filters_by_owner(db, tmp_path):
    svc, owner, ws, _ = _setup(db, tmp_path)
    artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
    d = svc.deploy(db, owner, ws, artifact, "franka")
    intruder = _user(db, user_id="u-2")
    db.commit()
    assert svc.list(db, owner) == [d]
    assert svc.list(db, intruder) == []
    assert svc.list(db, owner, workspace_id=ws.id) == [d]
    assert svc.list(db, owner, workspace_id="other-ws") == []


# ---------------------------------------------------------------------------
# create_artifact：本地缺失时经 provider.pull_artifact 拉取（K8s PVC 场景）
# ---------------------------------------------------------------------------


class PullRecordingProvider(MockProvider):
    """记录 pull_artifact 返回的临时路径，验证「调用方负责清理」。"""

    def __init__(self, public_base_url: str, workspace_root):
        super().__init__(public_base_url, workspace_root=workspace_root)
        self.source_paths: list[str] = []
        self.pulled: list = []

    def pull_artifact(self, workspace, source_path: str):
        tmp = super().pull_artifact(workspace, source_path)
        self.source_paths.append(source_path)
        self.pulled.append(tmp)
        return tmp


def _setup_pull(db, tmp_path):
    """构造：控制面本地无文件、runtime 侧有文件（模拟 K8s workspace Pod PVC）。"""
    _template(db)
    owner = _user(db)
    ws = _workspace(db, owner)
    db.commit()
    control_root = tmp_path / "control"
    control_root.mkdir()
    runtime_root = tmp_path / "runtime"
    (runtime_root / ws.id).mkdir(parents=True)
    return owner, ws, control_root, runtime_root


def test_create_artifact_pulls_when_local_missing_and_cleans_tmp(db, tmp_path):
    owner, ws, control_root, runtime_root = _setup_pull(db, tmp_path)
    (runtime_root / ws.id / "checkpoint.pt").write_bytes(CHECKPOINT_BYTES)

    provider = PullRecordingProvider("http://127.0.0.1:8000", workspace_root=runtime_root)
    svc = DeploymentService(None, control_root, provider=provider)

    artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
    assert artifact.name == "checkpoint.pt"
    assert artifact.path == "checkpoint.pt"
    assert artifact.checksum == hashlib.sha256(CHECKPOINT_BYTES).hexdigest()
    assert artifact.size_bytes == len(CHECKPOINT_BYTES)
    # 经 provider.pull_artifact 拉取
    assert provider.source_paths == ["checkpoint.pt"]
    assert len(provider.pulled) == 1
    # 临时文件与其父目录（mkdtemp 建的临时目录）均被 finally 清理
    assert not provider.pulled[0].exists()
    assert not provider.pulled[0].parent.exists()


def test_create_artifact_pull_cleans_tmp_dir_on_store_failure(db, tmp_path):
    """store.put 抛异常路径：pull 的临时目录仍被 finally 清理（不泄漏空目录）。"""
    owner, ws, control_root, runtime_root = _setup_pull(db, tmp_path)
    (runtime_root / ws.id / "checkpoint.pt").write_bytes(CHECKPOINT_BYTES)

    provider = PullRecordingProvider("http://127.0.0.1:8000", workspace_root=runtime_root)

    class FailingStore:
        name = "failing"

        def put(self, object_key, data, content_type="application/octet-stream"):
            raise RuntimeError("store write failed")

        def get(self, object_key):
            raise RuntimeError("not implemented")

        def exists(self, object_key):
            return False

        def delete(self, object_key):
            return None

    svc = DeploymentService(None, control_root, store=FailingStore(), provider=provider)

    with pytest.raises(RuntimeError, match="store write failed"):
        svc.create_artifact(db, owner, ws, "checkpoint.pt")
    assert len(provider.pulled) == 1
    assert not provider.pulled[0].parent.exists()  # 异常路径也清理临时目录


def test_create_artifact_uses_local_when_present(db, tmp_path):
    """本地文件存在 → 现状路径，不触发 provider.pull_artifact。"""
    _template(db)
    owner = _user(db)
    ws = _workspace(db, owner)
    db.commit()
    ws_dir = tmp_path / ws.id
    ws_dir.mkdir(parents=True)
    (ws_dir / "checkpoint.pt").write_bytes(CHECKPOINT_BYTES)

    provider = PullRecordingProvider("http://127.0.0.1:8000", workspace_root=tmp_path / "runtime")
    svc = DeploymentService(None, tmp_path, provider=provider)

    artifact = svc.create_artifact(db, owner, ws, "checkpoint.pt")
    assert artifact.checksum == hashlib.sha256(CHECKPOINT_BYTES).hexdigest()
    assert provider.source_paths == []  # 本地存在 → 不拉取


def test_create_artifact_pull_missing_file_404(db, tmp_path):
    owner, ws, control_root, runtime_root = _setup_pull(db, tmp_path)
    # runtime 侧也无文件 → pull 抛 FileNotFoundError → 404
    provider = MockProvider("http://127.0.0.1:8000", workspace_root=runtime_root)
    svc = DeploymentService(None, control_root, provider=provider)

    with pytest.raises(HTTPException) as exc:
        svc.create_artifact(db, owner, ws, "missing.pt")
    assert exc.value.status_code == 404


def test_create_artifact_pull_failure_500(db, tmp_path):
    owner, ws, control_root, runtime_root = _setup_pull(db, tmp_path)

    class FailingPullProvider(MockProvider):
        def pull_artifact(self, workspace, source_path: str):
            raise RuntimeError("kubectl cp failed")

    provider = FailingPullProvider("http://127.0.0.1:8000", workspace_root=runtime_root)
    svc = DeploymentService(None, control_root, provider=provider)

    with pytest.raises(HTTPException) as exc:
        svc.create_artifact(db, owner, ws, "checkpoint.pt")
    assert exc.value.status_code == 500
    assert "failed to pull artifact" in exc.value.detail

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
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("deploy-bypass"), connect_args={"check_same_thread": False})
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


# ---------------------------------------------------------------------------
# §10：report-checksum 认证口径（X-Agent-Token）
# ---------------------------------------------------------------------------


def _api_register(client, email: str, username: str) -> dict:
    """通过 API 注册用户，返回 {token, id}。"""
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "username": username, "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    return {"token": resp.json()["token"], "id": resp.json()["user"]["id"]}


def _api_register_agent(client, headers: dict, name: str) -> dict:
    """通过 API 注册 EdgeAgent，返回 {agent: {...}, token: 原始 token}。"""
    resp = client.post("/api/edge/agents/register", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _api_seed_downloading_deployment(client, headers: dict, checksum: str) -> str:
    """通过 API 建 workspace，再直连 DB 构造 DOWNLOADING 状态的 deployment。"""
    import uuid

    from app.deps import SessionFactory
    from app.models import Artifact, DeploymentRecord

    ws_resp = client.post(
        "/api/workspaces", json={"template_id": "cartpole", "auto_start": False}, headers=headers
    )
    assert ws_resp.status_code == 201, ws_resp.text
    wid = ws_resp.json()["id"]
    dep_id = str(uuid.uuid4())
    art_id = str(uuid.uuid4())
    with SessionFactory() as db:
        db.add(
            Artifact(
                id=art_id,
                workspace_id=wid,
                name="model.pt",
                path="model.pt",
                object_key=f"{wid}/model.pt",
                checksum=checksum,
                size_bytes=1,
            )
        )
        db.add(
            DeploymentRecord(
                id=dep_id,
                workspace_id=wid,
                artifact_id=art_id,
                model_version="0.1.0",
                template_version="0.1.0",
                robot_type="franka",
                checksum=checksum,
                status=DeploymentStatus.DOWNLOADING.value,
            )
        )
        db.commit()
    return dep_id


def test_report_checksum_requires_agent_token():
    """§10：report-checksum 需要 X-Agent-Token；无 token / 用户会话 → 401。"""
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as client:
        user = _api_register(client, "report-no-token@example.com", "report-no-token")
        headers = {"Authorization": f"Bearer {user['token']}"}
        dep_id = _api_seed_downloading_deployment(client, headers, "0" * 64)

        # 无 token → 401
        assert (
            client.post(
                f"/api/deployments/{dep_id}/report-checksum", json={"actual_sha256": "0" * 64}
            ).status_code
            == 401
        )
        # 用户会话（Bearer）不再是有效认证方式 → 401
        assert (
            client.post(
                f"/api/deployments/{dep_id}/report-checksum",
                json={"actual_sha256": "0" * 64},
                headers=headers,
            ).status_code
            == 401
        )


def test_report_checksum_rejects_invalid_agent_token():
    """§10：错误 agent token → 401。"""
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as client:
        user = _api_register(client, "report-bad-token@example.com", "report-bad-token")
        headers = {"Authorization": f"Bearer {user['token']}"}
        dep_id = _api_seed_downloading_deployment(client, headers, "0" * 64)

        assert (
            client.post(
                f"/api/deployments/{dep_id}/report-checksum",
                json={"actual_sha256": "0" * 64},
                headers={"X-Agent-Token": "wrong-agent-token"},
            ).status_code
            == 401
        )


def test_report_checksum_cross_tenant_agent_404():
    """§10：跨租户 agent 上报他人 deployment → 404（不泄露存在性）。"""
    import hashlib

    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    checksum = hashlib.sha256(b"cross-tenant-bytes").hexdigest()
    with TestClient(fastapi_app) as client:
        user_a = _api_register(client, "report-cross-a@example.com", "report-cross-a")
        agent_a = _api_register_agent(
            client, {"Authorization": f"Bearer {user_a['token']}"}, "report-cross-a-agent"
        )
        user_b = _api_register(client, "report-cross-b@example.com", "report-cross-b")
        dep_id = _api_seed_downloading_deployment(
            client, {"Authorization": f"Bearer {user_b['token']}"}, checksum
        )

        resp = client.post(
            f"/api/deployments/{dep_id}/report-checksum",
            json={"actual_sha256": checksum},
            headers={"X-Agent-Token": agent_a["token"]},
        )
        assert resp.status_code == 404


def test_report_checksum_same_tenant_agent_verifies():
    """§10：同租户 agent 上报正确 checksum → VERIFIED。"""
    import hashlib

    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    checksum = hashlib.sha256(b"agent-report-bytes").hexdigest()
    with TestClient(fastapi_app) as client:
        user = _api_register(client, "report-ok@example.com", "report-ok")
        headers = {"Authorization": f"Bearer {user['token']}"}
        agent = _api_register_agent(client, headers, "report-ok-agent")
        dep_id = _api_seed_downloading_deployment(client, headers, checksum)

        resp = client.post(
            f"/api/deployments/{dep_id}/report-checksum",
            json={"actual_sha256": checksum},
            headers={"X-Agent-Token": agent["token"]},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "verified"


def test_report_checksum_mismatch_fails():
    """§10：同租户 agent 上报错误 checksum → FAILED。"""
    import hashlib

    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    checksum = hashlib.sha256(b"agent-report-bytes").hexdigest()
    with TestClient(fastapi_app) as client:
        user = _api_register(client, "report-mismatch@example.com", "report-mismatch")
        headers = {"Authorization": f"Bearer {user['token']}"}
        agent = _api_register_agent(client, headers, "report-mismatch-agent")
        dep_id = _api_seed_downloading_deployment(client, headers, checksum)

        resp = client.post(
            f"/api/deployments/{dep_id}/report-checksum",
            json={"actual_sha256": "0" * 64},
            headers={"X-Agent-Token": agent["token"]},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"


def test_report_checksum_bound_agent_exclusive():
    """§10：deployment 已绑定执行 agent（run 阶段设置 edge_agent_id）后，
    仅该 agent 可上报；同租户其他 agent 上报 → 404（不泄露存在性）。"""
    import hashlib

    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    checksum = hashlib.sha256(b"bound-agent-bytes").hexdigest()
    with TestClient(fastapi_app) as client:
        user = _api_register(client, "report-bound@example.com", "report-bound")
        headers = {"Authorization": f"Bearer {user['token']}"}
        agent_a = _api_register_agent(client, headers, "report-bound-a")
        agent_b = _api_register_agent(client, headers, "report-bound-b")
        dep_id = _api_seed_downloading_deployment(client, headers, checksum)

        # agent A 上报正确 → VERIFIED
        r = client.post(
            f"/api/deployments/{dep_id}/report-checksum",
            json={"actual_sha256": checksum},
            headers={"X-Agent-Token": agent_a["token"]},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "verified"

        # run 绑定 agent A（写 edge_agent_id）
        r = client.post(
            f"/api/deployments/{dep_id}/run",
            json={"edge_agent_id": agent_a["agent"]["id"]},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "running"

        # 同租户 agent B 上报已绑定(A)的 deployment → 404
        r = client.post(
            f"/api/deployments/{dep_id}/report-checksum",
            json={"actual_sha256": checksum},
            headers={"X-Agent-Token": agent_b["token"]},
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "deployment not found"


# ---------------------------------------------------------------------------
# §21 + docs/adr/0008：对象存储「不可用」与「产物不存在」必须是两种判决
# ---------------------------------------------------------------------------


class _VerdictStore:
    """把 store 的读结果钉成指定异常（写方法被调用即失败：verify 不该写对象）。"""

    name = "verdict"

    def __init__(self, error: Exception):
        self.error = error

    def put(self, object_key, data, content_type="application/octet-stream"):
        raise AssertionError("verify 不该写对象")

    def get(self, object_key):
        raise self.error

    def exists(self, object_key):
        raise self.error

    def delete(self, object_key):
        raise AssertionError("verify 不该删对象")


def _verify_with_store_error(monkeypatch, email: str, error: Exception):
    """建一条 DOWNLOADING 部署，把 store 换成会抛 error 的实现，再走真实 HTTP verify。

    返回 (响应, 落库状态)。`raise_server_exceptions=False` 让未处理的异常以 500 结束，
    而不是把测试进程打断——未处理本身就是要观测的结果之一。
    """
    import hashlib

    from fastapi.testclient import TestClient

    from app.deps import SessionFactory, deployment_service
    from app.main import app as fastapi_app
    from app.models import DeploymentRecord

    checksum = hashlib.sha256(b"store-verdict-bytes").hexdigest()
    monkeypatch.setattr(deployment_service, "store", _VerdictStore(error))
    with TestClient(fastapi_app, raise_server_exceptions=False) as client:
        user = _api_register(client, email, email.split("@")[0])
        headers = {"Authorization": f"Bearer {user['token']}"}
        dep_id = _api_seed_downloading_deployment(client, headers, checksum)
        resp = client.post(f"/api/deployments/{dep_id}/verify", headers=headers)
        with SessionFactory() as db:
            row = db.get(DeploymentRecord, dep_id)
            return resp, (row.status if row else None), (row.error_message if row else None)


def test_storage_outage_gives_503_and_leaves_deployment_retryable(monkeypatch):
    """存储不可用 → 503，且记录停在 downloading（FAILED 是终态，进了就再也验不回来）。

    变异对照：把 verify_checksum 改回 `except Exception → _fail(...)` 本条即红。
    """
    from app.services.artifact_store import ArtifactStoreUnavailableError

    resp, status, error_message = _verify_with_store_error(
        monkeypatch, "store-outage@example.com", ArtifactStoreUnavailableError("S3 head_bucket failed")
    )
    assert resp.status_code == 503, resp.text
    assert "unavailable" in resp.text
    assert status == DeploymentStatus.DOWNLOADING.value
    assert error_message is None


def test_genuinely_missing_object_still_fails_the_deployment(monkeypatch):
    """产物确实不存在 → 仍是 200 + FAILED + "artifact object missing"（旧语义保留）。"""
    from app.services.artifact_store import ArtifactNotFoundError

    resp, status, error_message = _verify_with_store_error(
        monkeypatch, "store-absent@example.com", ArtifactNotFoundError("object not found: 'ws/x'")
    )
    assert resp.status_code == 200, resp.text
    assert status == DeploymentStatus.FAILED.value
    assert "artifact object missing" in (error_message or "")


def test_untranslated_store_error_does_not_consume_the_deployment(monkeypatch):
    """没被协议层翻译过的 SDK 异常：不改状态（500），不能谎报"产物不存在"。"""
    from botocore.exceptions import ClientError

    resp, status, error_message = _verify_with_store_error(
        monkeypatch,
        "store-raw-sdk@example.com",
        ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject"),
    )
    assert resp.status_code == 500, resp.text
    assert status == DeploymentStatus.DOWNLOADING.value
    assert error_message is None

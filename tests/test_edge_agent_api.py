"""Edge agent 的服务端鉴权面（§25 / ADR 0007）。

三个新端点，全走 `X-Agent-Token`：

| 端点 | 语义 |
|---|---|
| `GET  /api/edge/agents/{id}/deployments/assigned` | 工作发现：只看绑定给自己的部署 |
| `POST /api/edge/agents/{id}/deployments/{dep}/begin` | 设备侧开门：pending → downloading（条件 UPDATE） |
| `GET  /api/deployments/{dep}/artifact` | 取件：字节 + `X-Artifact-Sha256` |

ADR 0007 要求这一轮必须自带三件：越权 404（agent A 取 agent B 的件）、路径穿越、
真起 server 的 e2e（在 `test_edge_agent_e2e.py`）。这里覆盖前两件与状态机前提。
全部走真实 HTTP 端点 + 真实 `demo-checkpoint` 产物，不手工塞库。
"""

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.deps import SessionFactory, scheduler
from app.main import app
from app.models import Artifact, DeploymentStatus
from tests.http_auth import auth_headers, register_body

ROBOT = "franka"
#: 本模块创建过的 workspace；模块结束时把占住的 mock 卡还回池子。
#: 全套共用一个库、mock 只有 8 张卡，不还就会把后面的用例饿死（tests/gpu_pool.py）。
_module_workspaces: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _return_gpus_to_the_pool():
    yield
    with SessionFactory() as db:
        for workspace_id in _module_workspaces:
            scheduler.release(db, workspace_id)


def _setup(client: TestClient, email: str, agent_name: str = "arm-01") -> dict:
    """用户 + 设备 + workspace + 一份真实产物（走 demo-checkpoint 端点）。"""
    body = register_body(client, email, email.split("@")[0])
    headers = auth_headers(body["token"])
    agent = client.post("/api/edge/agents/register", json={"name": agent_name}, headers=headers)
    assert agent.status_code == 201, agent.text
    ws = client.post(
        "/api/workspaces", json={"template_id": "cartpole", "auto_start": False}, headers=headers
    )
    assert ws.status_code == 201, ws.text
    wid = ws.json()["id"]
    _module_workspaces.append(wid)
    checkpoint = client.post(f"/api/workspaces/{wid}/demo-checkpoint", headers=headers)
    assert checkpoint.status_code == 200, checkpoint.text
    return {
        "headers": headers,
        "agent_id": agent.json()["agent"]["id"],
        "agent_token": agent.json()["token"],
        "workspace_id": wid,
        "path": checkpoint.json()["path"],
        "sha256": checkpoint.json()["sha256"],
    }


def _deploy(client: TestClient, st: dict, robot: str = ROBOT) -> dict:
    resp = client.post(
        "/api/deployments",
        json={
            "workspace_id": st["workspace_id"],
            "artifact_path": st["path"],
            "robot_type": robot,
            "edge_agent_id": st["agent_id"],
        },
        headers=st["headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _agent(token: str) -> dict:
    return {"X-Agent-Token": token}


def _begin(client: TestClient, st: dict, dep_id: str):
    return client.post(
        f"/api/edge/agents/{st['agent_id']}/deployments/{dep_id}/begin",
        json={},
        headers=_agent(st["agent_token"]),
    )


# ----------------------------------------------------------------------
# 发现
# ----------------------------------------------------------------------
def test_assigned_lists_only_deployments_bound_to_this_agent():
    with TestClient(app) as client:
        mine = _setup(client, "disc-a@example.com", "arm-a")
        theirs = _setup(client, "disc-b@example.com", "arm-b")
        dep = _deploy(client, mine)
        _deploy(client, theirs, robot="ur5")  # 另一租户、另一设备名下

        seen = client.get(
            f"/api/edge/agents/{mine['agent_id']}/deployments/assigned",
            headers=_agent(mine["agent_token"]),
        )
        assert seen.status_code == 200, seen.text
        ids = [d["id"] for d in seen.json()]
        assert ids == [dep["id"]], "发现面必须恰好是本设备名下的部署"

        empty = client.get(
            f"/api/edge/agents/{theirs['agent_id']}/deployments/assigned",
            headers=_agent(theirs["agent_token"]),
        )
        assert [d["id"] for d in empty.json()] != ids


def test_assigned_requires_agent_token_and_self_id():
    with TestClient(app) as client:
        a = _setup(client, "selfid-a@example.com", "arm-a")
        b = _setup(client, "selfid-b@example.com", "arm-b")
        path = f"/api/edge/agents/{a['agent_id']}/deployments/assigned"
        # 无 token / 用户 Bearer 都不是设备侧认证方式 → 401
        assert client.get(path).status_code == 401
        assert client.get(path, headers=a["headers"]).status_code == 401
        # 拿 A 的 token 去查 B 的 agent_id：自指不过 → 404（不泄露存在性）
        assert client.get(
            f"/api/edge/agents/{b['agent_id']}/deployments/assigned", headers=_agent(a["agent_token"])
        ).status_code == 404


def test_assignment_at_deploy_time_requires_ownership():
    """控制面不能把别人的设备指派为执行者（与 /run 同一条规则）。"""
    with TestClient(app) as client:
        a = _setup(client, "assign-a@example.com", "arm-a")
        b = _setup(client, "assign-b@example.com", "arm-b")
        resp = client.post(
            "/api/deployments",
            json={
                "workspace_id": a["workspace_id"],
                "artifact_path": a["path"],
                "robot_type": ROBOT,
                "edge_agent_id": b["agent_id"],
            },
            headers=a["headers"],
        )
        assert resp.status_code == 404, resp.text


# ----------------------------------------------------------------------
# 取件
# ----------------------------------------------------------------------
def test_full_agent_path_begins_fetches_and_verifies():
    with TestClient(app) as client:
        st = _setup(client, "fetch-ok@example.com")
        dep = _deploy(client, st)
        assert dep["status"] == DeploymentStatus.PENDING.value
        assert dep["checksum"] == st["sha256"], "部署记录的摘要应与产物一致"

        assert _begin(client, st, dep["id"]).status_code == 200
        got = client.get(f"/api/deployments/{dep['id']}/artifact", headers=_agent(st["agent_token"]))
        assert got.status_code == 200, got.text
        assert got.headers["X-Artifact-Sha256"] == st["sha256"]
        assert int(got.headers["X-Artifact-Size"]) == len(got.content)
        assert hashlib.sha256(got.content).hexdigest() == st["sha256"]
        assert "content-disposition" not in {k.lower() for k in got.headers}

        reported = client.post(
            f"/api/deployments/{dep['id']}/report-checksum",
            json={"actual_sha256": hashlib.sha256(got.content).hexdigest()},
            headers=_agent(st["agent_token"]),
        )
        assert reported.status_code == 200, reported.text
        assert reported.json()["status"] == DeploymentStatus.VERIFIED.value


def test_fetch_requires_begin_first():
    """没 begin 就没有字节：与 §23 的"没下载不得判 VERIFIED"同一个状态前提。"""
    with TestClient(app) as client:
        st = _setup(client, "fetch-nobegin@example.com")
        dep = _deploy(client, st)
        resp = client.get(f"/api/deployments/{dep['id']}/artifact", headers=_agent(st["agent_token"]))
        assert resp.status_code == 409, resp.text
        assert dep["status"] == DeploymentStatus.PENDING.value


def test_cross_tenant_agent_cannot_fetch_another_devices_artifact():
    """ADR 0007 要求的那条：agent A 取 agent B 名下的件 → 404（不是 403）。"""
    with TestClient(app) as client:
        a = _setup(client, "xtenant-a@example.com", "arm-a")
        b = _setup(client, "xtenant-b@example.com", "arm-b")
        dep_b = _deploy(client, b)
        _begin(client, b, dep_b["id"])
        resp = client.get(
            f"/api/deployments/{dep_b['id']}/artifact", headers=_agent(a["agent_token"])
        )
        assert resp.status_code == 404, resp.text


def test_unbound_agent_of_the_same_tenant_cannot_fetch():
    """同租户但没绑定：也不能取（绑定关系才是授权，租户只是上界）。"""
    with TestClient(app) as client:
        st = _setup(client, "sametenant@example.com", "arm-1")
        same = client.post(
            "/api/edge/agents/register", json={"name": "arm-2"}, headers=st["headers"]
        ).json()
        dep = _deploy(client, st)
        _begin(client, st, dep["id"])
        resp = client.get(
            f"/api/deployments/{dep['id']}/artifact", headers=_agent(same["token"])
        )
        assert resp.status_code == 404, resp.text


def test_planted_object_key_pointing_at_another_workspace_is_not_served():
    """越权取件：object_key 不是请求参数，但一行被改写的库记录也不能借端点读别人的件。

    两种改写分别由两道防线接：
    - 直接写成**别人的合法 key**（不含 `..`，`_safe_key` 放行）→ 只有 workspace 前缀
      复核挡得住，这才是这道防线的开火对照；
    - 写成 `../` 逃逸形态 → 前缀复核先给 404（`_safe_key` 是第二道，会给 500）。

    开火前提：victim 那份对象**同一端点、自己的凭证**取得到；否则这里的 404
    可能只是"文件不存在"，防线有没有生效根本看不见。
    """
    with TestClient(app) as client:
        victim = _setup(client, "victim@example.com")
        attacker = _setup(client, "attacker@example.com")
        dep_v = _deploy(client, victim)
        _begin(client, victim, dep_v["id"])
        reachable = client.get(
            f"/api/deployments/{dep_v['id']}/artifact", headers=_agent(victim["agent_token"])
        )
        assert reachable.status_code == 200, reachable.text

        dep = _deploy(client, attacker)
        _begin(client, attacker, dep["id"])

        def _plant(key: str) -> None:
            with SessionFactory() as db:
                art = db.get(Artifact, dep["artifact_id"])
                assert art is not None
                art.object_key = key
                db.commit()

        _plant(f"{victim['workspace_id']}/{victim['path']}")
        stolen = client.get(
            f"/api/deployments/{dep['id']}/artifact", headers=_agent(attacker["agent_token"])
        )
        assert stolen.status_code == 404, stolen.text
        assert stolen.content != reachable.content

        _plant(f"../{victim['workspace_id']}/{victim['path']}")
        escaped = client.get(
            f"/api/deployments/{dep['id']}/artifact", headers=_agent(attacker["agent_token"])
        )
        assert escaped.status_code == 404, escaped.text


def test_begin_is_idempotent_and_refuses_non_pending():
    with TestClient(app) as client:
        st = _setup(client, "begin-idem@example.com")
        dep = _deploy(client, st)
        assert _begin(client, st, dep["id"]).json()["status"] == DeploymentStatus.DOWNLOADING.value
        # 第二次：不报错、不重复推进（条件 UPDATE 的 rowcount=0 走幂等支）
        assert _begin(client, st, dep["id"]).json()["status"] == DeploymentStatus.DOWNLOADING.value
        client.post(
            f"/api/deployments/{dep['id']}/report-checksum",
            json={"actual_sha256": st["sha256"]},
            headers=_agent(st["agent_token"]),
        )
        refused = _begin(client, st, dep["id"])
        assert refused.status_code == 409, refused.text


def test_store_outage_on_fetch_is_503_and_leaves_the_record_retryable(monkeypatch):
    """v0.6.0 的口径（ADR 0008）同样适用于取件：故障不是"产物不存在"，更不落终态。"""
    from app.services.artifact_store import ArtifactStoreUnavailableError
    from app.services.deployment import DeploymentService

    def boom(self, db, deployment):
        raise ArtifactStoreUnavailableError("head_object failed: connection refused")

    monkeypatch.setattr(DeploymentService, "read_artifact", boom)
    with TestClient(app) as client:
        st = _setup(client, "outage@example.com")
        dep = _deploy(client, st)
        _begin(client, st, dep["id"])
        resp = client.get(f"/api/deployments/{dep['id']}/artifact", headers=_agent(st["agent_token"]))
        assert resp.status_code == 503, resp.text
        with SessionFactory() as db:
            from app.models import DeploymentRecord

            row = db.get(DeploymentRecord, dep["id"])
            assert row is not None
            assert row.status == DeploymentStatus.DOWNLOADING.value, "故障不得把记录写成终态"

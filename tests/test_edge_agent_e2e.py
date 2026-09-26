"""Sim2Real 真进程 e2e（§25 / ADR 0007 明写要求的那件）。

控制面是**真 uvicorn 子进程**，设备是**另一个子进程**（`python -m edge_agent run`）。
不用 TestClient：进程内 ASGI 调用看不见 agent 的那一半——摘要核对、落盘、
原子改名、遥测，全都发生在设备进程里。

凭据一律走环境变量而不是 argv：`--token` 会出现在 `ps` 的进程列表里，
本机任何用户都能读到（`register` 子命令的 `--owner-token` 同理，只在人值守时用）。
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

from tests.live_server import REPO_ROOT, live_server

PASSWORD = "agent-e2e-pass-123"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = Path(tempfile.mkdtemp(prefix="ec-agent-e2e-", dir=tmp_path_factory.mktemp("srv")))
    with live_server(root) as srv:
        yield srv


def _api(server) -> httpx.Client:
    return httpx.Client(base_url=f"{server.url}/api", timeout=30)


def _provision(server, email: str) -> dict:
    """准备一个"等着设备来取件"的部署：产物、设备、绑定，全部走真实端点。"""
    with _api(server) as api:
        user = api.post(
            "/auth/register",
            json={"email": email, "username": email.split("@")[0][:12], "password": PASSWORD},
        )
        assert user.status_code == 201, user.text
        headers = {"Authorization": f"Bearer {user.json()['token']}"}
        agent = api.post("/edge/agents/register", json={"name": "arm-e2e"}, headers=headers)
        assert agent.status_code == 201, agent.text
        ws = api.post(
            "/workspaces", json={"template_id": "cartpole", "auto_start": False}, headers=headers
        )
        assert ws.status_code == 201, ws.text
        checkpoint = api.post(f"/workspaces/{ws.json()['id']}/demo-checkpoint", headers=headers)
        assert checkpoint.status_code == 200, checkpoint.text
        dep = api.post(
            "/deployments",
            json={
                "workspace_id": ws.json()["id"],
                "artifact_path": checkpoint.json()["path"],
                "robot_type": "franka",
                "edge_agent_id": agent.json()["agent"]["id"],
            },
            headers=headers,
        )
        assert dep.status_code == 201, dep.text
        return {
            "headers": headers,
            "agent": agent.json(),
            "checkpoint": checkpoint.json(),
            "deployment": dep.json(),
        }


def _run_agent(server, env_extra: dict, *, iterations: int = 1) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), **env_extra)
    return subprocess.run(  # noqa: S603 受控常量参数（本包 CLI），无用户输入
        [sys.executable, "-m", "edge_agent", "run", "--json", "--iterations", str(iterations)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_agent_subprocess_completes_the_sim2real_round(server, tmp_path):
    st = _provision(server, "e2e-agent@example.org")
    dep = st["deployment"]
    assert dep["status"] == "pending"
    workdir = tmp_path / "agent-store"

    proc = _run_agent(
        server,
        {
            "EMBODIEDCLOUD_EDGE_SERVER": server.url,
            "EMBODIEDCLOUD_EDGE_AGENT_ID": st["agent"]["agent"]["id"],
            "EMBODIEDCLOUD_EDGE_TOKEN": st["agent"]["token"],
            "EMBODIEDCLOUD_EDGE_WORKDIR": str(workdir),
        },
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}\nsrv={server.log_tail()}"
    outcomes = json.loads(proc.stdout)
    assert [o["action"] for o in outcomes] == ["ran"], outcomes
    assert outcomes[0]["status_after"] == "verified", outcomes
    assert outcomes[0]["sha256"] == st["checkpoint"]["sha256"]

    # 设备侧：落盘字节就是登记的那份产物（原子改名，没有 .part 残留）
    landed = workdir / f"{dep['id']}.pt"
    assert hashlib.sha256(landed.read_bytes()).hexdigest() == st["checkpoint"]["sha256"]
    assert not list(workdir.glob("*.part")), workdir

    with _api(server) as api:
        # 控制面侧：迁移完全由设备完成，没有任何用户 token 参与
        assert api.get(f"/deployments/{dep['id']}", headers=st["headers"]).json()["status"] == "verified"
        telemetry = api.get(
            f"/edge/agents/{st['agent']['agent']['id']}/telemetry", headers=st["headers"]
        ).json()
        runs = [t for t in telemetry if t["kind"] == "edge-run"]
        assert len(runs) == 1, telemetry
        assert runs[0]["payload"]["deployment_id"] == dep["id"]
        assert runs[0]["payload"]["sha256"] == st["checkpoint"]["sha256"]
        # 心跳确实打过（设备在线状态是被 agent 进程改的，不是夹具改的）
        agent_row = api.get(f"/edge/agents/{st['agent']['agent']['id']}", headers=st["headers"]).json()
        assert agent_row["status"] == "online", agent_row

    # 第二轮：assigned 是轮询，已经验证过的部署不得被重复上机
    again = _run_agent(
        server,
        {
            "EMBODIEDCLOUD_EDGE_SERVER": server.url,
            "EMBODIEDCLOUD_EDGE_AGENT_ID": st["agent"]["agent"]["id"],
            "EMBODIEDCLOUD_EDGE_TOKEN": st["agent"]["token"],
            "EMBODIEDCLOUD_EDGE_WORKDIR": str(workdir),
        },
    )
    assert again.returncode == 0, again.stdout + again.stderr
    assert [o["action"] for o in json.loads(again.stdout)] == ["skipped"]
    with _api(server) as api:
        telemetry = api.get(
            f"/edge/agents/{st['agent']['agent']['id']}/telemetry", headers=st["headers"]
        ).json()
        assert len([t for t in telemetry if t["kind"] == "edge-run"]) == 1


def test_agent_refuses_to_run_without_credentials(server, tmp_path):
    """缺凭据必须是硬失败，不能"静默什么都不做"地退 0（那会让运维以为设备在跑）。"""
    proc = _run_agent(
        server,
        {
            "EMBODIEDCLOUD_EDGE_SERVER": server.url,
            "EMBODIEDCLOUD_EDGE_AGENT_ID": "",
            "EMBODIEDCLOUD_EDGE_TOKEN": "",
            "EMBODIEDCLOUD_EDGE_WORKDIR": str(tmp_path),
        },
    )
    assert proc.returncode == 2
    assert "agent-id" in proc.stderr

    # 服务可达但 token 无效 → 心跳 401，非零退出
    bogus = _run_agent(
        server,
        {
            "EMBODIEDCLOUD_EDGE_SERVER": server.url,
            "EMBODIEDCLOUD_EDGE_AGENT_ID": "00000000-0000-0000-0000-000000000000",
            "EMBODIEDCLOUD_EDGE_TOKEN": "not-a-real-token",
            "EMBODIEDCLOUD_EDGE_WORKDIR": str(tmp_path),
        },
    )
    assert bogus.returncode != 0
    assert "401" in bogus.stderr + bogus.stdout

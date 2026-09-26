"""GPU 管理端点的 HTTP 行为测试（此前仅测「非 admin 403」，未验证真实行为）。

- admin 可见 inventory / hosts
- 工作区 running 时 GPU ALLOCATED 且绑定 workspace；stop 后释放回 AVAILABLE
  （审计发现「GPU 释放回 available」此前只有注释、没有断言）
- unhealthy / drain 状态流转
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.deps import SessionFactory, scheduler
from app.main import app
from app.models import Gpu, GpuStatus, Role, User
from tests.gpu_pool import ensure_free_gpus
from tests.test_demo_workspace import _auth, _register
from tests.workspace_progress import wait_status


def _promote(email: str) -> None:
    from app.deps import SessionFactory

    with SessionFactory() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = Role.ADMIN.value
        db.commit()


def test_gpu_endpoints_admin_and_release_cycle():
    with TestClient(app) as client:
        # 前置条件要自己达成，不能靠"排在别人前面"：全套共用一个库、mock 只有 8 张卡，
        # 而上游用例创建的 workspace 不会替自己回收（实测读数见 tests/gpu_pool.py）。
        # 放在 lifespan 之后：建表是 bootstrap_db() 干的。
        with SessionFactory() as db:
            ensure_free_gpus(db, scheduler)
        token = _register(client, "gpuadmin@example.com", "gpuadmin")
        _promote("gpuadmin@example.com")

        # admin 可见 inventory（mock 8 张虚拟 GPU）与 hosts
        gpus = client.get("/api/gpus", headers=_auth(token))
        assert gpus.status_code == 200, gpus.text
        assert len(gpus.json()) >= 1
        hosts = client.get("/api/gpus/hosts", headers=_auth(token))
        assert hosts.status_code == 200
        assert any(h["name"] == "mock-host" for h in hosts.json())

        # 创建并运行工作区 → GPU 被分配并绑定
        created = client.post(
            "/api/workspaces",
            json={"template_id": "cartpole", "auto_start": True},
            headers=_auth(token),
        )
        assert created.status_code == 201
        wid = created.json()["id"]
        wait_status(client, token, wid, "running")

        ws = client.get(f"/api/workspaces/{wid}", headers=_auth(token)).json()
        assert ws["gpu_id"], "running workspace 必须绑定 GPU"
        gpu_list = {g["id"]: g for g in client.get("/api/gpus", headers=_auth(token)).json()}
        assert gpu_list[ws["gpu_id"]]["status"] == GpuStatus.ALLOCATED.value
        assert gpu_list[ws["gpu_id"]]["workspace_id"] == wid

        # stop → GPU 释放回 AVAILABLE、绑定清空（此前只有注释没有断言）
        assert client.post(f"/api/workspaces/{wid}/stop", headers=_auth(token)).status_code == 200
        wait_status(client, token, wid, "stopped")
        gpu_list = {g["id"]: g for g in client.get("/api/gpus", headers=_auth(token)).json()}
        assert gpu_list[ws["gpu_id"]]["status"] == GpuStatus.AVAILABLE.value
        assert gpu_list[ws["gpu_id"]]["workspace_id"] is None

        # 维护/异常流转（对已释放的 GPU）
        gid = ws["gpu_id"]
        assert client.post(f"/api/gpus/{gid}/drain", headers=_auth(token)).status_code == 204
        assert client.post(f"/api/gpus/{gid}/unhealthy", headers=_auth(token)).status_code == 204
        final = {g["id"]: g for g in client.get("/api/gpus", headers=_auth(token)).json()}
        assert final[gid]["status"] == GpuStatus.UNHEALTHY.value


def test_gpu_endpoints_non_admin_403():
    with TestClient(app) as client:
        token = _register(client, "gpuplain@example.com", "gpuplain")
        assert client.get("/api/gpus", headers=_auth(token)).status_code == 403
        assert client.get("/api/gpus/hosts", headers=_auth(token)).status_code == 403
        from app.deps import SessionFactory

        with SessionFactory() as db:
            gpu = db.scalar(select(Gpu).limit(1))
            assert gpu is not None
            gid = gpu.id
        assert client.post(f"/api/gpus/{gid}/drain", headers=_auth(token)).status_code == 403
        assert client.post(f"/api/gpus/{gid}/unhealthy", headers=_auth(token)).status_code == 403

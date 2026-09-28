"""GPU 管理端点的 HTTP 行为测试（此前仅测「非 admin 403」，未验证真实行为）。

- admin 可见 inventory / hosts
- 工作区 running 时 GPU ALLOCATED 且绑定 workspace；stop 后释放回 AVAILABLE
  （审计发现「GPU 释放回 available」此前只有注释、没有断言）
- unhealthy / drain 三维度流转（状态、健康列、下架意图列）与两条解除路径
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.deps import SessionFactory, scheduler
from app.main import app
from app.models import Gpu, GpuHealth, GpuStatus, Role, User
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
        # N-110：`status` 只是结论，读端必须同时拿到它的凭证列——OPERATIONS 的排查表
        # 就指着这一列，端点不吐它的话那张表是在承诺一个拿不到的东西。
        mock_host = next(h for h in hosts.json() if h["name"] == "mock-host")
        assert mock_host["last_synced_at"], f"开机 inventory 之后 hosts 读不到同步时刻：{mock_host}"
        assert mock_host["status"] == "online"

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

        # 三个维度各一列（ADR 0010）：下架是状态＋意图，健康是另一列，互不覆盖
        gid = ws["gpu_id"]
        assert client.post(f"/api/gpus/{gid}/drain", headers=_auth(token)).status_code == 204
        assert client.post(f"/api/gpus/{gid}/unhealthy", headers=_auth(token)).status_code == 204
        final = {g["id"]: g for g in client.get("/api/gpus", headers=_auth(token)).json()}
        assert final[gid]["status"] == GpuStatus.DRAINED.value, (
            f"人工判决不该被健康判决顶掉：{final[gid]}"
        )
        assert final[gid]["health"] == GpuHealth.UNHEALTHY.value, final[gid]
        assert final[gid]["drain_requested_at"], "下架意图必须读得到（204 的那份主张要能被核对）"

        # 两条解除路径都走产品接口：借走的卡必须还回共享池，否则后面的用例少一张
        assert client.post(f"/api/gpus/{gid}/healthy", headers=_auth(token)).status_code == 204
        assert client.post(f"/api/gpus/{gid}/undrain", headers=_auth(token)).status_code == 204
        after = {g["id"]: g for g in client.get("/api/gpus", headers=_auth(token)).json()}
        assert (after[gid]["status"], after[gid]["health"], after[gid]["drain_requested_at"]) == (
            GpuStatus.AVAILABLE.value, None, None,
        ), f"解除路径没把这张卡完整还回池子：{after[gid]}"


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
        # N-125 新增的解除路径同样在管理门之后（少这一条，它就是个无人守的写入口）
        assert client.post(f"/api/gpus/{gid}/undrain", headers=_auth(token)).status_code == 403

"""`tests.workspace_progress.wait_status` 的极性对照。

常驻动机：`test_gpu_admin.py::test_gpu_endpoints_admin_and_release_cycle` 在两个
pytest 进程并排跑同一棵树时红过一次（另一进程全绿），报错是"20s 内没到 running"；
而 T-1 那一轮已把"同仓并发跑两个 pytest"登记为受支持的模式。旧写法是
「sleep + 读 HTTP」，等于把后台 worker 线程**拿不拿得到 CPU**当成前提——那是这台
机器的调度状态，不是被测系统的性质。

这里不用"再撞一次运气"来验修复，而是把前提做成确定性的：直接掐掉后台线程的循环体
（`_run_forever`），于是"只有测试线程会 tick"成为事实。两档必须反向：
主动 tick 的到得了 running，被动等的到不了。
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.deps import SessionFactory, scheduler
from app.main import app
from app.services.worker import OperationWorker
from tests.gpu_pool import ensure_free_gpus
from tests.test_demo_workspace import _auth, _register
from tests.workspace_progress import wait_status

#: 本模块创建的 workspace 结束时归还其占用的 mock 卡（共用一库，见 tests/gpu_pool.py）。
_module_workspaces: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _return_gpus_to_the_pool():
    yield
    with SessionFactory() as db:
        for workspace_id in _module_workspaces:
            scheduler.release(db, workspace_id)


def _block_worker(monkeypatch) -> None:
    """让后台 worker 线程一启动就退出：此后只有测试线程会 tick。"""
    monkeypatch.setattr(OperationWorker, "_run_forever", lambda self: None)


def _queued_workspace(client: TestClient, token: str) -> str:
    """auto_start=True 只入队；在后台线程被掐住的前提下它必须停在非 running。"""
    created = client.post(
        "/api/workspaces",
        json={"template_id": "cartpole", "auto_start": True},
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    workspace_id = str(created.json()["id"])
    _module_workspaces.append(workspace_id)
    return workspace_id


def test_passive_polling_never_reaches_running_with_the_worker_thread_blocked(monkeypatch):
    """前提档：后台线程被掐住后，被动 sleep+读 拿不到 running。

    这支先跑——它定义"这里没有别的推进者"。如果它读到了 running，说明线程没被
    掐住，下一支的正例读数也就不作数（所以正例必须建立在这条前提之上）。
    """
    _block_worker(monkeypatch)
    with TestClient(app) as client:
        token = _register(client, "prog-passive@example.com", "progpassive")
        wid = _queued_workspace(client, token)
        deadline = time.monotonic() + 2.0
        seen = ""
        while time.monotonic() < deadline:
            seen = client.get(f"/api/workspaces/{wid}", headers=_auth(token)).json()["status"]
            if seen == "running":
                break
            time.sleep(0.1)
        assert seen != "running", (
            f"后台线程没有被掐住（读到 {seen}），本文件的主动/被动对照前提失效"
        )


def test_wait_status_reaches_running_with_the_worker_thread_blocked(monkeypatch):
    """正例：`wait_status` 自己推进队列，后台线程被掐住也到得了 running。"""
    _block_worker(monkeypatch)
    with TestClient(app) as client:
        # 这支真占一张卡：前置条件自己达成（全套共用一库，见 tests/gpu_pool.py）
        with SessionFactory() as db:
            ensure_free_gpus(db, scheduler)
        token = _register(client, "prog-active@example.com", "progactive")
        wid = _queued_workspace(client, token)
        ws = wait_status(client, token, wid, "running", timeout=15.0)
        assert ws["status"] == "running"
        assert ws["gpu_id"], "到 running 却没绑 GPU：说明没真的走完 START，而不是被跳过"

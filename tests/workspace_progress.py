"""等待工作区到达目标状态：由测试线程自己推进 worker。

此前 `test_gpu_admin` 与 `test_api_success_paths` 各有一份 `_wait_status`，形状是
「sleep + 读 HTTP」——等于把"队列被后台线程调到"当成前提，而后台线程能否拿到 CPU
取决于这台机器同时在跑什么。实测：两个 pytest 进程并排跑同一棵树，
`test_gpu_endpoints_admin_and_release_cycle` 在 20s 预算内没到 running（另一进程全绿），
而 T-1 那一轮已把"同仓并发跑两个 pytest"登记为受支持的模式。所以那不是"环境太慢"，
是断言方式把进度外包给了调度器。

改法：每轮先 `worker.tick_once()` 再读状态。claim 是 CAS + fencing token，测试线程
与后台线程同时调用的胜者唯一，不会双执行同一个 operation（`test_worker_fencing` 钉过）。
超时时的报错带上当前状态与该工作区的操作队列行，让失败自带归因。
"""

import time
from typing import Any

from fastapi.testclient import TestClient

from tests.http_auth import auth_headers


def _progress(client: TestClient, token: str, workspace_id: str) -> dict[str, Any]:
    from sqlalchemy import select

    from app.deps import SessionFactory
    from app.models import WorkspaceOperation

    ws: dict[str, Any] = client.get(
        f"/api/workspaces/{workspace_id}", headers=auth_headers(token)
    ).json()
    with SessionFactory() as db:
        ops = list(
            db.scalars(
                select(WorkspaceOperation)
                .where(WorkspaceOperation.workspace_id == workspace_id)
                .order_by(WorkspaceOperation.created_at)
            )
        )
        ws["_operations"] = [
            f"{op.operation_type}:{op.status}(attempts={op.attempts}"
            + (f", error={op.last_error}" if op.last_error else "")
            + ")"
            for op in ops
        ]
    return ws


def wait_status(
    client: TestClient, token: str, workspace_id: str, target: str, timeout: float = 20.0
) -> dict[str, Any]:
    """推进 worker 直到工作区到达 `target`，返回终态的那份投影。"""
    from app.deps import worker

    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        worker.tick_once()
        last = _progress(client, token, workspace_id)
        if last.get("status") == target:
            return last
        time.sleep(0.05)
    raise AssertionError(
        f"workspace {workspace_id} 在 {timeout}s 内未到 {target}；"
        f"当前 {last.get('status')}，操作队列 {last.get('_operations')}，"
        f"完整响应 {last}"
    )

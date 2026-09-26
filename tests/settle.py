"""等 workspace 收敛到"读者可以据以下结论"的状态——4 处用例共用的一份前提。

为什么要有这一份（而不是各文件自己写 `for _ in range(40): sleep(0.05)`）：

1. **2s 不是判据预算，是被测系统的重试节奏。** provision 的一次瞬时失败（共享 mock
   卡池被上游用例借走就是这一类）会进 `OperationWorker` 的指数 backoff：
   `RETRY_BASE_DELAY=1s`、`MAX_ATTEMPTS=3` ⇒ 第 3 次尝试最早落在 1+2=3s 之后。
   旧轮询在 2s 就收工，于是"没等到"被写成 `assert 'queued' == 'running'`，
   读者以为被测系统给出了失败判据，实际是夹具的前置条件没达成。
2. **终态集合必须是真终态。** 本轮产品修复之后（
   `app/services/orchestrator.py::_failure_is_terminal`），status=failed 只在 attempts
   用尽时才出现；非终态的失败停在 queued。所以 `{running, failed}` 现在确实是
   "之后不会再自己变"的集合——旧代码里它同时包含了"还要重试"的中间态。
3. 前提没达成时必须把**域内现场**一起报出来（空闲卡数 / 在借的卡数 / 最后一次读数），
   否则下一轮还是要从 `assert 'queued' == 'running'` 反推 20 分钟。
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import func, select

#: 收敛集合：只有这两个状态之后不会再被系统自己改写。
SETTLED_STATUSES = frozenset({"running", "failed"})


def _pool_census() -> str:
    """失败读数里的域内现场：够用的空闲卡有几张、被占着有几张。

    整段是诊断信息，读不到也不得掩盖"前提未达成"本身（所以吞异常，但把异常写进串里）。
    """
    try:
        from app.deps import SessionFactory
        from app.models import Gpu, GpuStatus
        from tests.gpu_pool import count_big_enough

        with SessionFactory() as db:
            free = count_big_enough(db, 8)
            allocated = int(
                db.scalar(
                    select(func.count())
                    .select_from(Gpu)
                    .where(Gpu.status == GpuStatus.ALLOCATED.value)
                )
                or 0
            )
        return f"池内 ≥8GB 空闲卡 {free} 张、ALLOCATED {allocated} 张"
    except Exception as exc:  # 诊断臂不得改变判决
        return f"(池子读数不可得：{exc!r})"


def await_workspace_settled(
    client: Any,
    workspace_id: str,
    headers: dict[str, str],
    *,
    timeout_seconds: float = 30.0,
    interval: float = 0.1,
) -> dict[str, Any]:
    """轮询到 workspace 进入终态并返回最后一次读数；超时按"前提未达成"抛错。

    `client` 只需要 `.get(url, headers=...).json()` 这一形状（TestClient 即可满足）。
    """
    deadline = time.monotonic() + timeout_seconds
    state: dict[str, Any] = {}
    while True:
        state = client.get(f"/api/workspaces/{workspace_id}", headers=headers).json()
        if isinstance(state, dict) and state.get("status") in SETTLED_STATUSES:
            return state
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    raise AssertionError(
        f"前提未达成：{timeout_seconds:.0f}s 内 workspace {str(workspace_id)[:8]} 没有收敛到终态 "
        f"{sorted(SETTLED_STATUSES)}；最后一次读数 status={state.get('status')!r} "
        f"error_message={state.get('error_message')!r}；{_pool_census()}。"
        "status=queued 且带 error_message ＝ worker 还在按 attempts/backoff 重试"
        "（OperationWorker.MAX_ATTEMPTS=3、RETRY_BASE_DELAY=1s 指数退避），"
        "那是夹具的前置条件没达成，不是被测系统给出的判据。"
    )

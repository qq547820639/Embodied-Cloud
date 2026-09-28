"""跨表一致性量具：`gpus` 与 `workspaces` 互相指认的冲突清单。

从 `tests/test_recover_gpu_column_drift.py` 搬来（N-101）：同一把尺子要能同时量
「回收器清列之后格子还指着卡」（N-82/N-84 那一族）与
「release 自己抛错之后卡还指着格子」——后者是 N-99 的驱动者要负责收掉的那一格。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Gpu, Workspace


def gpu_ownership_disagreements(db: Session) -> list[dict[str, str]]:
    """`gpus` 与 `workspaces` 互相指认的冲突清单；空表 = 两张权威表一致。

    - `card-points-at-holder`：`gpus.workspace_id = w.id` 而 `w.gpu_id != gpus.id`
      —— 卡被一个不声称持有它的格占着：谁也抢不走，持有它的人也不会来放（卡被钉死）。
    - `holder-points-at-card`：`w.gpu_id = g.id` 而 `g.workspace_id != w.id`
      —— 正是本轮缺陷的形状：强制放卡之后格子还声称持有那张卡。
    """
    out: list[dict[str, str]] = []
    for gpu in db.scalars(select(Gpu).order_by(Gpu.id)):
        if gpu.workspace_id is None:
            continue
        holder = db.get(Workspace, gpu.workspace_id)
        if holder is None or holder.gpu_id != gpu.id:
            out.append(
                {
                    "kind": "card-points-at-holder",
                    "gpu_id": gpu.id,
                    "workspace_id": gpu.workspace_id,
                }
            )
    for ws in db.scalars(select(Workspace).order_by(Workspace.id)):
        if ws.gpu_id is None:
            continue
        card = db.get(Gpu, ws.gpu_id)
        if card is None or card.workspace_id != ws.id:
            out.append(
                {"kind": "holder-points-at-card", "gpu_id": ws.gpu_id, "workspace_id": ws.id}
            )
    return out

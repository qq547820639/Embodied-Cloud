"""warm pool 的补位量 vs 舰队容量（登记表 N-29 的取证台）。

`maintain()` 的补位是**按模板逐个**算的：`missing = warm_pool_size − (ready + prewarming + legacy)`，
所以整池需求 = `size × enabled 模板数`，今天没有任何一处把它跟舰队实际卡数对一下。
这里把那件事量成一张表，回答三个问题：

1. 池被容量截住时，代价长什么样（创建了多少行、多少行最终变成 tombstone、多少次 provision 失败）；
2. 池把舰队吃掉之后，**交互式请求**还能不能起来（这才是用户看得见的伤害）；
3. 这些代价在 `size=1`（生产默认）下是否真的存在——决定 N-29 是修还是记为惰性。

复用：舰队形状取自 `tests/scheduler_policy_lab.FLEET`（8 张卡／两台 host，混合 8/16/24/48 GiB），
模板是 `app/seed.py` 的真 5 份（VRAM 需求 8/16/24/24/16 GiB）。库是一次性文件库，不碰开发用的那份。

    make warm-capacity          # 打表（size ∈ 1,2,4）
    make warm-capacity --json   # 可对账的 JSON
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    Gpu,
    GpuHost,
    GpuStatus,
    OperationStatus,
    OperationType,
    Template,
    WarmPoolState,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from app.seed import seed_templates
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.services.warmpool import WarmPoolManager
from app.services.worker import OperationWorker
from tests.scheduler_policy_lab import FLEET

ROOT = Path(tempfile.gettempdir())


def _seed_fleet(db) -> None:
    hosts = [
        GpuHost(id=f"cap-host-{i}", name=f"cap-h{i}", address="127.0.0.1", provider="mock")
        for i in {h for h, _ in FLEET}
    ]
    db.add_all(hosts)
    db.flush()
    for idx, (host_ix, gib) in enumerate(FLEET):
        db.add(
            Gpu(
                id=f"cap-gpu-{idx:02d}",
                gpu_uuid=f"cap-{uuid.uuid4().hex[:10]}",
                host_id=f"cap-host-{host_ix}",
                model=f"cap-{gib}gib",
                memory_total=gib * 1024,
                gpu_index=idx,
                status=GpuStatus.AVAILABLE.value,
            )
        )
    db.commit()


def _drain(worker: OperationWorker, ticks: int = 40) -> int:
    """跑到没有可执行的 operation；返回跑了多少 tick。"""
    for i in range(ticks):
        if worker.tick_once() == 0:
            return i + 1
    return ticks


def run_size(size: int, rounds: int = 4, db_name: str = "warm-capacity", reserve: int = 0) -> dict:
    path = ROOT / f"{db_name}-{size}-{uuid.uuid4().hex[:6]}.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, expire_on_commit=False)
    workspace_root = Path(tempfile.mkdtemp(prefix="warm-capacity-"))
    try:
        provider = MockProvider("http://127.0.0.1:8000", workspace_root=workspace_root)
        orchestrator = WorkspaceOrchestrator(Factory, provider, workspace_root)
        manager = WarmPoolManager(
            Factory,
            orchestrator,
            SimpleNamespace(
                warm_pool_enabled=True, warm_pool_size=size, warm_pool_reserve_slots=reserve
            ),
        )
        with Factory() as db:
            _seed_fleet(db)
            seed_templates(db)
            templates = list(db.scalars(select(Template).where(Template.enabled.is_(True))))
            total_cards = len(FLEET)
            for _ in range(rounds):
                manager.maintain(db)
                _drain(OperationWorker(Factory, orchestrator))
                manager.maintain(db)  # 收割 PREWARMING → READY/FAILED
                _drain(OperationWorker(Factory, orchestrator))  # 清理 FAILED 的 DESTROY
            db.expunge_all()
            ready = int(
                db.scalar(
                    select(func.count(Workspace.id)).where(
                        Workspace.warm_pool_state == WarmPoolState.READY.value,
                        Workspace.deleted_at.is_(None),
                    )
                )
                or 0
            )
            warm_rows = int(
                db.scalar(
                    select(func.count(Workspace.id)).where(Workspace.name.like("warm-%"))
                )
                or 0
            )
            warm_tombstones = int(
                db.scalar(
                    select(func.count(Workspace.id)).where(
                        Workspace.name.like("warm-%"), Workspace.deleted_at.is_not(None)
                    )
                )
                or 0
            )
            provision_ops = int(
                db.scalar(
                    select(func.count(WorkspaceOperation.id)).where(
                        WorkspaceOperation.operation_type == OperationType.PROVISION.value
                    )
                )
                or 0
            )
            provision_failed = int(
                db.scalar(
                    select(func.count(WorkspaceOperation.id)).where(
                        WorkspaceOperation.operation_type == OperationType.PROVISION.value,
                        WorkspaceOperation.status == OperationStatus.FAILED.value,
                    )
                )
                or 0
            )
            provision_attempts = int(
                db.scalar(
                    select(func.coalesce(func.sum(WorkspaceOperation.attempts), 0)).where(
                        WorkspaceOperation.operation_type == OperationType.PROVISION.value
                    )
                )
                or 0
            )
            per_template = {}
            for template in templates:
                with Factory() as sdb:
                    states = sdb.execute(
                        select(Workspace.warm_pool_state, func.count(Workspace.id))
                        .where(
                            Workspace.template_id == template.id,
                            Workspace.deleted_at.is_(None),
                        )
                        .group_by(Workspace.warm_pool_state)
                    ).all()
                per_template[template.id] = {
                    "vram_gb": template.recommended_vram_gb,
                    "rows": {str(state): count for state, count in states},
                    "ready": int(
                        sum(c for s, c in states if s == WarmPoolState.READY.value) or 0
                    ),
                }
            free_cards = int(
                db.scalar(
                    select(func.count(Gpu.id)).where(Gpu.status == GpuStatus.AVAILABLE.value)
                )
                or 0
            )
            # 交互式请求：一次只问一个、问完就释放，否则测的是"5 个并发能不能同时塞进 8 张卡"
            # 而不是"池占着卡的时候用户起不起得来"（第一版就是这个错，读数 3/5 与池无关）。
            interactive = []
            for template in templates:
                probe = WorkspaceOrchestrator(Factory, provider, workspace_root)
                try:
                    ws = probe.create(
                        db, template, name=f"interactive-{template.id}",
                        user_id=None, organization_id=None,
                    )
                    ws_id = ws.id
                    db.commit()
                    probe._start(ws_id)
                    with Factory() as check:
                        row = check.get(Workspace, ws_id)
                        ok = row is not None and row.status == WorkspaceStatus.RUNNING.value
                        why = row.status if row is not None else "no-row"
                        if row is not None:
                            probe.destroy(db, row)
                            db.commit()
                    interactive.append({"template": template.id, "ok": ok, "why": why})
                except Exception as exc:
                    interactive.append({"template": template.id, "ok": False, "why": str(exc)[:70]})
            return {
                "warm_pool_size": size,
                "reserve_slots": reserve,
                "enabled_templates": len(templates),
                "requested_slots": size * len(templates),
                "fleet_cards": total_cards,
                "ready": ready,
                "warm_rows_created": warm_rows,
                "warm_tombstones": warm_tombstones,
                "provision_ops": provision_ops,
                "provision_failed": provision_failed,
                "provision_attempts": provision_attempts,
                "free_cards_after": free_cards,
                "interactive_ok": sum(1 for i in interactive if i["ok"]),
                "interactive_total": len(interactive),
                "interactive": interactive,
                "per_template": per_template,
            }
    finally:
        engine.dispose()
        path.unlink(missing_ok=True)
        shutil.rmtree(workspace_root, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON（可对账）")
    ap.add_argument("--sizes", default="1,2,4")
    ap.add_argument("--reserve", default="0", help="逗号分隔，与 --sizes 一一对应（各跑一遍）")
    args = ap.parse_args()
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    reserves = [int(s) for s in args.reserve.split(",") if s.strip()]
    if len(reserves) == 1:
        reserves = reserves * len(sizes)
    assert len(reserves) == len(sizes), f"--reserve 要给每个 size 一个数：{reserves} vs {sizes}"
    rows = [run_size(s, reserve=r) for s, r in zip(sizes, reserves, strict=True)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    header = (
        f"{'size':>4} {'预留':>4} {'需求位':>6} {'卡数':>4} {'READY':>6} {'建过的行':>8} "
        f"{'tombstone':>9} {'provision失败':>13} {'剩余空卡':>8} {'交互可起':>8}"
    )
    print(header)
    for r in rows:
        print(
            f"{r['warm_pool_size']:>4} {r['reserve_slots']:>4} {r['requested_slots']:>6} "
            f"{r['fleet_cards']:>4} "
            f"{r['ready']:>6} {r['warm_rows_created']:>8} {r['warm_tombstones']:>9} "
            f"{r['provision_failed']:>13} {r['free_cards_after']:>8} "
            f"{r['interactive_ok']}/{r['interactive_total']:<6}"
        )
    worst = min(rows, key=lambda r: r["interactive_ok"])
    print(
        f"\n[capacity] 生产默认 size=1 → 交互可起 "
        f"{next(r['interactive_ok'] for r in rows if r['warm_pool_size'] == 1)}"
        f"/{next(r['interactive_total'] for r in rows if r['warm_pool_size'] == 1)}；"
        f"抽得最狠的是 size={worst['warm_pool_size']}（需求 {worst['requested_slots']} 位 vs "
        f"{worst['fleet_cards']} 张卡），失败原因样例："
        + "；".join(
            f"{i['template']}→{i['why']}" for i in worst["interactive"] if not i["ok"]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

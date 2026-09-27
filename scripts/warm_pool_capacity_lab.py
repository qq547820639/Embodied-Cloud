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
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

# 本台子有两种活法：`make warm-capacity` 走 `-m scripts.…`（仓库根在 sys.path 上），
# 而人照着源码注释直接 `python scripts/warm_pool_capacity_lab.py` 时 sys.path[0] 是 scripts/，
# `import tests.…` 会当场 ModuleNotFoundError（本轮实测过）。常驻判据
# tests/test_warm_pool_labs.py::test_capacity_lab_runs_from_both_entry_points 两种写法都跑，
# 所以这里自己把仓库根挂上，不把"能跑"绑在某一种调用形状上。
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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


def gpu_side_counts(db) -> dict:
    """换一条查法：从 Gpu 侧数，专门用来复核读数（读数走 workspace 侧）。

    两条路必须给出同一个数，否则这张表——以及引用它的登记表 N-29／N-35——就是在说
    一句没法验证的话。`available_but_bound` 抓的是"卡已经放回 AVAILABLE 但还挂着
    workspace_id"这种没释放干净的状态。
    """
    allocated = int(
        db.scalar(select(func.count(Gpu.id)).where(Gpu.status == GpuStatus.ALLOCATED.value)) or 0
    )
    free = int(db.scalar(select(func.count(Gpu.id)).where(Gpu.status == GpuStatus.AVAILABLE.value)) or 0)
    ghost = int(
        db.scalar(
            select(func.count(Gpu.id)).where(
                Gpu.status == GpuStatus.AVAILABLE.value, Gpu.workspace_id.is_not(None)
            )
        )
        or 0
    )
    holders = (
        select(Gpu.workspace_id)
        .join(Workspace, Workspace.id == Gpu.workspace_id)
        .where(Workspace.warm_pool_state == WarmPoolState.READY.value, Workspace.deleted_at.is_(None))
        .distinct()
        .subquery()
    )
    ready_with_card = int(db.scalar(select(func.count()).select_from(holders)) or 0)
    return {
        "allocated": allocated,
        "free": free,
        "available_but_bound": ghost,
        "ready_with_card": ready_with_card,
    }


def recount_discrepancies(reading: dict, gpu: dict, enforce_ready: bool = True) -> list[str]:
    """纯比较器：读数 vs 独立复算。分母不成立时**不当作干净**。

    `enforce_ready=False` 给积压形状用——那时 worker 没跑，READY 本来就该是 0，
    拿"READY 格数 == 持卡数"去核会误开火。
    """
    out: list[str] = []
    if reading.get("fleet_cards", 0) <= 0:
        return ["fleet_cards<=0：复算的分母不成立，不能按「没有偏离」放行"]
    if gpu["allocated"] + gpu["free"] > reading["fleet_cards"]:
        out.append(
            f"卡数对不上：Gpu 侧 allocated {gpu['allocated']} + free {gpu['free']} "
            f"> 舰队 {reading['fleet_cards']}"
        )
    if reading.get("free_cards_after") != gpu["free"]:
        out.append(f"free_cards_after={reading.get('free_cards_after')} 与 Gpu 侧复算 {gpu['free']} 不一致")
    if gpu["available_but_bound"]:
        out.append(f"{gpu['available_but_bound']} 张 AVAILABLE 的卡还挂着 workspace_id：释放没做干净")
    if enforce_ready and reading.get("ready") != gpu["ready_with_card"]:
        out.append(
            f"ready={reading.get('ready')} 但 Gpu 侧只有 {gpu['ready_with_card']} 张卡属于 READY 格"
            "（有一格没拿到卡，或一格拿到了两张）"
        )
    return out


def run_size(
    size: int,
    rounds: int = 4,
    db_name: str = "warm-capacity",
    reserve: int = 0,
    drain_between: bool = True,
) -> dict:
    """一种形状的读数。

    `drain_between=True`（原有形状）：两遍 maintain 之间把 worker 排空，量的是
    "池追上目标之后与舰队的关系"。
    `drain_between=False`（积压形状，N-35 才需要）：两遍 maintain 之间**不**排空，
    于是补位仍在队列里、卡还没离开 AVAILABLE —— 这正是闸门只算一遍账时会超卖的形状。
    """
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
                if not drain_between:
                    manager.maintain(db)  # 第二遍：补位还在队列里，卡仍 AVAILABLE
                    break
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
            # 积压形状里不测交互式：那时卡还没被真占住（补位仍在队列里），
            # "用户照样起得来"是假读数，不如不报。
            for template in (templates if drain_between else []):
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
                "shape": "drained" if drain_between else "backlog",
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
                "recount_offenders": recount_discrepancies(
                    {
                        "ready": ready,
                        "free_cards_after": free_cards,
                        "fleet_cards": total_cards,
                    },
                    gpu_side_counts(db),
                    enforce_ready=drain_between,
                ),
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
    # 第二种形状：两遍 maintain 之间**不**排空 worker。N-35 那类"闸门只算一遍账"的
    # 超卖只在这一段窗口里露出来（原先这台子两遍之间都 drain，等于替被测者关上了窗口）。
    backlog = [
        run_size(s, rounds=1, reserve=r, drain_between=False)
        for s, r in zip(sizes, reserves, strict=True)
    ]
    offenders = [
        f"{r['shape']}#{r['warm_pool_size']}: {msg}"
        for r in rows + backlog
        for msg in r["recount_offenders"]
    ]
    if args.json:
        print(json.dumps(rows + backlog, ensure_ascii=False, indent=2))
        if offenders:
            print("[capacity] 复算不通过（读数与 Gpu 侧不一致）：", *offenders, sep="\n  - ", file=sys.stderr)
            return 1
        return 0
    header = (
        f"{'形状':>8} {'size':>4} {'预留':>4} {'需求位':>6} {'卡数':>4} {'READY':>6} {'建过的行':>8} "
        f"{'tombstone':>9} {'provision失败':>13} {'剩余空卡':>8} {'交互可起':>8}"
    )
    print(header)
    for r in rows + backlog:
        print(
            f"{r['shape']:>8} {r['warm_pool_size']:>4} {r['reserve_slots']:>4} {r['requested_slots']:>6} "
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
    if offenders:
        print("[capacity] 复算不通过（读数与 Gpu 侧不一致）：", *offenders, sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

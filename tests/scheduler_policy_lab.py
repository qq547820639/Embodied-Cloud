"""分配策略的可复现实测台：同一份 `GpuScheduler.allocate`，只换候选排序。

为什么要有这个东西：`allocate()` 的 ORDER BY 就是策略本身（best-fit：先用刚好够用的卡，
把大卡留给大任务），但一条写在 SQL 里的排序没人知道它值多少张卡——把它改成 `desc()`
今天不会产生任何红灯。这里把候选排序抽成可替换项，用**同一个分配器**跑**同一份工作负载**，
把几种策略的差别量成数：

    python -m tests.scheduler_policy_lab            # 打表
    python -m tests.scheduler_policy_lab --json     # 可对账的 JSON

设计要点（第一条读数就是照这几条量出来的，不是先写结论再挑数据）：

- 舰队总容量 **正好等于** 工作负载总需求：这时"全部接得下"只有靠不浪费才做得到，
  策略差异才会显形。第一轮我用了一个总需求 336 GiB、池子 192 GiB 的负载，四种策略
  一律"接 8 拒 8、剩余 0"——那份读数什么也分不出，只量出"池子不够"。
- 两台 host 各放 8/16/24/48 一张（组合相同），但**入库顺序与容量无关**：
  否则"按入库顺序"这种策略会因为 id 恰好等于大小序而与 best-fit 打平，
  那是建表顺序造出来的假平局，不是策略的性质。
- 容量数字是合成的（每档都取整 GiB），不是任何厂商的上报值；这里测的是排序策略在
  异构池上的取舍，不是真机行为。
"""

from __future__ import annotations

import argparse
import json
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Gpu, GpuHost, GpuStatus
from app.services import scheduler as sched
from app.services.scheduler import GpuScheduler

# (host 下标, GiB)。入库顺序刻意打乱：见模块 docstring 第三条。
FLEET: list[tuple[int, int]] = [
    (0, 24),
    (0, 8),
    (0, 48),
    (0, 16),
    (1, 8),
    (1, 48),
    (1, 16),
    (1, 24),
]
# 每个容量档各两张 = 恰好把两台机摆满；48 那两张排在最后，专门看小任务会不会把它们吃掉
WORKLOAD: list[int] = [8, 8, 16, 16, 24, 24, 48, 48]
BIG_GB = 48

POLICIES: dict[str, str] = {
    "best_fit": "memory_asc",       # 现产默认
    "worst_fit": "memory_desc",     # 先用最大的卡
    "arrival": "id_asc",            # 不看容量，按入库顺序（"随便挑一张够用的"）
    "pack_host": "host_then_size",  # 先把同一台机的卡填满，再谈大小
}


def order_expr(policy: str) -> list:
    """策略名 → ORDER BY 表达式。未登记的名字必须炸，不能悄悄退回默认。"""
    key = POLICIES.get(policy)
    if key == "memory_asc":
        return [Gpu.memory_total.asc()]
    if key == "memory_desc":
        return [Gpu.memory_total.desc()]
    if key == "id_asc":
        return [Gpu.id.asc()]
    if key == "host_then_size":
        return [Gpu.host_id.asc(), Gpu.memory_total.asc()]
    raise KeyError(f"未知策略 {policy!r}（可选：{sorted(POLICIES)}）")


def _build(db, fleet: list[tuple[int, int]]) -> None:
    hosts = [
        GpuHost(id=f"bench-host-{i}", name=f"bench-host-{i}", address="127.0.0.1", provider="mock")
        for i in range(max(h for h, _ in fleet) + 1)
    ]
    db.add_all(hosts)
    db.flush()
    for idx, (host_ix, gib) in enumerate(fleet):
        db.add(
            Gpu(
                id=f"bench-gpu-{idx:02d}",
                gpu_uuid=f"uuid-{uuid.uuid4().hex[:12]}",
                host_id=hosts[host_ix].id,
                model=f"bench-{gib}gib",
                memory_total=gib * 1024,
                gpu_index=idx,
                status=GpuStatus.AVAILABLE.value,
            )
        )
    db.commit()


def run_policy(policy: str, fleet: list[tuple[int, int]] = FLEET, workload: list[int] = WORKLOAD) -> dict:
    """在一个一次性文件库里跑完整工作负载，返回可对账的读数。

    只替换 `sched.candidate_order`（分配器本体一行不改），跑完复原——包括异常路径。
    """
    order = order_expr(policy)
    tmp = Path(tempfile.mkdtemp(prefix="sched-policy-"))
    engine = create_engine(f"sqlite:///{tmp / 'bench.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    real = sched.candidate_order
    sched.candidate_order = lambda: list(order)
    accepted: list[tuple[int, int]] = []  # (需求 GiB, 实占卡 GiB)
    rejected: list[int] = []
    try:
        with factory() as db:
            _build(db, fleet)
        scheduler = GpuScheduler(factory)
        for req in workload:
            with factory() as db:
                try:
                    gpu = scheduler.allocate(db, f"bench-{uuid.uuid4().hex[:8]}", req)
                except RuntimeError:
                    rejected.append(req)
                    continue
                accepted.append((req, gpu.memory_total // 1024))
        with factory() as db:
            allocated = list(db.scalars(select(Gpu).where(Gpu.status == GpuStatus.ALLOCATED.value)))
            available = list(db.scalars(select(Gpu).where(Gpu.status == GpuStatus.AVAILABLE.value)))
            per_host: dict[str, int] = {}
            for g in allocated:
                per_host[g.host_id] = per_host.get(g.host_id, 0) + 1
            free_gib = sum(g.memory_total for g in available) // 1024
            biggest_free_gib = max((g.memory_total for g in available), default=0) // 1024
    finally:
        sched.candidate_order = real
        Base.metadata.drop_all(engine)
        engine.dispose()
        (tmp / "bench.db").unlink(missing_ok=True)
        tmp.rmdir()
    want = sum(req for req, _ in accepted)
    held = sum(card for _, card in accepted)
    return {
        "policy": policy,
        "accepted": len(accepted),
        "rejected": len(rejected),
        "rejected_requests": rejected,
        "big_requests_served": sum(1 for req, _ in accepted if req >= BIG_GB),
        "requested_gib_served": want,
        "reserved_gib": held,
        "waste_ratio": round(held / want, 4) if want else None,
        "free_gib_after": free_gib,
        "biggest_free_card_gib": biggest_free_gib,
        "cards_per_host": dict(sorted(per_host.items())),
        "placement": [f"{req}->{card}" for req, card in accepted],
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="scheduler-policy-lab")
    parser.add_argument("--json", action="store_true", help="输出 JSON（默认打表）")
    args = parser.parse_args()
    rows = [run_policy(name) for name in POLICIES]
    if args.json:
        print(
            json.dumps(
                {"fleet": [f"h{h}:{g}" for h, g in FLEET], "workload": WORKLOAD, "results": rows},
                ensure_ascii=False,
            )
        )
        return 0
    total = sum(g for _, g in FLEET)
    print(f"fleet={total} GiB {FLEET}")
    print(f"workload={WORKLOAD} (合计 {sum(WORKLOAD)} GiB)")
    print(
        f"{'policy':<11} {'accept':>6} {'reject':>6} {'big48':>6} "
        f"{'waste':>6} {'freeG':>6} {'maxFree':>7}  placement"
    )
    for r in rows:
        print(
            f"{r['policy']:<11} {r['accepted']:>6} {r['rejected']:>6} {r['big_requests_served']:>6} "
            f"{r['waste_ratio']:>6} {r['free_gib_after']:>6} {r['biggest_free_card_gib']:>7}  "
            f"{' '.join(r['placement'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def production_policy() -> str:
    """现产 `candidate_order()` 对应实验室里哪个策略名（按表达式比对，不按字面量猜）。

    常驻判据靠它把"生产默认"与"实测结论"绑在一起：谁把 ORDER BY 换向，
    这里就会认出它是 worst_fit，而 worst_fit 接不满池子 ⇒ 判据当场红。
    """
    live = [str(e) for e in sched.candidate_order()]
    for name in POLICIES:
        if [str(e) for e in order_expr(name)] == live:
            return name
    raise AssertionError(f"生产排序不属于任何已测策略：{live}")

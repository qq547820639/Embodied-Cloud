"""GPU 分配策略（`allocate()` 的 ORDER BY）是被量过、并被钉住的，不是习惯。

现产是 best-fit（先用刚好够用的卡）。这条排序过去写在 SQL 里没人知道它值多少：
把它改成 `desc()` 全套用例照绿。这里用 `tests/scheduler_policy_lab` 的**同一个分配器**
跑同一份工作负载，把四种候选量成一张表，然后：

1. 断言"生产用的那一档"能接满池子（按表达式比对认档，不比对字面量，换向即红）；
2. 断言其余三档都**接不满**（这就是 best-fit 值多少的证据）；
3. 断言实测表本身没漂（数字写死，改分配器或改夹具都会露出来）。

舰队/负载是合成的，只回答"排序策略的取舍"，不涉及真机（见 lab 的 docstring）。
"""

from app.services import scheduler as sched
from tests.scheduler_policy_lab import FLEET, POLICIES, WORKLOAD, production_policy, run_policy

# 本轮实测（python -m tests.scheduler_policy_lab）：
#   best_fit   accept=8 reject=0  8->8 8->8 16->16 16->16 24->24 24->24 48->48 48->48  waste=1.0
#   worst_fit  accept=4 reject=4  8->48 8->48 16->24 16->24                            waste=3.0
#   arrival    accept=6 reject=2  8->24 8->8 16->48 16->16 24->48 24->24               waste=1.75
#   pack_host  accept=6 reject=2  8->8 8->16 16->24 16->48 24->24 24->48               waste=1.75
EXPECTED_ACCEPTED = {"best_fit": 8, "worst_fit": 4, "arrival": 6, "pack_host": 6}


def test_policies_were_all_measured_on_the_same_rig():
    rows = {p: run_policy(p) for p in POLICIES}
    assert {p: r["accepted"] for p, r in rows.items()} == EXPECTED_ACCEPTED, (
        "实测表漂了：分配器、舰队或工作负载改过，请重跑 python -m tests.scheduler_policy_lab 再改这里"
    )
    # 负载与池子等容量：只有不浪费才接得满
    assert sum(WORKLOAD) == sum(g for _, g in FLEET) == 192


def test_production_policy_is_the_only_one_that_fills_the_pool():
    """把"生产用哪一档"与"哪一档接得满"绑成一条断言。

    `production_policy()` 按 ORDER BY 表达式比对认档：改成 desc 就会被认成 worst_fit，
    而 worst_fit 只接 4/8 ⇒ 这条立刻红。放在一条里是为了不让"默认值仍是 asc"这种
    字面量断言冒充"策略有效"的证据。
    """
    # 先按表达式直比一次，不经过实验室的认档函数：认档函数本身也是可篡改的一面，
    # 只信它就留下"认档永远报 best_fit + 生产改向"这条组合逃逸路径（实测见 CHANGELOG）。
    from app.models import Gpu

    assert [str(e) for e in sched.candidate_order()] == [str(Gpu.memory_total.asc())], (
        "现产排序不是 best-fit（这条不查实验室，只查生产）"
    )
    prod = production_policy()
    row = run_policy(prod)
    assert row["accepted"] == len(WORKLOAD), f"现产策略 {prod} 接不满池子：{row['placement']}"
    assert row["rejected_requests"] == []
    assert row["big_requests_served"] == 2, f"大卡被小任务吃掉了：{row['placement']}"
    for other, other_row in ((p, run_policy(p)) for p in POLICIES if p != prod):
        assert other_row["accepted"] < row["accepted"], (
            f"{other} 与现产策略 {prod} 打平，实测台已失去区分力（读数 {other_row['accepted']}）"
        )


def test_lab_leaves_the_production_order_alone():
    """跑完实验室必须把生产的 ORDER BY 复原；否则后面的用例会量到被换掉的排序。

    开头先认一次档：漏复原的破坏通常发生在**本支之前**跑过的用例里，只比对
    "自己前后"是看不见的（实测过一次：拔掉 finally 里的复原，本条照样绿）。
    """
    assert production_policy() == "best_fit", "进入本支时生产排序已不是默认档——有用例漏复原"
    before = [str(e) for e in sched.candidate_order()]
    for policy in POLICIES:
        run_policy(policy)
    assert [str(e) for e in sched.candidate_order()] == before
    assert production_policy() == "best_fit"
    # 同一档跑两次必须给同一份摆放（否则上面的表不可比对）
    assert run_policy("best_fit")["placement"] == run_policy("best_fit")["placement"]


def test_unknown_policy_name_is_a_hard_error():
    """未知策略名不许悄悄退回默认：那会让整张表读成"四种策略都一样"。"""
    import pytest

    with pytest.raises(KeyError, match="未知策略"):
        run_policy("surprise_me")

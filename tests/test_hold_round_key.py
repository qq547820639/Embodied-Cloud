"""§18 hold 的幂等键按"启动轮次"发，而不是每个 workspace 一辈子一次（N-66 修复轮的判据）。

缺陷（/tmp 探针实测，不是推演）：`reserve_launch` 用 `f"hold:{workspace_id}"` 当键，
而 `CreditHold.idempotency_key` 是全局唯一列（`app/models.py:447`）；第一次启动把那条
pending capture 掉之后，同一 workspace 再次启动时 INSERT 必然撞唯一键，而改前的兜底
查询只按 key 查、不看状态 ⇒ 把上一轮**已 capture 的行**当成本轮授权返回。磁盘上该
workspace 的 pending 数为 0、`available_credits` 一分不减，而调用方
（`orchestrator.py:190`）把返回值丢掉 —— 预授权对第二次启动静默失效。

读数（改前）：`reserve(ws-A)` → `('hold:ws-A','pending',300)`、available 9700；
capture 之后再 `reserve(ws-A)` → `('hold:ws-A','captured',300)`、pending 数 0、
available 回到 10000。对照档全新 ws-B 仍能拿到 pending 300。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import Base
from app.models import CreditHold, HoldStatus
from tests.test_credit_holds import (
    ENGINE,
    Factory,
    get_user,
    make_policy,
    seed_user,
    seed_workspace,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BILLING_SRC = REPO_ROOT / "app" / "services" / "billing.py"

REQUIRED = 300  # make_policy: minimum_launch_minutes=5 → 5×60 credits


@pytest.fixture(autouse=True)
def _fresh_db():
    """自己建表：夹具不会跨模块继承（借了兄弟模块的 ENGINE，就得自己负责清库）。

    沿用 test_credit_holds 的取径——只 drop/create，不 unlink 文件：模块级 ENGINE
    仍连着同一个 inode，删文件会让下一条用例撞上 "attempt to write a readonly database"。
    """
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


def _rows(db, workspace_id: str) -> list[CreditHold]:
    return list(
        db.scalars(
            select(CreditHold).where(CreditHold.workspace_id == workspace_id).order_by(
                CreditHold.idempotency_key
            )
        )
    )


def test_second_launch_after_capture_gets_a_pending_hold():
    """本轮修掉的那一格：capture 过一轮之后，下一次启动必须重新圈住额度。"""
    user_id, _ = seed_user(credits=10000)
    ws = seed_workspace(user_id, "ws-a")
    policy = make_policy()
    with Factory() as db:
        first = policy.reserve_launch(db, get_user(user_id), ws)
        assert (first.status, first.amount) == (HoldStatus.PENDING.value, REQUIRED)
        policy.capture_hold(db, ws, usage_seconds=100, ledger_usage_key="usage:ws-a:t0")

    with Factory() as db:
        user = get_user(user_id)
        before = policy.available_credits(db, user)
        second = policy.reserve_launch(db, user, ws)
        assert second.status == HoldStatus.PENDING.value, (
            f"改前这里返回的是上一轮已 {second.status} 的行：本轮没有任何额度被圈住"
        )
        assert second.id != first.id
        # 圈住了才算授权：可用额必须真的减掉 REQUIRED
        assert policy.available_credits(db, get_user(user_id)) == before - REQUIRED


def test_repeat_within_a_round_is_idempotent():
    """合规侧（改前也绿）：同一轮内重复调用收敛到同一条 pending，不重复圈钱。"""
    user_id, _ = seed_user(credits=10000)
    ws = seed_workspace(user_id, "ws-a")
    policy = make_policy()
    with Factory() as db:
        user = get_user(user_id)
        first = policy.reserve_launch(db, user, ws)
        second = policy.reserve_launch(db, user, ws)
        assert first.id == second.id
        assert len(_rows(db, ws)) == 1
        assert policy.available_credits(db, user) == 10000 - REQUIRED


def test_release_then_reserve_starts_a_new_round():
    """启动失败把额度退回之后，重试必须能重新圈住（`_fail` 的注释就是这么承诺的）。"""
    user_id, _ = seed_user(credits=10000)
    ws = seed_workspace(user_id, "ws-a")
    policy = make_policy()
    with Factory() as db:
        policy.reserve_launch(db, get_user(user_id), ws)
        policy.release_hold(db, ws, reason="provision failed")

    with Factory() as db:
        hold = policy.reserve_launch(db, get_user(user_id), ws)
        assert hold.status == HoldStatus.PENDING.value
        assert policy.available_credits(db, get_user(user_id)) == 10000 - REQUIRED


def test_each_launch_leaves_its_own_audit_row():
    """每一轮各留一行、键互不相同：'这次启动是被哪笔授权放行的' 必须还查得出来。"""
    user_id, _ = seed_user(credits=30000)
    ws = seed_workspace(user_id, "ws-a")
    policy = make_policy()
    for _ in range(2):
        with Factory() as db:
            policy.reserve_launch(db, get_user(user_id), ws)
            policy.capture_hold(db, ws, usage_seconds=50, ledger_usage_key="usage:ws-a:t")
    with Factory() as db:
        policy.reserve_launch(db, get_user(user_id), ws)

    with Factory() as db:
        rows = _rows(db, ws)
        assert [r.status for r in rows] == [
            HoldStatus.CAPTURED.value,
            HoldStatus.CAPTURED.value,
            HoldStatus.PENDING.value,
        ]
        assert len({r.idempotency_key for r in rows}) == 3, "键 reused = 上一轮的行被当成本轮授权"


def test_reserve_never_hands_back_a_terminal_row():
    """跨四种真实序列的公共不变量：`reserve_launch` 的返回值只能是 pending。

    兜底路径（撞唯一键后回查）是这条性质的关键：它一旦不带状态过滤，
    "已花掉的额度"就会被当成"新的授权"发出去。
    """
    user_id, _ = seed_user(credits=50000)
    ws = seed_workspace(user_id, "ws-a")
    policy = make_policy()
    sequences = ["repeat", "capture-then-reserve", "release-then-reserve", "capture-twice"]
    for seq in sequences:
        with Factory() as db:
            user = get_user(user_id)
            hold = policy.reserve_launch(db, user, ws)
            assert hold.status == HoldStatus.PENDING.value, seq
            if seq == "repeat":
                hold = policy.reserve_launch(db, user, ws)
            elif seq == "capture-then-reserve":
                policy.capture_hold(db, ws, usage_seconds=10, ledger_usage_key="usage:ws-a:k1")
                db.expire_all()
                hold = policy.reserve_launch(db, user, ws)
            elif seq == "release-then-reserve":
                policy.release_hold(db, ws, reason="provision failed")
                db.expire_all()
                hold = policy.reserve_launch(db, user, ws)
            else:
                policy.capture_hold(db, ws, usage_seconds=10, ledger_usage_key="usage:ws-a:k2")
                db.expire_all()
                policy.reserve_launch(db, user, ws)
                policy.capture_hold(db, ws, usage_seconds=10, ledger_usage_key="usage:ws-a:k3")
                db.expire_all()
                hold = policy.reserve_launch(db, user, ws)
            assert hold.status == HoldStatus.PENDING.value, seq


# ---------------------------------------------------------------------------
# 键的形状：一个 workspace 一个键 = 缺陷本身；轮次号必须在模板里
# ---------------------------------------------------------------------------


def hold_key_template_arity(source: str) -> int:
    """返回 `f"hold:..."` 模板里的占位符个数；0 = 这个字面形状已经不在源码里。

    0 与 1 都要被拒：1 表示键只按 workspace 发（一辈子一次），0 表示判据读不到东西
    （模板换了形状却没换判据 = 假绿）。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        literal = "".join(
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
        if literal.startswith("hold:"):
            return sum(1 for v in node.values if isinstance(v, ast.FormattedValue))
    return 0


def test_hold_key_template_carries_the_round_and_exists_once():
    src = BILLING_SRC.read_text(encoding="utf-8")
    assert hold_key_template_arity(src) == 2, "键模板必须同时含 workspace 与轮次号"


def test_the_key_arity_checker_rejects_the_old_workspace_only_form():
    """反向对照：改前那个只带 workspace 的键形状必须被判读成 1（不合格）。"""
    old = 'def reserve_launch(db, workspace_id):\n    return f"hold:{workspace_id}"\n'
    assert hold_key_template_arity(old) == 1
    # 另一极：模板形状改了、判据不许悄悄读到 0 还报绿
    gone = "def reserve_launch(db, workspace_id):\n    return build_key(workspace_id)\n"
    assert hold_key_template_arity(gone) == 0

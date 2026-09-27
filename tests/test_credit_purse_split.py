"""两个钱包必须不相交：成员行不算进组织池（N-65）。

缺陷（实测）：`available_credits` 把 `balance(user)` 与 `organization_balance(org)` 直接
相加，而账本行常态是**同时**带 `user_id` 与 `organization_id`（`ledger.py:100-111` 的
usage、`routers/usage.py:68-77` 的充值都两个一起写）。两个谓词按构造相交 ⇒
给 a1 充 1000：a1 可用 2000（应然 1000），同组织从未出钱的 b1 可用 1000（应然 0）
——b1 可以直接通过 `check_launch_eligible` 开走计费 GPU。反向也一样：a1 消耗 300 会把
b1 的可用额扣掉 300。

分池口径不是我发明的：`tests/test_credit_holds.py::test_org_credits_are_visible_but_hold_is_taken_once`
的既有用意就是"组织充值只写 `organization_id`、不写 `user_id`"（:278-284，断言 5100=100+5000），
`billing.py` 的 reserve 注释也写"记账账户：优先个人"。所以组织池的定义应是
**无主行**（`user_id IS NULL`），个人池收该用户全部行，两池不相交。

写侧的"一行只能有一个归属"（Odoo `_check_accountable_required_fields` 那种 CHECK 约束、
PostgreSQL ddl.sgml:602-616 的 CHECK 语义）本轮没做：`models.py:400` 明写
"append-only，不回填改写"，而 CHECK 只约束新行 ⇒ 历史双主行仍需读侧规则归池。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    LedgerType,
    Organization,
    Role,
    Template,
    User,
)
from app.services.billing import BillingError, BillingPolicy
from app.services.ledger import CreditLedgerService
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("purse-split"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"


@pytest.fixture(autouse=True)
def _db():
    """每例重建表并灌入固定世界：同组织两成员（a1/b1）＋一个无组织成员（s1）＋模板。

    钱一行都不预先写——每个用例自己决定谁的钱包进钱，这正是本轮要断言的那件事。
    """
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    with Factory() as db:
        db.add(Organization(id="o1", name="org1"))
        for uid in ("a1", "b1"):
            db.add(
                User(
                    id=uid,
                    email=f"{uid}@x",
                    username=uid,
                    password_hash="x",  # noqa: S106 测试桩用户，非真实密码
                    role=Role.USER.value,
                    organization_id="o1",
                )
            )
        db.add(
            User(
                id="s1",
                email="s1@x",
                username="s1",
                password_hash="x",  # noqa: S106 测试桩用户，非真实密码
                role=Role.USER.value,
            )
        )
        db.add(
            Template(
                id="cartpole",
                slug="cartpole",
                name="cartpole",
                description="test",
                category="test",
                runtime="mock",
                launch_command="echo ok",
                enabled=True,
                recommended_vram_gb=16,
                estimated_hourly_cost_cny=1.0,
            )
        )
        db.commit()
    yield
    Base.metadata.drop_all(ENGINE)


def _recharge(ledger, db, *, amount, user_id=None, organization_id=None, key="r1"):
    return ledger.record(
        db,
        type=LedgerType.RECHARGE,
        amount=amount,
        user_id=user_id,
        organization_id=organization_id,
        description="recharge",
        idempotency_key=key,
    )


def _available(policy, db, uid: str) -> int:
    return policy.available_credits(db, db.get(User, uid))


def test_member_recharge_is_counted_once():
    """真实写入形状（两个归属都填）只该加进个人池一次。"""
    ledger, policy = world_live(Factory)
    with Factory() as db:
        _recharge(ledger, db, amount=1000, user_id="a1", organization_id="o1")
        assert ledger.balance(db, "a1") == 1000
        assert ledger.organization_balance(db, "o1") == 0, "成员行不该算进组织池"
        assert _available(policy, db, "a1") == 1000


def test_co_member_gets_nothing_from_your_recharge():
    """本轮要堵的那扇门：同组织、从未出钱的人可用额必须是 0。

    两档判据分开钉（`config.py:56` 出厂默认 `billing_enforce_preauthorization=False`，
    此时门禁只看"余额是否为负"，所以 0 余额仍可开机——那是另一格，见登记项 N-71）：
    - 预授权关闭档：可用额从 1000 归 0 就是本轮的修复本体（改前 b1 白拿 a1 的钱）；
    - 预授权开启档（生产配置）：0 可用额必须被拒，且同一笔钱下的 a1 必须放行。
    """
    ledger, _ = world_live(Factory)
    strict = BillingPolicy(Factory, ledger, minimum_launch_minutes=5, enforce_preauthorization=True)
    with Factory() as db:
        _recharge(ledger, db, amount=1000, user_id="a1", organization_id="o1")
        assert _available(strict, db, "a1") == 1000
        assert _available(strict, db, "b1") == 0, "同组织未出钱的人不该继承别人的充值"

        b1 = db.get(User, "b1")
        a1 = db.get(User, "a1")
        template = db.get(Template, "cartpole")
        with pytest.raises(BillingError, match="insufficient credits for launch"):
            strict.check_launch_eligible(db, b1, template)
        strict.check_launch_eligible(db, a1, template)  # 合规侧：有钱的那个必须放行


def test_org_pool_row_without_owner_is_shared():
    """另一极：组织自己的入账（无 user_id）对成员可用——这是产品要的能力，不能被
    分池规则一起关掉。`test_credit_holds.py:274-297` 钉的就是这一档。"""
    ledger, policy = world_live(Factory)
    with Factory() as db:
        _recharge(ledger, db, amount=5000, organization_id="o1", key="org-1")
        assert ledger.organization_balance(db, "o1") == 5000
        assert ledger.balance(db, "a1") == 0
        assert _available(policy, db, "a1") == 5000
        assert _available(policy, db, "b1") == 5000
        # 个人行与组织行并存时相加不相交：100 + 5000 = 5100（与既有用例同数）
        _recharge(ledger, db, amount=100, user_id="a1", key="p1")
        assert _available(policy, db, "a1") == 5100
        assert _available(policy, db, "b1") == 5000


def test_usage_reduces_only_the_paying_member():
    """消耗侧同样不能跨人扣：a1 跑掉 300 秒，b1 的可用额不该跟着少 300。"""
    ledger, policy = world_live(Factory)
    with Factory() as db:
        _recharge(ledger, db, amount=1000, user_id="b1", organization_id="o1", key="rb")
        before = _available(policy, db, "b1")
        ledger.record(
            db,
            type=LedgerType.USAGE,
            amount=-300,
            user_id="a1",
            organization_id="o1",
            description="gpu",
            idempotency_key="usage:a1:t0",
        )
        assert before == 1000
        assert _available(policy, db, "b1") == 1000, "同伴的消耗扣到了未出钱的人头上"
        assert _available(policy, db, "a1") == -300


def test_user_without_organization_is_unaffected():
    """无组织档（合规侧）：只数个人行，和组织池完全无关。"""
    ledger, policy = world_live(Factory)
    with Factory() as db:
        _recharge(ledger, db, amount=500, user_id="s1", key="s1r")
        assert _available(policy, db, "s1") == 500
        assert ledger.organization_balance(db, "o1") == 0


# ---------------------------------------------------------------------------
# 一份加法：三处副本合一，且尺子自己会开火
# ---------------------------------------------------------------------------


def count_balance_additions(source: str) -> int:
    """数"把 balance()/organization_balance() 的结果相加"的表达式出现几次。

    按 AST 判（BinOp Add / AugAssign add），不按文本判：注释里写"个人 + 组织"不算一份实现。
    """
    tree = ast.parse(source)
    counted = 0
    for node in ast.walk(tree):
        adds: list[ast.expr] = []
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            adds = [node.left, node.right]
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
            adds = [node.value]
        for operand in adds:
            if _is_balance_call(operand):
                counted += 1
                break
    return counted


def _is_balance_call(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"balance", "organization_balance", "gross_credits"}
    )


def _app_sources() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_gross_sum_is_written_exactly_once_in_app():
    """`gross_credits` 全仓一份定义；把余额相加的表达式在 app/ 里只出现一处。

    改前三处各抄一遍（`available_credits`、`reserve_launch`、monitor 的投影余额），
    任何一处改动都会让三个判据口径分叉。
    """
    defs = 0
    for path in _app_sources():
        src = path.read_text(encoding="utf-8")
        if path.name == "billing.py":
            defs += src.count("def gross_credits(")
        n = count_balance_additions(src)
        assert n == 0 or path.name == "billing.py", f"{path} 里另有一份相加"
        assert n <= 1, f"{path} 有 {n} 份相加"
    assert defs == 1


def test_the_addition_counter_can_see_a_second_copy():
    """反向对照：再加一份相加（回到改前形状），尺子必须数出 2。

    没有这一支，上一条在"尺子什么都数不到"时会一直绿。
    """
    single = (
        "def gross_credits(db, user):\n"
        "    gross = self.ledger.balance(db, user.id)\n"
        "    gross += self.ledger.organization_balance(db, user.organization_id)\n"
        "    return gross\n"
    )
    assert count_balance_additions(single) == 1
    doubled = single + (
        "def available_credits(db, user):\n"
        "    return self.ledger.balance(db, user.id) "
        "+ self.ledger.organization_balance(db, user.organization_id)\n"
    )
    assert count_balance_additions(doubled) == 2


def test_consumers_all_read_the_single_sum():
    """三处判据都改读 gross_credits：可用额、预授权额度、monitor 投影余额。"""
    billing_src = (APP_DIR / "services" / "billing.py").read_text(encoding="utf-8")
    orch_src = (APP_DIR / "services" / "orchestrator.py").read_text(encoding="utf-8")
    assert billing_src.count("self.gross_credits(db, user)") == 2, billing_src.count(
        "self.gross_credits(db, user)"
    )
    assert orch_src.count("self.billing.gross_credits(db, user)") == 1
    # 加法与 hold 扣减的顺序也留在 available_credits 里（不是分两处判据）
    assert "pending_hold_total" in billing_src


def world_live(factory) -> tuple[CreditLedgerService, BillingPolicy]:
    """与 `world` 夹具等价的函数版（参数化夹具在跨用例复用时更直白）。"""
    ledger = CreditLedgerService(factory)
    return ledger, BillingPolicy(factory, ledger)

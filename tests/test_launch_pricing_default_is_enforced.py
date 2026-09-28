"""启动定价的默认档必须真的生效（N-72，闭登记项 N-72）。

缺陷形状不是"算错了钱"，而是**一个存在但没人走的路径**：§18 把预授权（`CreditHold`
圈额度、结算转正、超时回收）整套建好了，而 `Settings.billing_enforce_preauthorization`
出厂是 `False` ⇒ 生产部署里这套机制一行都不跑；任何余额 ≥ 0 的账户都能开 GPU，钱在第一次
结算时变成负数，只靠配额 monitor 兜。全仓唯一的 RECHARGE 写点是 `app/routers/usage.py:68`
的充值端点（没有注册送额度的动作）⇒ 新账户可用额恒为 0。

定档依据（本机打开读过的原文，不是记忆）：
- Vast.ai 计费文档：新算力要先有钱 —— "Vast requires pre-payment of credits for GPU rentals"；
  运行中的实例在耗尽时被 "stopped automatically"，并允许一段
  "the system allows a short grace period where your balance may go negative"。
  这正好是两半：**启动前圈额度（预授权）＋ 运行中由配额 monitor 兜（已有的那半）**。
- 反面教材是"默认值能让整套机制变哑"这件事本身（为 N-70 打开 docker-py 时读到的
  类型分层：404 与"问不到"是两个判决，默认值把两者合一就会误判）。

改动：`billing_enforce_preauthorization` 默认 True（演示部署显式设 false 即回旧行为）；
新增 `billing_signup_credits = 300`，`POST /api/auth/register` 注册时向**个人**池记一笔
RECHARGE（幂等键 `signup:<user_id>`）。只进个人池是 N-71 的口径：组织池只数
`user_id IS NULL` 的无主行，这里把 `organization_id` 一起写上就会让同一笔钱既算个人又算组织。
`BillingPolicy` 的构造默认**故意保持** False（几十个夹具靠它），所以这里用恒等判据钉
"生产那一个实例走的是 Settings 的值"，而不是要求两边同值。
"""

import ast
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.deps import billing, settings
from app.main import app
from app.services.billing import BillingPolicy
from tests.http_auth import PASSWORD, auth_headers

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTH_SRC = REPO_ROOT / "app" / "routers" / "auth.py"


def _register(client: TestClient, email: str, username: str, *, organization: str | None = None):
    """直发注册请求（共享夹具 `register_body` 不覆盖带机构名这一档）。"""
    payload = {"email": email, "username": username, "password": PASSWORD}
    if organization is not None:
        payload["organization_name"] = organization
    resp = client.post("/api/auth/register", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _token(body: dict) -> str:
    return body["token"]


def _first_template_id(client: TestClient, headers: dict) -> str:
    templates = client.get("/api/templates", headers=headers).json()
    assert templates, "夹具前提：模板目录为空，启动门禁无从判起"
    return templates[0]["id"]


# ---------------------------------------------------------------------------
# A 默认档：预授权在生产装配里是真的开着
# ---------------------------------------------------------------------------


def test_factory_default_enforces_preauthorization():
    """`Settings` 出厂默认＝True：关掉它必须是运维的显式动作，不是没想起来。"""
    assert Settings.model_fields["billing_enforce_preauthorization"].default is True


def test_production_wiring_does_not_use_its_own_default():
    """恒等判据：`deps.billing` 这个实例用的就是 Settings 的值，不是构造函数的默认。

    §8 立的规矩是"配置字段存在但对象用 constructor default"＝对运维的假承诺。
    这里比 identity 而不是比"两边默认值相等"：`BillingPolicy` 自己的默认故意留 False
    （见 `test_the_construction_default_is_still_opt_in`），要求相等反而是错的断言。
    """
    assert billing.enforce_preauthorization is settings.billing_enforce_preauthorization
    assert billing.minimum_launch_minutes == settings.billing_minimum_launch_minutes
    assert billing.hold_ttl_minutes == settings.billing_hold_ttl_minutes


def test_the_construction_default_is_still_opt_in():
    """另一极：直接 new 出来的 BillingPolicy 仍不强制预授权 —— 夹具不许被静默换档。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from tests.dbfiles import db_url

    engine = create_engine(db_url("launch-pricing-default"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    assert BillingPolicy(factory).enforce_preauthorization is False


# ---------------------------------------------------------------------------
# B 注册发体验额度：钱进个人池，且启动门禁真的在跑
# ---------------------------------------------------------------------------


def test_registration_grants_signup_credits_into_the_personal_purse():
    with TestClient(app) as client:
        token = _token(_register(client, "n72-grant@example.com", "n72-grant"))
        headers = auth_headers(token)
        usage = client.get("/api/usage", headers=headers).json()
        assert usage["credits_balance"] == settings.billing_signup_credits, usage


def test_signup_grant_never_joins_the_organization_purse():
    """带着机构名注册：这笔钱只能进个人池。

    N-71 之后组织池 = `user_id IS NULL` 的无主行；注册时若把 `organization_id` 一起写上，
    同一笔充值就会被个人数一遍、被组织再数一遍（正是 N-65 那个双主行 bug 的形状）。
    """
    with TestClient(app) as client:
        body = _register(client, "n72-org@example.com", "n72-org", organization="Lab N72")
        headers = auth_headers(body["token"])
        assert body["user"]["organization_id"], "夹具前提：这个用户应当归属一个机构"
        entries = client.get("/api/ledger", headers=headers).json()
        signup = [e for e in entries if e["description"] == "signup credits"]
        assert [e["amount"] for e in signup] == [settings.billing_signup_credits], entries
        assert all(e["organization_id"] is None for e in signup), signup


def test_zero_signup_credits_makes_the_launch_gate_fire(monkeypatch):
    """把体验额度调成 0：新账户启动必须被 402 挡下，且原因是预授权那一档。

    上一支只证明字段值，这一支证明门禁真会拦 —— 少了它，"默认开着"可以只是一个字符串。
    """
    monkeypatch.setattr(settings, "billing_signup_credits", 0)
    with TestClient(app) as client:
        token = _token(_register(client, "n72-broke@example.com", "n72-broke"))
        headers = auth_headers(token)
        assert client.get("/api/usage", headers=headers).json()["credits_balance"] == 0
        resp = client.post(
            "/api/workspaces",
            json={"name": "should not start", "template_id": _first_template_id(client, headers)},
            headers=headers,
        )
        assert resp.status_code == 402, resp.text
        assert "preauthorization" in resp.text or "minimum" in resp.text, resp.text


def test_funded_user_can_launch_under_the_default():
    """合规侧：默认档不是"谁都开不了" —— 有体验额度的新账户照样能开。"""
    with TestClient(app) as client:
        token = _token(_register(client, "n72-rich@example.com", "n72-rich"))
        headers = auth_headers(token)
        resp = client.post(
            "/api/workspaces",
            json={"name": "affordable", "template_id": _first_template_id(client, headers)},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# C 结构判据：注册那一笔只能写个人归属
# ---------------------------------------------------------------------------


def signup_grant_kwargs(source: str) -> list[str]:
    """检查"幂等键以 `signup:` 开头"的那次 `record(...)` 调用的归属参数。

    只认带 `signup:` 的那一笔，别的账本写入（充值、管理员调整）不归本判据管 ——
    它们的归属规则不同（充值按 N-71 写个人，组织充值写无主行）。
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        names = {kw.arg for kw in node.keywords if kw.arg}
        if "idempotency_key" not in names:
            continue
        key = next(kw for kw in node.keywords if kw.arg == "idempotency_key")
        if "signup:" not in ast.unparse(key.value):
            continue
        if "organization_id" in names:
            found.append(f"line {node.lineno}: 注册发放写了 organization_id")
        if "user_id" not in names:
            found.append(f"line {node.lineno}: 注册发放没写 user_id（会掉进组织池）")
    return found


def test_the_signup_grant_is_written_to_the_personal_purse_only():
    assert signup_grant_kwargs(AUTH_SRC.read_text(encoding="utf-8")) == []


def test_the_grant_shape_checker_can_see_the_double_attribution():
    """反向对照：两种错写法各自点名，两种合法写法不开火。

    不开火的形状里包括"充值端点那笔" —— 它不属于注册发放的口径，若被同一把尺子扫到，
    这条判据就会在合法改动上常红。
    """
    bad_org = (
        'ledger.record(db, type=LedgerType.RECHARGE, amount=1, user_id=u.id,\n'
        '    organization_id=u.organization_id, idempotency_key=f"signup:{u.id}")\n'
    )
    no_user = 'ledger.record(db, amount=1, idempotency_key=f"signup:{u.id}")\n'
    good = 'ledger.record(db, amount=1, user_id=u.id, idempotency_key=f"signup:{u.id}")\n'
    other = 'ledger.record(db, amount=1, user_id=u.id, idempotency_key=f"recharge:{u.id}")\n'
    assert signup_grant_kwargs(bad_org) == ["line 1: 注册发放写了 organization_id"]
    assert signup_grant_kwargs(no_user) == ["line 1: 注册发放没写 user_id（会掉进组织池）"]
    assert signup_grant_kwargs(good) == []
    assert signup_grant_kwargs(other) == []

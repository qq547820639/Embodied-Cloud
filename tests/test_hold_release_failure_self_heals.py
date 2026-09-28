"""hold 释放失败留下的那一笔 pending，由谁收（N-104，闭登记项 N-104）。

形状（读 `app/services/orchestrator.py:360-366`）：provision 失败时 `_fail` 第一件事是
把圈住的额度原样退回，而那一步被 `try/except` 包着——注释写「释放失败不得掩盖状态置位
（盲捕获有意，见 ADR 0002）」，except 里只有一条 `logger.warning`。也就是说**额度那一侧
被故意留在未完成态**，指望 TTL 清扫（`billing.release_expired_holds`）来补。

此前的覆盖情况：合规那一半有（`tests/test_credit_holds.py::test_failed_provision_releases_the_hold`
走 worker 真链路证明失败会把 hold 退回）；TTL 清扫本身有（同文件 `:235-249`，含「RUNNING 段的
hold 不许扫成 released」）；周期表里注册了它有（`tests/test_periodic_loop_and_boot_wiring.py`）。
**中间那段没有**：没有任何用例让 `release_hold` 真的抛错，也就没人证明
「抛错之后那一笔确实还在、状态置位确实没被带回、且最终由那次周期清扫收掉」。

三档：
- H1 抛错之后：`_fail` 不许把异常泄给调用面，状态照写 FAILED、原因照落，而那笔 hold 仍 PENDING、
  额度仍被圈着（`available` 比合规档少一整笔）。这一档量的不是"没漏"，而是"漏在哪张表上"。
- H2 死线之前：清扫**不许**提前收回这一笔——残留的补做是时间门控的，不是"下一次扫就清"。
- H3 死线之后：走 `orchestrator.release_expired_holds()`（`app/deps.py` 注册进周期表的那个入口）
  必须把它收掉并把额度还给用户。

H2/H3 之间只动 `expires_at` 一个变量，两档合起来才是"TTL 兜底"这句主张的形状：
既不是"永远收不回"，也不是"随手就收回"。

牙齿（五臂变异电池实测，2026-09-28；各臂恢复后 `cmp` 逐字节相同、`git diff app/` 为空、末跑 3 passed）：
基线与无关注释臂都 0 红。
- E1 摘掉 `_fail` 里那个 try/except（让 hold 的异常泄给调用面）⇒ 三支全红：它们共用同一个夹具，
  而夹具跑的就是 `_fail`。这一臂钉的是"释放失败不得掩盖状态置位"那半句，红的方式是异常穿出。
- E2 把清扫的时间谓词（`expires_at < utcnow()`）换成恒真 ⇒ **只红 H2**：死线这一半有独立的牙。
- E3 把清扫的 RUNNING 保护反过来（只收还在跑的）⇒ **只红 H3**：终态行该被收这一半也有独立的牙。
"""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    CreditHold,
    Gpu,
    GpuHost,
    GpuStatus,
    HoldStatus,
    LedgerType,
    Role,
    Template,
    User,
    Workspace,
    WorkspaceStatus,
)
from app.services.billing import BillingPolicy
from app.services.ledger import CreditLedgerService
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.mock import MockProvider
from app.utils import utcnow
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("hold-self-heal"), connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)
START_CREDITS = 1000
HOLD_AMOUNT = 300  # 5 分钟 × 60 秒，`minimum_launch_minutes` 的默认档


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield


def _hold_raising_fail(tmp_path: Path, monkeypatch) -> tuple[WorkspaceOrchestrator, BillingPolicy, str]:
    """造 `_fail` 的 hold 抛错档：预授权开、额度已圈住、`release_hold` 换成必抛。

    前置三件逐条断言（hold 真在 PENDING、available 真被圈住、状态真是 PROVISIONING）——
    它们就是后面三档的坐标系。
    """
    policy = BillingPolicy(
        Factory,
        CreditLedgerService(Factory),
        enforce_preauthorization=True,
        minimum_launch_minutes=5,
        hold_ttl_minutes=60,
    )
    orchestrator = WorkspaceOrchestrator(
        Factory,
        MockProvider("http://127.0.0.1:8000"),
        Path(tmp_path),
        ledger=CreditLedgerService(Factory),
        billing=policy,
    )
    with Factory() as db:
        db.add(
            User(id="u1", email="u1@x", username="u1", password_hash="x",  # noqa: S106
                 role=Role.STUDENT.value)
        )
        db.add(GpuHost(id="host-1", name="h1", address="127.0.0.1", provider="mock"))
        db.add(
            Template(
                id="cartpole", slug="cartpole", name="cartpole", description="d", category="c",
                runtime="mock", launch_command="echo ok", enabled=True,
                recommended_vram_gb=16, estimated_hourly_cost_cny=1.0,
            )
        )
        db.add(
            Gpu(
                id="gpu-1", gpu_uuid="gpu-uuid-1", host_id="host-1", model="RTX-1",
                memory_total=24564, gpu_index=0, status=GpuStatus.AVAILABLE.value,
            )
        )
        db.commit()
        # 余额住在账本里（`User` 没有 credits 列）：gross_credits 读的就是这笔 RECHARGE
        CreditLedgerService(Factory).record(
            db,
            type=LedgerType.RECHARGE,
            amount=START_CREDITS,
            user_id="u1",
            idempotency_key="recharge:u1",
        )
        db.commit()
        user = db.get(User, "u1")
        workspace = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        wid = workspace.id
        hold = policy.reserve_launch(db, user, wid)
        assert hold is not None and hold.status == HoldStatus.PENDING.value
        assert hold.amount == HOLD_AMOUNT, f"坐标系：一笔 hold 应当圈住 {HOLD_AMOUNT}"
        assert policy.available_credits(db, user) == START_CREDITS - HOLD_AMOUNT
        gpu = db.get(Gpu, "gpu-1")
        gpu.status = GpuStatus.ALLOCATED.value
        gpu.workspace_id = wid
        ws = db.get(Workspace, wid)
        ws.status = WorkspaceStatus.PROVISIONING.value
        ws.gpu_id, ws.gpu_index, ws.gpu_name = gpu.id, gpu.gpu_index, gpu.model
        db.commit()

    def _boom(db, workspace_id, *, reason):
        raise RuntimeError("simulated hold release failure")

    monkeypatch.setattr(policy, "release_hold", _boom)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        # 准入放行（mock 不再自述 ALIVE）⇒ 走到"卡交给 scheduler、hold 交给 billing"那一段
        orchestrator._fail(db, ws, "boom", command_succeeded=True)
        db.commit()
    return orchestrator, policy, wid


def _state(policy: BillingPolicy) -> dict:
    with Factory() as db:
        hold = db.scalar(select(CreditHold))
        return {
            "hold_status": hold.status if hold is not None else None,
            "hold_reason": hold.reason if hold is not None else None,
            "available": policy.available_credits(db, db.get(User, "u1")),
        }


def test_hold_release_failure_does_not_mask_the_status_write(tmp_path, monkeypatch) -> None:
    """H1：release_hold 抛错 ⇒ 状态与原因照写，而那笔额度确实还留在 PENDING、仍被圈着。"""
    _orchestrator, policy, wid = _hold_raising_fail(tmp_path, monkeypatch)

    state = _state(policy)
    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.FAILED.value, ws.status
        assert ws.error_message == "boom", ws.error_message
    assert state["hold_status"] == HoldStatus.PENDING.value, (
        f"抛错那一档没留下可补做的残留（{state}）⇒ H3 的量就没有对象"
    )
    assert state["available"] == START_CREDITS - HOLD_AMOUNT, (
        "额度已被偷偷退回：这一档就不是'靠 TTL 兜底'的形状"
    )


def test_the_sweep_does_not_take_the_hold_before_its_deadline(tmp_path, monkeypatch) -> None:
    """H2：死线还没到，清扫不许提前收——补做是时间门控的。"""
    orchestrator, policy, _wid = _hold_raising_fail(tmp_path, monkeypatch)

    released = orchestrator.release_expired_holds()

    assert released == 0, f"清扫提前收了这一笔：{released}"
    assert _state(policy)["hold_status"] == HoldStatus.PENDING.value
    assert _state(policy)["available"] == START_CREDITS - HOLD_AMOUNT


def test_the_periodic_sweep_returns_the_credit_the_failed_release_left_behind(
    tmp_path, monkeypatch
) -> None:
    """H3：死线一过，周期表里那个入口必须把它收掉并把额度还给用户。"""
    orchestrator, policy, _wid = _hold_raising_fail(tmp_path, monkeypatch)
    before = _state(policy)

    with Factory() as db:
        hold = db.scalar(select(CreditHold))
        hold.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()

    released = orchestrator.release_expired_holds()
    after = _state(policy)

    assert released == 1, f"清扫没收到这一笔：{released}"
    assert after["hold_status"] == HoldStatus.RELEASED.value, after
    assert after["available"] == START_CREDITS, f"额度没还回来：{after}"
    assert "hold expired" in (after["hold_reason"] or ""), after["hold_reason"]
    # 差分只属于这次清扫：H2 之前它一直是圈住的
    assert before["available"] == START_CREDITS - HOLD_AMOUNT

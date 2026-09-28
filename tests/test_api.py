from fastapi.testclient import TestClient

from app.deps import settings
from app.main import app
from tests.http_auth import auth_headers as _auth
from tests.http_auth import register_body
from tests.settle import await_workspace_settled


def _register(client: TestClient, email: str, username: str) -> str:
    body = register_body(client, email, username)
    return body["token"]


def test_end_to_end_workspace_lifecycle():
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["provider_ready"] is True

        token = _register(client, "e2e@example.com", "e2e-user")
        headers = _auth(token)

        templates = client.get("/api/templates").json()
        ids = {x["id"] for x in templates}
        assert {"cartpole", "franka-lift", "franka-pick-place", "domain-randomization", "rgbd-perception"} <= ids
        # Registry 完整性：version locked + 镜像版本化
        for t in templates:
            assert t["version"]
            assert "latest" not in (t["image"] or "")
            assert t["slug"]

        # 这一支要真起一个 workspace：空闲卡是前提，得自己达成并写明
        # （全套共用一个库 + mock 只 seed 8 张卡，见 tests/gpu_pool.py）
        from app.deps import SessionFactory
        from app.deps import scheduler as app_scheduler
        from tests.gpu_pool import ensure_free_gpus

        with SessionFactory() as db:
            ensure_free_gpus(db, app_scheduler)

        created = client.post(
            "/api/workspaces",
            json={"template_id": "cartpole", "auto_start": True},
            headers=headers,
        )
        assert created.status_code == 201
        workspace_id = created.json()["id"]

        state = await_workspace_settled(client, workspace_id, headers)
        assert state["status"] == "running"
        assert state["ide_url"].endswith(workspace_id)

        access = client.get(f"/api/workspaces/{workspace_id}/access", headers=headers)
        assert access.status_code == 200
        assert access.json()["ide_url"].endswith(workspace_id)
        assert access.json()["ide_password"]

        usage = client.get("/api/usage", headers=headers)
        assert usage.status_code == 200
        assert usage.json()["running_workspaces"] >= 1

        # 未认证访问 → 401
        assert client.get("/api/workspaces").status_code == 401
        assert client.get("/api/usage").status_code == 401

        stopped = client.post(f"/api/workspaces/{workspace_id}/stop", headers=headers)
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "stopped"

        # GPU 释放：mock GPU 应回到 available
        assert client.get("/api/gpus", headers=headers).status_code == 403  # 非 admin

        deleted = client.delete(f"/api/workspaces/{workspace_id}", headers=headers)
        assert deleted.status_code == 204
        assert client.get(f"/api/workspaces/{workspace_id}", headers=headers).status_code == 404


def test_workspace_delete_is_soft_tombstone():
    """删除语义：soft delete / tombstone —— 行保留（billing/audit），API 默认不返回。"""
    with TestClient(app) as client:
        token = _register(client, "softdel@example.com", "softdel-user")
        headers = _auth(token)

        created = client.post(
            "/api/workspaces", json={"template_id": "cartpole", "auto_start": False}, headers=headers
        )
        assert created.status_code == 201
        workspace_id = created.json()["id"]

        # 删除 → 204；行保留为 tombstone（DB 层验证）
        assert client.delete(f"/api/workspaces/{workspace_id}", headers=headers).status_code == 204

        # API 默认不返回 deleted workspace：list 不含、GET 404
        assert client.get("/api/workspaces", headers=headers).json() == []
        assert client.get(f"/api/workspaces/{workspace_id}", headers=headers).status_code == 404

        # 重复删除幂等（tombstone 后对普通 API 不可见 → 404，与 GET 语义一致）
        assert client.delete(f"/api/workspaces/{workspace_id}", headers=headers).status_code == 404

        # DB 层：行保留（DELETED + deleted_at）
        from app.deps import SessionFactory
        from app.models import Workspace, WorkspaceStatus

        with SessionFactory() as db:
            ws = db.get(Workspace, workspace_id)
            assert ws is not None  # 行未被物理删除
            assert ws.status == WorkspaceStatus.DELETED.value
            assert ws.deleted_at is not None

        # usage 不统计 tombstone
        usage = client.get("/api/usage", headers=headers).json()
        assert usage["total_workspaces"] == 0


def test_workspace_logs_endpoint_owner_isolated():
    """GET /api/workspaces/{id}/logs：owner 可读，越权 404。"""
    with TestClient(app) as client:
        token = _register(client, "logs@example.com", "logs-user")
        headers = _auth(token)
        created = client.post(
            "/api/workspaces", json={"template_id": "cartpole", "auto_start": True}, headers=headers
        )
        assert created.status_code == 201
        workspace_id = created.json()["id"]

        # 等到终态再读日志（前提，见 tests/settle.py）
        await_workspace_settled(client, workspace_id, headers)

        resp = client.get(f"/api/workspaces/{workspace_id}/logs", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["workspace_id"] == workspace_id
        assert "logs" in resp.json()

        # 越权用户 404
        token2 = _register(client, "logs2@example.com", "logs2-user")
        headers2 = _auth(token2)
        assert client.get(f"/api/workspaces/{workspace_id}/logs", headers=headers2).status_code == 404


def test_metrics_endpoint():
    with TestClient(app) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        body = resp.text
        for metric in [
            "workspace_launch_total",
            "workspace_launch_failed_total",
            "workspace_launch_seconds",
            "workspace_running",
            "gpu_allocated",
            "template_launch_total",
            "stream_session_total",
        ]:
            assert metric in body


def test_metrics_exposition_is_what_the_code_declares():
    """`/metrics` 实际暴露的序列与标签，必须能由 `app/metrics.py` 的声明派生。

    N-42 那两支核的是"文档 ↔ 声明"；这一支接上第三边——**跑出来的文本**。四条一起断：
    ①  exposition 解析不出任何序列 ⇒ 判据恒真，先红；
    ②  本仓前缀下出现的序列名必须在声明派生集合里（Counter 的 `_created`、Histogram 的
        `_bucket/_sum/_count` 算派生内）；
    ③  每条序列的标签键必须是声明标签的子集（`_bucket` 额外允许 `le`）；活进程里
        prometheus_client 自己就拒绝未声明的标签键，所以这条分支的开火证明放在解析层
        （`tests/test_metrics_spec.py` 的两支：`le` 不许漏给非 bucket 序列、喂一份带越界标签的文本）；
    ④  刚刚真跑过一次启动 ⇒ `workspace_launch_total` 必须带着 `template_id`+`provider` 出现
        （否则 ②③ 可以对着空集合恒真）。
    已知边界（N-44 收掉的那半）：划界不再靠"族名的第一段前缀来自声明"，而是问默认注册表
    "这个名字是不是由我们的 Counter/Gauge/Histogram 对象注册的"——全新前缀的族同样会被点名；
    prometheus_client 自带的平台收集器（`python_gc_*`/`python_info`…）由同一条判据排除在外。
    """
    from tests.metrics_spec import (
        allowed_series,
        declared_metrics,
        declared_series,
        expected_labels,
        parse_exposition,
        registered_names,
    )

    with TestClient(app) as client:
        token = _register(client, "metrics-shape@example.com", "metrics-shape")
        headers = _auth(token)
        created = client.post(
            "/api/workspaces", json={"template_id": "cartpole", "auto_start": True}, headers=headers
        )
        assert created.status_code == 201
        await_workspace_settled(client, created.json()["id"], headers)
        body = client.get("/metrics").text

    declared = declared_metrics()
    allowed = declared_series(declared)
    owners = registered_names()
    observed = parse_exposition(body)
    assert observed, "exposition 里一个序列都没解析出来：本判据恒真"

    unknown = sorted(name for name in observed if name not in allowed and owners.get(name, True))
    assert not unknown, f"暴露了声明之外的序列：{unknown}"

    extra_labels: dict[str, list[str]] = {}
    for series, keys in observed.items():
        family = allowed_series(declared).get(series)
        if family is None or not owners.get(series, True):
            continue
        expected = expected_labels(series, set(declared[family]["labels"]))  # type: ignore[arg-type]
        extra = sorted(keys - expected)
        if extra:
            extra_labels[series] = extra
    assert not extra_labels, f"这些序列带了声明外的标签：{extra_labels}"

    launch_labels = observed.get("workspace_launch_total", set())
    assert {"template_id", "provider"} <= launch_labels, (
        f"刚完成一次真启动，workspace_launch_total 却没带 template_id/provider：{sorted(launch_labels)}"
    )


def test_standalone_user_usage_balance_matches_ledger():
    """Regression: 无 organization 的 standalone user 有 CreditLedger 记录时，
    GET /api/usage 必须返回真实 balance，而不是 0。"""
    with TestClient(app) as client:
        token = _register(client, "standalone@example.com", "standalone-user")
        headers = _auth(token)

        # 充值前：/api/usage 报的余额必须等于账本之和（注册那笔体验额度也在内），
        # 而不是硬编码某个数 —— 本用例的主题是"两边对得上"，不是"余额恰好是 0"。
        usage0 = client.get("/api/usage", headers=headers)
        assert usage0.status_code == 200
        base = sum(e["amount"] for e in client.get("/api/ledger", headers=headers).json())
        assert usage0.json()["credits_balance"] == base
        # 体验额度确实进了**个人**池（N-71 之后组织池只数无主行，这里顺带钉住不串池）
        assert base == settings.billing_signup_credits, base

        # 充值 500 credits
        resp = client.post("/api/ledger/recharge", json={"amount": 500}, headers=headers)
        assert resp.status_code == 200, resp.text

        # GET /api/usage → credits_balance == ledger balance == 起点 + 500
        usage1 = client.get("/api/usage", headers=headers)
        assert usage1.status_code == 200
        assert usage1.json()["credits_balance"] == base + 500

        # 与 /api/ledger 聚合结果一致
        entries = client.get("/api/ledger", headers=headers).json()
        assert sum(e["amount"] for e in entries) == base + 500


def test_lifecycle_conflict_is_reported_instead_of_faking_success():
    """有 active operation 时 start/stop/delete 必须 409，不能谎报"已受理/已完成"。

    复现的真实缺陷（浏览器档实测抓到）：provisioning 未落定时点删除 →
    enqueue 被 uq_ops_active_per_workspace 挡下（worker 日志
    "skip enqueue destroy … active operation exists"），而端点仍返回 204：
    前端显示"已删除"，runtime 继续跑、GPU 继续占用、计费继续累加。
    """
    import uuid
    from datetime import timedelta

    from sqlalchemy import select

    from app.deps import SessionFactory
    from app.models import OperationStatus, OperationType, WorkspaceOperation
    from app.utils import utcnow

    with TestClient(app) as client:
        token = _register(client, "conflict-lifecycle@example.org", "conflict")
        headers = _auth(token)
        created = client.post(
            "/api/workspaces",
            json={"template_id": "cartpole", "auto_start": False},
            headers=headers,
        )
        assert created.status_code in (200, 201), created.text
        workspace_id = created.json()["id"]

        # 造一个"别人正在执行、且当前不可 reclaim"的 active operation：
        # RUNNING + 未过期 lease → _claim_next 两档都跳得过它，判据与后台 worker 无关
        with SessionFactory() as db:
            blocker = WorkspaceOperation(
                id=str(uuid.uuid4()),
                workspace_id=workspace_id,
                operation_type=OperationType.PROVISION.value,
                status=OperationStatus.RUNNING.value,
                attempts=1,
                lease_owner="other-worker",
                fencing_token="other-token",  # noqa: S106  测试用假 token，非真实凭据
                heartbeat_at=utcnow(),
                lease_expires_at=utcnow() + timedelta(hours=1),
            )
            db.add(blocker)
            db.commit()
            blocker_id = blocker.id

        started = client.post(f"/api/workspaces/{workspace_id}/start", headers=headers)
        assert started.status_code == 409, started.text
        # 冲突期不得先把状态翻成 QUEUED：那等于"前端显示排队中、队列里什么都没有"
        assert client.get(f"/api/workspaces/{workspace_id}", headers=headers).json()["status"] != "queued"

        stopped = client.post(f"/api/workspaces/{workspace_id}/stop", headers=headers)
        assert stopped.status_code == 409, stopped.text
        deleted = client.delete(f"/api/workspaces/{workspace_id}", headers=headers)
        assert deleted.status_code == 409, deleted.text
        # 未被谎报：仍在列表里，且没有产生 DESTROY/STOP operation 行
        listed = [w["id"] for w in client.get("/api/workspaces", headers=headers).json()]
        assert workspace_id in listed
        with SessionFactory() as db:
            ops = list(
                db.scalars(
                    select(WorkspaceOperation).where(WorkspaceOperation.workspace_id == workspace_id)
                )
            )
            assert [o.operation_type for o in ops] == [OperationType.PROVISION.value]

        # 反向对照（must-not-fire）：占用释放后，同一条 DELETE 必须正常成功
        with SessionFactory() as db:
            row = db.get(WorkspaceOperation, blocker_id)
            row.status = OperationStatus.SUCCEEDED.value
            row.lease_expires_at = utcnow() - timedelta(minutes=5)
            db.commit()
        assert client.delete(f"/api/workspaces/{workspace_id}", headers=headers).status_code == 204
        assert workspace_id not in [w["id"] for w in client.get("/api/workspaces", headers=headers).json()]

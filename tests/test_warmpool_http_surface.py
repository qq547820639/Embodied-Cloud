"""warm pool 观测/基准两端的 HTTP 面：谁能触发、能触发多大、跑完留不留东西。

起因（本轮实测，非推测）：`GET /api/streaming/warmpool/benchmark` 用 `CurrentUser`
（任何登录用户，无角色检查），而它调的 `benchmark_launch` 每轮 **create + `_start` 一个
真实 workspace**、`iterations` 只有下限（改造前是 `max(1, iterations)`），
并且创建的 workspace `user_id=None`、跑完不清理。四条各是一条常驻断言：
1. 非 admin 必须 403（与 `app/routers/gpus.py:13`、`usage.py:18` 同一条仓内惯例），
   且被拒之后不能已经留下东西；
2. `iterations` 必须有上限，且上限只有一份定义（路由与服务共用，避免两处各写一个数）；
3. 基准跑完不留活体：无主 bench workspace 必须被 tombstone；
4. 而且不留占卡——这一支有自己的反证（把 `scheduler.release` 换成空操作必须能读到卡被占着），
   否则"回收"的两半里只有一半被证明过。
"""

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.deps import SessionFactory
from app.main import app
from app.models import Gpu, GpuStatus, Role, User, Workspace
from tests.test_demo_workspace import _auth, _register

BENCH = "/api/streaming/warmpool/benchmark"
METRICS = "/api/streaming/warmpool/metrics"


def _promote(email: str) -> None:
    with SessionFactory() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = Role.ADMIN.value
        db.commit()


def _bench_ids() -> list[str]:
    with SessionFactory() as db:
        return list(db.scalars(select(Workspace.id).where(Workspace.name.like("bench-%"))))


def _live_bench_rows() -> int:
    with SessionFactory() as db:
        return int(
            db.scalar(
                select(func.count(Workspace.id)).where(
                    Workspace.name.like("bench-%"), Workspace.deleted_at.is_(None)
                )
            )
            or 0
        )


def _stuck_bench_gpus() -> int:
    """仍绑在 bench 行上的 ALLOCATED 卡数。

    只数**它自己那些行**，不数全库：全库断言（改造前的第一版）隔离跑绿、进全套就红，
    因为别的模块合法地留着自己的 running workspace——那是抢别人的账当判据。
    """
    with SessionFactory() as db:
        return int(
            db.scalar(
                select(func.count(Gpu.id)).where(
                    Gpu.status == GpuStatus.ALLOCATED.value,
                    Gpu.workspace_id.in_(_bench_ids()),
                )
            )
            or 0
        )


def _release_all_bench_gpus() -> None:
    """反证夹具用完自己收尾：把被 stub 掉的释放补做回去，别把卡留在账上。"""
    from app.deps import scheduler

    for wid in _bench_ids():
        with SessionFactory() as db:
            try:
                scheduler.release(db, wid)
            except Exception:
                db.rollback()


def test_benchmark_endpoint_rejects_non_admin():
    """普通用户不得借 GET 触发建卡：403，且一个 workspace 都不许多出来。"""
    with TestClient(app) as client:
        token = _register(client, "benchuser@example.com", "benchuser")
        before = _live_bench_rows()
        resp = client.get(BENCH, params={"template_id": "cartpole", "iterations": 1}, headers=_auth(token))
        assert resp.status_code == 403, resp.text
        assert _live_bench_rows() == before, "被拒的请求仍然建了 workspace：判据没量到副作用"


def test_metrics_endpoint_rejects_non_admin():
    """池水位是跨模板的全量视图（改造前实测普通用户拿到 5 个模板的水位），不该给普通用户。"""
    with TestClient(app) as client:
        token = _register(client, "benchwatch@example.com", "benchwatch")
        resp = client.get(METRICS, headers=_auth(token))
        assert resp.status_code == 403, resp.text


def test_admin_can_run_benchmark_and_it_is_bounded():
    """admin 能跑（200），但越界的 iterations 必须被路由挡在门外（422）而不是照跑。"""
    with TestClient(app) as client:
        token = _register(client, "benchadmin@example.com", "benchadmin")
        _promote("benchadmin@example.com")
        ok = client.get(BENCH, params={"template_id": "cartpole", "iterations": 1}, headers=_auth(token))
        assert ok.status_code == 200, ok.text
        assert {"p50_s", "p95_s", "samples", "iterations"} <= set(ok.json()), ok.json()
        assert ok.json()["iterations"] == 1

        too_big = client.get(
            BENCH, params={"template_id": "cartpole", "iterations": 21}, headers=_auth(token)
        )
        assert too_big.status_code == 422, too_big.text
        assert _live_bench_rows() == 0, f"被拒的越界请求留下了活体：{_live_bench_rows()}"


def test_benchmark_leaves_no_live_workspaces_and_no_allocated_gpus():
    """跑完必须自己收尾：无主 bench workspace 全部 tombstone，且没有一张卡还被它们占着。"""
    with TestClient(app) as client:
        token = _register(client, "benchclean@example.com", "benchclean")
        _promote("benchclean@example.com")
        resp = client.get(BENCH, params={"template_id": "cartpole", "iterations": 2}, headers=_auth(token))
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["samples"]) == 2
        assert _live_bench_rows() == 0, f"基准留下了 {_live_bench_rows()} 个活体 workspace"
        assert _stuck_bench_gpus() == 0, f"基准留下了 {_stuck_bench_gpus()} 张仍被占用的卡"


def test_the_allocated_gpu_clause_can_actually_fire(monkeypatch):
    """"不占卡"那一支要能开火：把 `scheduler.release` 换成空操作后必须读得到卡被占着。

    destroy 做两半（provider 清理 + 释放卡）。上一支用例的反证打在"不留活体"上
    （实测读数：跳过清理 → 留下 2 个活体），所以那一半已经有人证过；
    这一支单独证 GPU 那一半——tombstone 照做、只把释放抽掉，
    若此时仍读 0，说明这条断言是恒真的，得先修探针。
    """
    from app.deps import scheduler

    monkeypatch.setattr(scheduler, "release", lambda db, workspace_id: None)
    try:
        with TestClient(app) as client:
            token = _register(client, "benchgpu@example.com", "benchgpu")
            _promote("benchgpu@example.com")
            resp = client.get(
                BENCH, params={"template_id": "cartpole", "iterations": 1}, headers=_auth(token)
            )
            assert resp.status_code == 200, resp.text
            assert _live_bench_rows() == 0, "tombstone 仍应发生（否则两支控制混在一起，定不了责）"
            assert _stuck_bench_gpus() >= 1, (
                "release 被换成空操作之后仍读不到被占的卡 ⇒ GPU 那一支判据恒真，探针坏了"
            )
    finally:
        monkeypatch.undo()
        _release_all_bench_gpus()
    assert _stuck_bench_gpus() == 0, f"反证夹具没收尾，留了 {_stuck_bench_gpus()} 张卡"

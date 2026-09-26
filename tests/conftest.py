import os
import uuid
from pathlib import Path

import pg_server
import pytest

from tests.dbfiles import db_name, db_url

os.environ["EMBODIEDCLOUD_PROVIDER"] = "mock"
os.environ["EMBODIEDCLOUD_DATABASE_URL"] = db_url("embodiedcloud")
os.environ["EMBODIEDCLOUD_WORKSPACE_ROOT"] = "/tmp/test-embodiedcloud-workspaces"  # noqa: S108 测试隔离目录


def pytest_sessionfinish(session, exitstatus):
    Path(db_name("embodiedcloud")).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PostgreSQL 夹具：真服务器、真行锁。缺 docker/镜像/驱动时干净跳过，且 skip
# 文案带 GATE_SENTINEL —— scripts/validate_release.py 靠它把该 gate 登记为
# "PENDING(原因)"，而不是与普通 skip 混为一谈。
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _gpu_pool_not_starved():
    """整池被上游用例占满时，先回收再开测（背景与实测读数见 tests/gpu_pool.py）。

    只在"一张够用的卡都没有"时动手：那不可能是新一轮测试开头的合法状态——真需要
    "没有卡"这一前提的用例是自己把池抽干的（`test_scheduler` 的两条
    No GPU available），不是靠上一轮的残骸。有了这道闸，"谁的 workspace 多"
    不再决定谁红；不触发时对任何用例零影响。
    """
    from sqlalchemy import inspect

    from app.deps import SessionFactory, scheduler
    from tests.gpu_pool import count_big_enough, reclaim_gpus

    with SessionFactory() as db:
        # 建表在 lifespan 里：第一个用例跑之前表可能还不存在
        if inspect(db.bind).has_table("gpus") and count_big_enough(db, 8) == 0:
            released = reclaim_gpus(db, scheduler)
            print(
                f"[conftest] mock GPU 池被上游用例占满：回收 {released} 张，"
                f"现在够用 {count_big_enough(db, 8)} 张"
            )
    yield


@pytest.fixture(scope="session")
def pg_server_url():
    reason = pg_server.gate_reason()
    if reason is not None:
        pytest.skip(f"{pg_server.GATE_SENTINEL}: {reason}")
    server = pg_server.PgServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def pg_url(pg_server_url):
    """每条用例一个全新空库 + 完整 alembic 链（顺带在 PG 上验证迁移链可执行）。"""
    dbname = f"ec_t_{uuid.uuid4().hex[:12]}"
    pg_server.create_database(pg_server_url.server_url(), dbname)
    url = pg_server_url.database_url(dbname)
    try:
        pg_server.migrate(url)
        yield url
    finally:
        pg_server.drop_database(pg_server_url.server_url(), dbname)


@pytest.fixture
def pg_factory(pg_url):
    return pg_server.make_factory(pg_url)

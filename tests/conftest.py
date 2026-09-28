import os
import uuid
from pathlib import Path

import pg_server
import pytest

from tests.dbfiles import db_name, db_url, sweep_own_test_dbs

os.environ["EMBODIEDCLOUD_PROVIDER"] = "mock"
os.environ["EMBODIEDCLOUD_DATABASE_URL"] = db_url("embodiedcloud")
os.environ["EMBODIEDCLOUD_WORKSPACE_ROOT"] = "/tmp/test-embodiedcloud-workspaces"  # noqa: S108 测试隔离目录


def pytest_sessionfinish(session, exitstatus):
    Path(db_name("embodiedcloud")).unlink(missing_ok=True)
    # 每个模块级 ENGINE 都留下一份带本进程 pid 的库文件；会话结束时就一并收掉
    # （只收本 pid 的，别的并发跑不受影响）。见 tests/dbfiles.py 的模块 docstring。
    sweep_own_test_dbs()


# ---------------------------------------------------------------------------
# PostgreSQL 夹具：真服务器、真行锁。缺 docker/镜像/驱动时干净跳过，且 skip
# 文案带 GATE_SENTINEL —— scripts/validate_release.py 靠它把该 gate 登记为
# "PENDING(原因)"，而不是与普通 skip 混为一谈。
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _gpu_pool_not_starved():
    """整池被上游用例占满、或剩余空闲卡已经排在队列欠款上时，先回收再开测。

    判据是**净额**（原始空闲 − 未执行的 provision op），不是原始空闲：只在
    "一张够用的卡都没有"时动手，会放过"正好 1 张空闲 + 队列里压着 1 个 queued op"
    这个合法但致命的状态——下一个 tick 会先替那条 op 吃掉那唯一一张卡
    （背景与实测读数见 tests/gpu_pool.py 模块 docstring）。

    真需要"没有卡"这一前提的用例是自己把池抽干的（`test_scheduler` 的两条
    No GPU available），不是靠上一轮的残骸。有了这道闸，"谁的 workspace 多"
    不再决定谁红；不触发时它对任何用例零影响（只读两个计数，不改任何行）。
    """
    from sqlalchemy import inspect

    from app.deps import SessionFactory, scheduler
    from tests.gpu_pool import count_big_enough, reclaim_gpus, unfulfilled_provision_ops

    with SessionFactory() as db:
        # 建表在 lifespan 里：第一个用例跑之前表可能还不存在
        if inspect(db.bind).has_table("gpus"):
            free = count_big_enough(db, 8)
            debt = unfulfilled_provision_ops(db)
            if free - debt < 1:
                released = reclaim_gpus(db, scheduler)
                print(
                    f"[conftest] mock GPU 池净额不足（原始空闲 {free} 张、未执行 provision op "
                    f"{debt} 个）：回收 {released} 张，现在够用 {count_big_enough(db, 8)} 张"
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

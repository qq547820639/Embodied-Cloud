import os
import uuid
from pathlib import Path

import pg_server
import pytest

os.environ["EMBODIEDCLOUD_PROVIDER"] = "mock"
os.environ["EMBODIEDCLOUD_DATABASE_URL"] = "sqlite:///./test-embodiedcloud.db"
os.environ["EMBODIEDCLOUD_WORKSPACE_ROOT"] = "/tmp/test-embodiedcloud-workspaces"  # noqa: S108 测试隔离目录


def pytest_sessionfinish(session, exitstatus):
    Path("test-embodiedcloud.db").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PostgreSQL 夹具：真服务器、真行锁。缺 docker/镜像/驱动时干净跳过，且 skip
# 文案带 GATE_SENTINEL —— scripts/validate_release.py 靠它把该 gate 登记为
# "PENDING(原因)"，而不是与普通 skip 混为一谈。
# ---------------------------------------------------------------------------


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

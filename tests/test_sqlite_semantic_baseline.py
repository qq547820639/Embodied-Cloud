"""SQLite 侧语义基线：把"为什么必须有 PostgreSQL 档"钉成常驻断言。

这三条都跑在默认 `make test` 里（不需要 docker），作用是：一旦 SQLite 侧的
补偿性行为（丢 FOR UPDATE、不校验外键）悄悄变化或被修掉，这里立刻可见。
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import make_engine
from app.models import Base, Gpu, GpuHost, GpuStatus
from app.services.scheduler import GpuScheduler


def test_sqlite_dialect_drops_for_update_and_pg_keeps_it():
    """`FOR UPDATE SKIP LOCKED` 在 sqlite 方言里被整个丢弃，在 pg 方言里保留。

    这就是 allocate 的并发保护在开发库上"看不见地失效"的机制本身；
    真行锁语义只能由 tests/test_postgres_concurrency.py 在 PG 上验。
    """
    stmt = (
        select(Gpu)
        .where(Gpu.status == GpuStatus.AVAILABLE.value)
        .with_for_update(skip_locked=True)
        .limit(1)
    )

    sqlite_sql = str(stmt.compile(dialect=sqlite.dialect()))
    pg_sql = str(stmt.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE" not in sqlite_sql.upper(), f"sqlite 方言竟渲染了行锁：{sqlite_sql}"
    assert "FOR UPDATE" in pg_sql.upper() and "SKIP LOCKED" in pg_sql.upper(), (
        f"pg 方言丢了行锁子句：{pg_sql}"
    )


@pytest.fixture
def sqlite_engine(tmp_path):
    url = f"sqlite:///{tmp_path}/fk.db"
    engine = make_engine(Settings(provider="mock", database_url=url))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_sqlite_engine_turns_on_foreign_keys(sqlite_engine):
    """§30 裁决：应用自己的 sqlite 连接显式开启外键校验（默认是 off）。"""
    with sqlite_engine.connect() as conn:
        assert int(conn.execute(text("PRAGMA foreign_keys")).scalar()) == 1


def test_sqlite_rejects_gpu_without_host(sqlite_engine):
    """开启后，孤儿 GPU 必须被拒 —— 与 PostgreSQL 档同一判据。"""
    Factory = sessionmaker(bind=sqlite_engine, expire_on_commit=False)
    with Factory() as db:
        db.add(
            Gpu(
                id="gpu-orphan",
                gpu_uuid="GPU-orphan",
                host_id="no-such-host",
                model="m",
                memory_total=1000,
                status=GpuStatus.AVAILABLE.value,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_scheduler_seed_order_works_under_enforced_fk(sqlite_engine):
    """生产写序（先 host 后 gpu，同一事务）在 FK 开启下仍然通过。

    这条守的是"scheduler.sync_host 本身没有写序缺陷"：它靠 db.flush() 把
    parent 落到 child 之前。若哪天去掉那次 flush，这里会红。
    """
    from app.services.scheduler import GpuInfo

    Factory = sessionmaker(bind=sqlite_engine, expire_on_commit=False)
    scheduler = GpuScheduler(Factory)
    with Factory() as db:
        scheduler.sync_host(
            db,
            host_id="host-1",
            name="h1",
            address="127.0.0.1",
            provider="mock",
            gpus=[GpuInfo(gpu_uuid="GPU-a", model="m", memory_total=24576, index=0)],
        )
    with Factory() as db:
        assert db.get(GpuHost, "host-1") is not None
        assert len(list(db.scalars(select(Gpu)))) == 1

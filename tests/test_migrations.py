"""Alembic 迁移体系：全新库 up → down → up 循环可用 + schema 落地校验。"""

import os
import sqlite3
import subprocess
import sys

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.models import Base

# 关键表清单：迁移真正「落地」的证据（空迁移/纯 no-op 迁移同样 returncode=0，
# 但不会建出这些表；此断言防「迁移不建 schema」的伪通过）。
EXPECTED_TABLES = {
    "alembic_version",
    "users",
    "organizations",
    "user_sessions",
    "templates",
    "template_versions",
    "workspaces",
    "workspace_operations",
    "gpu_hosts",
    "gpus",
    "gpu_allocations",
    "credit_ledger",
    "billing_accounts",
    "credit_holds",
    "streaming_sessions",
    "courses",
    "course_members",
    "labs",
    "assignments",
    "submissions",
    "artifacts",
    "deployments",
    "edge_agents",
    "telemetry_events",
}


def _run_alembic(args: list[str], env: dict) -> int:
    result = subprocess.run(  # noqa: S603 仅执行固定可执行文件 + 测试传入的受控参数
        [sys.executable, "-m", "alembic", *args],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed: {result.stdout}\n{result.stderr}"
    return result.returncode


def _tables(db_path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def test_migration_upgrade_downgrade_cycle(tmp_path):
    db_path = tmp_path / "migrate.db"
    url = f"sqlite:///{db_path}"
    env = {"EMBODIEDCLOUD_DATABASE_URL": url}

    # up → head：关键表全部落地
    _run_alembic(["upgrade", "head"], env)
    assert db_path.exists()
    missing = EXPECTED_TABLES - _tables(db_path)
    assert not missing, f"upgrade head 后缺失表：{sorted(missing)}"

    # downgrade → base（空）：业务表全部移除
    _run_alembic(["downgrade", "base"], env)
    remaining = _tables(db_path)
    assert remaining <= {"alembic_version"}, f"downgrade base 后残留表：{sorted(remaining)}"

    # 再次 up → head（可重复执行），schema 仍然完整
    _run_alembic(["upgrade", "head"], env)
    assert not (EXPECTED_TABLES - _tables(db_path))

    # 重复 up 是幂等的
    _run_alembic(["upgrade", "head"], env)


def _metadata_diff(db_path) -> list:
    """迁移产出的库 vs app/models.py 声明：返回 alembic 认为「还差什么」。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return list(compare_metadata(MigrationContext.configure(conn), Base.metadata))
    finally:
        engine.dispose()


def test_migrated_schema_declares_nothing_the_models_do_not(tmp_path):
    """模型里声明的每一个对象（含外键）都必须真的被迁移建出来。

    `c7c6f510d21f` 给 edge_agents 加了 owner_user_id/organization_id 两列并带上
    ForeignKey，但迁移从未创建这两个约束 —— 于是 SQLite 与 PostgreSQL 的生产
    schema 里都没有它：租户归属只是一个"看起来有约束"的应用层约定。此前的
    迁移门只核表名，看不见这一类漂移。
    """
    db_path = tmp_path / "drift.db"
    _run_alembic(["upgrade", "head"], {"EMBODIEDCLOUD_DATABASE_URL": f"sqlite:///{db_path}"})

    diff = _metadata_diff(db_path)
    assert not diff, f"迁移 schema 与模型声明漂移：{[repr(d)[:160] for d in diff]}"

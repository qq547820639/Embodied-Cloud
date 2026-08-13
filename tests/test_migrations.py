"""Alembic 迁移体系：全新库 up → down → up 循环可用 + schema 落地校验。"""

import os
import sqlite3
import subprocess
import sys

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

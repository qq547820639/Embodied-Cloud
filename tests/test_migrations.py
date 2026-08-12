"""Alembic 迁移体系：全新库 up → down → up 循环可用。"""

import os
import subprocess
import sys


def _run_alembic(args: list[str], env: dict) -> int:
    result = subprocess.run(  # noqa: S603 仅执行固定可执行文件 + 测试传入的受控参数
        [sys.executable, "-m", "alembic", *args],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )
    return result.returncode


def test_migration_upgrade_downgrade_cycle(tmp_path):
    db_path = tmp_path / "migrate.db"
    url = f"sqlite:///{db_path}"
    env = {"EMBODIEDCLOUD_DATABASE_URL": url}

    # up → head
    assert _run_alembic(["upgrade", "head"], env) == 0, "upgrade head failed"
    assert db_path.exists()

    # downgrade → base（空）
    assert _run_alembic(["downgrade", "base"], env) == 0, "downgrade base failed"

    # 再次 up → head（可重复执行）
    assert _run_alembic(["upgrade", "head"], env) == 0, "second upgrade failed"

    # 重复 up 是幂等的
    assert _run_alembic(["upgrade", "head"], env) == 0, "idempotent upgrade failed"

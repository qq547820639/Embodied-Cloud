"""Alembic 迁移环境。

数据库 URL 优先级：
1. 环境变量 EMBODIEDCLOUD_DATABASE_URL（生产/部署）
2. alembic.ini 的 sqlalchemy.url
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.config import Settings
from app.db import Base
from app import models  # noqa: F401  确保所有模型注册到 Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    env_url = os.environ.get("EMBODIEDCLOUD_DATABASE_URL")
    if env_url:
        return env_url
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return Settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connect_args = (
        {"check_same_thread": False}
        if _database_url().startswith("sqlite")
        else {}
    )
    engine = create_engine(_database_url(), connect_args=connect_args, future=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

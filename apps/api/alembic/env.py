"""Alembic environment.

Runs against the same async engine the application uses, reading DATABASE_URL
through `agent.config` so there is exactly one source of truth. On Railway this
is invoked as `preDeployCommand`, never at app startup — `api` and `worker`
boot concurrently and would race (PRD §5.2 rule 6).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from agent.config import get_settings
from agent.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `%` is the configparser interpolation character; escape it so passwords survive.
config.set_main_option("sqlalchemy.url", get_settings().async_database_url.replace("%", "%%"))

target_metadata = Base.metadata


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

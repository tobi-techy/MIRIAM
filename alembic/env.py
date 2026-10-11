from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import pool

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import models so autogenerate sees the tables.
from miriam_agent.database.models import Base  # noqa: E402

target_metadata = Base.metadata

# Alembic is offline/online against DATABASE_URL; prefer the app settings
# so production credentials are not duplicated in alembic.ini.
def _db_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or ""
    if url and not url.endswith("driver?") and ":///" not in url:
        # Use app settings if ini still has the default driver:///.
        try:
            from miriam_agent.config.settings import get_settings
            raw = get_settings().DATABASE_URL
            # Alembic needs a sync driver, not asyncpg.
            return raw.replace("+asyncpg", "").replace("postgresql+asyncpg://", "postgresql://")
        except Exception:
            pass
    raw = url.replace("+asyncpg", "")
    return raw


def run_migrations_offline() -> None:
    url = _db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import create_engine  # psycopg2-binary required in dev deps

    connectable = create_engine(_db_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

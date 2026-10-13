"""Schema bootstrap for the request paths that used to call ``create_all``.

``Base.metadata.create_all`` appears on four request paths (memory, the money
journal, payment references, audit). It is how local dev and the test suite run
without a migration — and it is also, in the deployed database, how the schema
came to hold the model tables with no ``alembic_version`` and a missing
``payment_references``: a request path created what the models declared and
silently diverged from the migrations that are meant to define the schema.

``create_schema_if_allowed`` keeps dev and test unchanged and refuses the DDL in
production, naming the missing tables at ERROR level instead of creating them.
Production is the one environment where a missing table is a deploy fault to be
surfaced, not repaired in the dark; the fix is ``alembic upgrade head``.
"""

from __future__ import annotations

import logging
from typing import Any

import sqlalchemy as sa

from miriam_agent.config.settings import get_settings
from miriam_agent.database.models import Base

logger = logging.getLogger(__name__)


async def create_schema_if_allowed(conn: Any) -> None:
    """Run ``create_all`` in dev/test; in production, log and do not create.

    ``conn`` is an ``AsyncConnection`` (the ``async with engine.begin() as
    conn`` the call sites already hold).
    """
    if get_settings().auto_create_tables:
        await conn.run_sync(Base.metadata.create_all)
        return

    existing = set(await conn.run_sync(lambda sync: sa.inspect(sync).get_table_names()))
    missing = sorted(set(Base.metadata.tables) - existing)
    if missing:
        logger.error(
            "database.schema.missing_tables: create_all is disabled in "
            "production, so this request path did not create them. models.py "
            "declares %s but they are absent. Run `alembic upgrade head` to "
            "reconcile the schema.",
            missing,
        )
    else:
        logger.info(
            "database.schema.create_all_skipped: create_all is disabled in "
            "production; all model tables are present."
        )

"""003: align the 34 columns the models declare NOT NULL.

``alembic check`` reported 51 items of ``models.py`` <-> migration-001 drift:
10 JSON/JSONB types, 7 ``ix_*`` indexes and 34 nullability items. The types and
the indexes are reconciled in ``miriam_agent/database/models.py`` (JSON is
declared ``JSONB`` on Postgres via a type variant, and the 7 indexes are
declared with ``index=True``) — those are metadata-only and touch no rows. What
is left, and what this migration does, is the 34 columns the models declare
``NOT NULL`` while migration 001 created them nullable.

Every one of those columns is populated by a Python-side default (``utcnow_naive``,
``dict``, ``list``, ``0.0``, ``"medium"``, ``False``), so a row written through
the ORM can never hold NULL there. A row written by raw SQL, by a hand-built
schema, or before a default existed can. This migration therefore refuses to
invent data:

* it skips a column that is already ``NOT NULL`` (idempotent: re-running is a
  no-op, and a half-applied database converges);
* before any DDL it counts ``WHERE col IS NULL`` per column and, if any NULL
  exists, raises — naming the column and the row count — instead of backfilling
  or dropping rows. The pre-flight in
  ``docs/ALEMBIC-DRIFT-RECONCILIATION.md`` runs those same counts read-only so
  the owner sees the answer before the deploy window;
* it aborts if a column is missing entirely, which is the signature of a
  database that was never built by migrations 001/002 (the ``init-db.sql``
  path). See ``scripts/schema_provenance.sql``.

Locking: ``SET NOT NULL`` takes ACCESS EXCLUSIVE and scans the table. On a live
money table that is a maintenance-window operation, not an online one — the
lock note and the ordering (small tables first) are in the doc. No ``alembic
stamp`` is used anywhere: this migration earns the version, it does not assert one.

Revision ID: 003
Revises: 002
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None

# (table, column) — the 34 columns migration 001 left nullable but the models
# declare NOT NULL. Order is smallest-first so a lock is held briefly on each
# table before the bigger ones; correctness does not depend on the order.
_NOT_NULL_COLUMNS: list[tuple[str, str]] = [
    ("channel_identities", "verified"),
    ("channel_identities", "created_at"),
    ("channel_identities", "last_seen_at"),
    ("budgets", "current_spend"),
    ("budgets", "created_at"),
    ("investments", "purchase_date"),
    ("investments", "acquired_at"),
    ("payment_references", "created_at"),
    ("audit_logs", "details"),
    ("audit_logs", "created_at"),
    ("conversations", "created_at"),
    ("conversations", "updated_at"),
    ("messages", "metadata"),
    ("messages", "created_at"),
    ("users", "created_at"),
    ("users", "is_active"),
    ("users", "preferences"),
    ("memory_entries", "metadata"),
    ("memory_entries", "created_at"),
    ("memory_entries", "updated_at"),
    ("transactions", "transaction_date"),
    ("transactions", "created_at"),
    ("transactions", "metadata"),
    ("financial_profiles", "current_savings"),
    ("financial_profiles", "risk_tolerance"),
    ("financial_profiles", "investment_goals"),
    ("financial_profiles", "financial_goals"),
    ("financial_profiles", "created_at"),
    ("financial_profiles", "updated_at"),
    ("tool_usage", "parameters"),
    ("tool_usage", "result"),
    ("tool_usage", "created_at"),
    ("money_executions", "created_at"),
    ("money_executions", "updated_at"),
]


def _is_nullable(bind, table: str, column: str) -> bool:
    """True when the column exists and is currently nullable.

    Returns False (treated as "already tight", so the caller skips it) when the
    column is NOT NULL; raises when the column is absent, because a table
    missing a models.py column was not built by migrations 001/002 and this
    migration must not paper over that.
    """
    columns = {col["name"]: col for col in sa.inspect(bind).get_columns(table)}
    if column not in columns:
        raise RuntimeError(
            f"003: column {table}.{column} does not exist. This database was "
            "not built by migrations 001/002 (it looks like the retired "
            "scripts/init-db.sql path). Run scripts/schema_provenance.sql and "
            "reconcile the base schema before applying 003."
        )
    return bool(columns[column]["nullable"])


def _count_nulls(bind, table: str, column: str) -> int:
    """``count(*) WHERE column IS NULL`` through SQLAlchemy Core, not string SQL.

    The identifiers come from ``_NOT_NULL_COLUMNS``, never from user input, but
    building the statement from the model's own ``Table`` object means there is
    no hand-quoted SQL to get wrong and no injection surface (this is what
    semgrep's ``avoid-sqlalchemy-text`` rule is about).
    """
    from miriam_agent.database.models import Base

    meta_table = Base.metadata.tables[table]
    meta_column = meta_table.columns[column]
    return bind.execute(
        sa.select(sa.func.count()).select_from(meta_table).where(meta_column.is_(None))
    ).scalar_one()


def _assert_no_nulls(bind) -> None:
    offenders: list[str] = []
    for table, column in _NOT_NULL_COLUMNS:
        if not _is_nullable(bind, table, column):
            continue
        count = _count_nulls(bind, table, column)
        if count:
            offenders.append(f"{table}.{column} ({count} row(s))")
    if offenders:
        raise RuntimeError(
            "003: refusing to SET NOT NULL — these columns hold NULL rows, and "
            "this migration will not invent values to satisfy a constraint. "
            "Backfill each column first (see the pre-flight in "
            "docs/ALEMBIC-DRIFT-RECONCILIATION.md), then re-run:\n  "
            + "\n  ".join(offenders)
        )


def upgrade() -> None:
    bind = op.get_bind()
    _assert_no_nulls(bind)
    for table, column in _NOT_NULL_COLUMNS:
        if _is_nullable(bind, table, column):
            op.alter_column(table, column, nullable=False)


def downgrade() -> None:
    # Restores the nullable shape migration 001 produced, so a rollback lands
    # on exactly 002's schema. It never drops data.
    bind = op.get_bind()
    for table, column in reversed(_NOT_NULL_COLUMNS):
        if not _is_nullable(bind, table, column):
            op.alter_column(table, column, nullable=True)

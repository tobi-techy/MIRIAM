"""004: drop the unused ``memory_entries.embedding`` column.

The Python agent does not compute embeddings in the pilot, so an ``embedding``
column is a value nothing ever writes. In the deployed database it is exactly
that: the column exists and every one of the 299 memory rows holds NULL. The
decision is not the column *type* (JSONB vs ``vector``) — it is whether the
Python agent embeds at all, because that is what carries a per-entry API cost
forever, and the pilot's success measures (shops active five days a week,
sell-through within 15% of the distributor's records, fake alerts caught, no
double-counted payments) are all deterministic-data measures that semantic
recall does nothing for. The Go platform's database, on the same server, already
carries real embeddings for the knowledge corpus.

So the column goes, and the ``vector`` extension is not enabled in the Python
agent's database. The path back in — enable the extension, add the column as
``vector(dim)``, wire a provider, backfill — is written once in
``docs/SEMANTIC-MEMORY-PATH-BACK.md`` so a reversal is a one-line change here
plus a backfill, never a re-litigation.

Guarded: the column is dropped only if it exists, so this is a no-op on a fresh
database (001 no longer creates it) and correct on a create_all database (which
still has it). Dropping an always-NULL column loses no data; the downgrade
re-adds it as a nullable JSON column so the schema is reversible.

Revision ID: 004
Revises: 003
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None

_TABLE = "memory_entries"
_COLUMN = "embedding"


def _has_column() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(_TABLE)
    return any(column["name"] == _COLUMN for column in columns)


def upgrade() -> None:
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)


def downgrade() -> None:
    if not _has_column():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON(), nullable=True))

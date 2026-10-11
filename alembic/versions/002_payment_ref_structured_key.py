"""G23/C052: dedupe inbound payments on the structured key, not the payload hash.

The unique constraint on ``payment_references`` included ``payload_hash``, so
two wordings of one transfer (or the same transfer sent as a different
screenshot) produced two rows and counted twice. The dedupe key is now the four
structured columns (channel, sender_handle, amount_minor, reference); the raw
payload and its hash are retained for dispute replay but no longer keyed.

Revision ID: 002
Revises: 001
Create Date: 2026-10-11
"""

from __future__ import annotations

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None

_CONSTRAINT = "uq_payment_ref_dedupe"
_NEW_COLUMNS = ["channel", "sender_handle", "amount_minor", "reference"]
_OLD_COLUMNS = [*_NEW_COLUMNS, "payload_hash"]


def upgrade() -> None:
    with op.batch_alter_table("payment_references") as batch:
        batch.drop_constraint(_CONSTRAINT, type_="unique")
        batch.create_unique_constraint(_CONSTRAINT, _NEW_COLUMNS)


def downgrade() -> None:
    with op.batch_alter_table("payment_references") as batch:
        batch.drop_constraint(_CONSTRAINT, type_="unique")
        batch.create_unique_constraint(_CONSTRAINT, _OLD_COLUMNS)

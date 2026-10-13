"""Baseline: all tables from models.py — created only where absent.

Revision ID: 001
Revises:
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def _table(name: str, *columns) -> None:
    """``op.create_table`` that is a no-op when the table already exists.

    The deployed database was built by ``Base.metadata.create_all`` on a
    request path, so its model tables exist and it has no ``alembic_version``
    at all. A baseline that only knows how to build an empty database cannot
    adopt it: ``op.create_table`` would raise "table already exists" and
    ``alembic upgrade head`` would fail on the first table. Guarding each
    create makes this revision *adopt or create* — it brings a create_all
    database to head with no data loss and no duplicate-table error, and it
    still builds an empty database exactly as before.
    """
    if not sa.inspect(op.get_bind()).has_table(name):
        op.create_table(name, *columns)


def _index(name: str, table: str, columns: list[str]) -> None:
    """``op.create_index`` that is a no-op when the index already exists."""
    existing = {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns)


def upgrade() -> None:
    # No ``CREATE EXTENSION vector``: the Python agent stores no embeddings
    # (``memory_entries.embedding`` is dropped in 004) and is not meant to
    # enable pgvector. The Go platform's database, on the same server, carries
    # the knowledge-corpus embeddings. See docs/SEMANTIC-MEMORY-PATH-BACK.md.

    _table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("username", sa.String(), nullable=False, unique=True),
        sa.Column("email", sa.String(), nullable=False, unique=True),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("preferences", sa.JSON(), nullable=True),
    )
    _table(
        "channel_identities",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("handle", sa.String(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("channel", "handle", name="uq_channel_handle"),
    )
    _table(
        "conversations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    _table(
        "messages",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(),
            sa.ForeignKey("conversations.id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    _table(
        "memory_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    _table(
        "financial_profiles",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("monthly_income", sa.Float(), nullable=False),
        sa.Column("current_savings", sa.Float(), nullable=True),
        sa.Column("risk_tolerance", sa.String(), nullable=True),
        sa.Column("investment_goals", sa.JSON(), nullable=True),
        sa.Column("financial_goals", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    _table(
        "transactions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "financial_profile_id",
            sa.String(),
            sa.ForeignKey("financial_profiles.id"),
            nullable=False,
        ),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("transaction_date", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
    )
    _table(
        "investments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "financial_profile_id",
            sa.String(),
            sa.ForeignKey("financial_profiles.id"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("purchase_price", sa.Float(), nullable=False),
        sa.Column("current_price", sa.Float(), nullable=False),
        sa.Column("purchase_date", sa.DateTime(), nullable=True),
        sa.Column("current_value", sa.Float(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(), nullable=True),
    )
    _table(
        "budgets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "financial_profile_id",
            sa.String(),
            sa.ForeignKey("financial_profiles.id"),
            nullable=False,
        ),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("monthly_limit", sa.Float(), nullable=False),
        sa.Column("current_spend", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    _table(
        "audit_logs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    _table(
        "tool_usage",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("execution_time", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    _table(
        "payment_references",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("sender_handle", sa.String(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("reference", sa.String(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="reported"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "channel",
            "sender_handle",
            "amount_minor",
            "reference",
            "payload_hash",
            name="uq_payment_ref_dedupe",
        ),
    )
    _table(
        "money_executions",
        sa.Column("idempotency_key", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("amount", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default=""),
        sa.Column("counterparty", sa.String(), nullable=False, server_default=""),
        sa.Column("sleeve", sa.String(), nullable=False, server_default=""),
        sa.Column("decision_id", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="reserved"),
        sa.Column("rail_reference", sa.String(), nullable=True),
        sa.Column("detail", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    for tbl, col in [
        ("conversations", "user_id"),
        ("messages", "conversation_id"),
        ("memory_entries", "user_id"),
        ("memory_entries", "type"),
        ("audit_logs", "user_id"),
        ("audit_logs", "created_at"),
        ("tool_usage", "user_id"),
    ]:
        _index(f"ix_{tbl}_{col}", tbl, [col])
    # Vector extension is external; embedding stays JSON until pgvector is wired.


def downgrade() -> None:
    for tbl in [
        "money_executions",
        "payment_references",
        "tool_usage",
        "audit_logs",
        "budgets",
        "investments",
        "transactions",
        "financial_profiles",
        "memory_entries",
        "messages",
        "conversations",
        "channel_identities",
        "users",
    ]:
        if sa.inspect(op.get_bind()).has_table(tbl):
            op.drop_table(tbl)

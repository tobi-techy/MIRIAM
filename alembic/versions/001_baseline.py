"""Baseline: all tables from models.py as create_all.

Revision ID: 001
Revises: 
Create Date: 2026-10-01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("username", sa.String(), nullable=False, unique=True),
        sa.Column("email", sa.String(), nullable=False, unique=True),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("preferences", JSONB(), nullable=True),
    )
    op.create_table(
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
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("conversation_id", sa.String(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "memory_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("embedding", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "financial_profiles",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("monthly_income", sa.Float(), nullable=False),
        sa.Column("current_savings", sa.Float(), nullable=True),
        sa.Column("risk_tolerance", sa.String(), nullable=True),
        sa.Column("investment_goals", JSONB(), nullable=True),
        sa.Column("financial_goals", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("financial_profile_id", sa.String(), sa.ForeignKey("financial_profiles.id"), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("transaction_date", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
    )
    op.create_table(
        "investments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("financial_profile_id", sa.String(), sa.ForeignKey("financial_profiles.id"), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("purchase_price", sa.Float(), nullable=False),
        sa.Column("current_price", sa.Float(), nullable=False),
        sa.Column("purchase_date", sa.DateTime(), nullable=True),
        sa.Column("current_value", sa.Float(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "budgets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("financial_profile_id", sa.String(), sa.ForeignKey("financial_profiles.id"), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("monthly_limit", sa.Float(), nullable=False),
        sa.Column("current_spend", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("details", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "tool_usage",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("parameters", JSONB(), nullable=True),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("execution_time", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "payment_references",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("sender_handle", sa.String(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("reference", sa.String(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="reported"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("channel", "sender_handle", "amount_minor", "reference", "payload_hash", name="uq_payment_ref_dedupe"),
    )
    op.create_table(
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
        op.create_index(f"ix_{tbl}_{col}", tbl, [col])
    # Vector extension handled externally; embedding remains JSON until pgvector is wired.

def downgrade() -> None:
    for tbl in ["money_executions","payment_references","tool_usage","audit_logs","budgets","investments","transactions","financial_profiles","memory_entries","messages","conversations","channel_identities","users"]:
        op.drop_table(tbl)


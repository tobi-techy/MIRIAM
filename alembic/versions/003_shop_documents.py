"""Shop document reading: stock books, invoices, delivery notes and notes.

Four tables back POST /api/v1/shop/documents/read. ``shop_documents`` holds the
read itself (raw extraction JSON, the file sha256, the models used, Jev's
routing decision, the prompt version and the confidence) and is unique on
(user_id, sha256) so a resent photo returns the original reply instead of a
second read. The three child tables hold the checked rows and notes.

Revision ID: 003
Revises: 002
Create Date: 2026-10-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shop_documents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=False, server_default=""),
        sa.Column("doc_type", sa.String(), nullable=False),
        sa.Column("trust_label", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("mime", sa.String(), nullable=False, server_default=""),
        sa.Column("model_path", JSONB(), nullable=True),
        sa.Column("models_used", JSONB(), nullable=True),
        sa.Column("jev_decision", JSONB(), nullable=True),
        sa.Column("flags", JSONB(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reply_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("raw_extraction", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "sha256", name="uq_shop_doc_user_sha"),
    )
    op.create_index("ix_shop_documents_user", "shop_documents", ["user_id"])

    op.create_table(
        "shop_stock_book_rows",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "document_id",
            sa.String(),
            sa.ForeignKey("shop_documents.id"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("item", sa.String(), nullable=False, server_default=""),
        sa.Column("opening", sa.Integer(), nullable=True),
        sa.Column("supplied", sa.Integer(), nullable=True),
        sa.Column("sold", sa.Integer(), nullable=True),
        sa.Column("left", sa.Integer(), nullable=True),
        sa.Column("price_naira", sa.String(), nullable=True),
        sa.Column("crossed_out_old", sa.String(), nullable=True),
        sa.Column("crossed_out_new", sa.String(), nullable=True),
        sa.Column("unclear", JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_shop_stock_rows_document", "shop_stock_book_rows", ["document_id"]
    )

    op.create_table(
        "shop_invoice_lines",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "document_id",
            sa.String(),
            sa.ForeignKey("shop_documents.id"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("item", sa.String(), nullable=False, server_default=""),
        sa.Column("qty", sa.Integer(), nullable=True),
        sa.Column("unit_price_naira", sa.String(), nullable=True),
        sa.Column("line_total_naira", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_shop_invoice_lines_document", "shop_invoice_lines", ["document_id"]
    )

    op.create_table(
        "shop_notes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "document_id",
            sa.String(),
            sa.ForeignKey("shop_documents.id"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(), nullable=False, server_default="other"),
        sa.Column("party", sa.String(), nullable=False, server_default=""),
        sa.Column("amount_naira", sa.String(), nullable=True),
        sa.Column("due", sa.String(), nullable=True),
    )
    op.create_index("ix_shop_notes_document", "shop_notes", ["document_id"])


def downgrade() -> None:
    op.drop_table("shop_notes")
    op.drop_table("shop_invoice_lines")
    op.drop_table("shop_stock_book_rows")
    op.drop_table("shop_documents")

"""Flatten a StatementExtraction to the contract ``raw`` field (JSON-safe)."""

from __future__ import annotations

from typing import Any

from miriam_agent.documents.categorize import (
    categorize_narration,
    category_label,
    is_essential,
    spend_kind,
)
from miriam_agent.documents.models import StatementExtraction


def _transaction_dict(t) -> dict[str, Any]:
    bucket = categorize_narration(t.description or t.raw_description, t.direction)
    return {
        "date": t.date.normalized.isoformat() if t.date else None,
        "description": t.description,
        "amount": str(t.amount.normalized) if t.amount else None,
        "direction": t.direction,
        "currency": t.currency,
        "balance_after": (
            str(t.balance_after.normalized) if t.balance_after else None
        ),
        "reference": t.reference,
        "page": t.page,
        "category": bucket,
        "category_label": category_label(bucket),
        "spend_kind": spend_kind(bucket),
        "is_essential": is_essential(bucket),
    }


def statement_to_dict(ext: StatementExtraction) -> dict[str, Any]:
    return {
        "institution": ext.institution,
        "account_name": ext.account_name,
        "masked_account_number": ext.masked_account_number,
        "currency": ext.currency,
        "period_start": (
            ext.period_start.normalized.isoformat() if ext.period_start else None
        ),
        "period_end": ext.period_end.normalized.isoformat() if ext.period_end else None,
        "opening_balance": (
            str(ext.opening_balance.normalized) if ext.opening_balance else None
        ),
        "closing_balance": (
            str(ext.closing_balance.normalized) if ext.closing_balance else None
        ),
        "total_credits": (
            str(ext.total_credits) if ext.total_credits is not None else None
        ),
        "total_debits": str(ext.total_debits) if ext.total_debits is not None else None,
        "transaction_count": len(ext.transactions),
        "transactions": [_transaction_dict(t) for t in ext.transactions],
    }

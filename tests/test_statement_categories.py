"""Narration categories on statements Miriam reads herself."""

from datetime import date
from decimal import Decimal

from miriam_agent.documents.categorize import (
    categorize_narration,
    category_label,
    is_essential,
    spend_kind,
)
from miriam_agent.documents.models import (
    ParsedTransaction,
    RawDate,
    RawMoney,
    StatementExtraction,
)
from miriam_agent.documents.processors.statement_dict import statement_to_dict


def test_nigerian_narrations():
    assert categorize_narration("POS PURCHASE SHOPRITE LEKKI", "debit") == "groceries"
    assert categorize_narration("WEB BET9JA/0123", "debit") == "betting"
    assert categorize_narration("IKEDC PAYMENT 44921", "debit") == "utilities"
    assert categorize_narration("NIP GTB/OBADEJO/0123456789/TRANSFER", "debit") == "transfer_out"
    assert categorize_narration("SALARY PAYMENT", "credit") == "salary"
    assert spend_kind("transfer_out") == "movement"
    assert spend_kind("groceries") == "consumption"


def test_statement_dict_carries_category():
    ext = StatementExtraction(
        transactions=[
            ParsedTransaction(
                date=RawDate(raw="2025-03-05", normalized=date(2025, 3, 5)),
                description="AIRTIME PURCHASE MTN",
                amount=RawMoney(raw="500.00", normalized=Decimal("500.00")),
                direction="debit",
            )
        ]
    )
    txn = statement_to_dict(ext)["transactions"][0]
    assert txn["category"] == "airtime"
    assert txn["category_label"] == "Airtime and data"
    assert txn["is_essential"] is True
    assert txn["spend_kind"] == "consumption"


def test_direction_fallback_and_unknown_bucket():
    # No rule hit: a credit is money coming in, a debit is just "other".
    assert categorize_narration("RANDOM NARRATION XYZ", "credit") == "transfer_in"
    assert categorize_narration("RANDOM NARRATION XYZ", "debit") == "other"
    assert categorize_narration("", None) == "other"
    assert category_label("nope") == "Other"
    assert spend_kind("salary") == "income"
    assert spend_kind("savings") == "movement"
    assert spend_kind("loan") == "movement"
    assert is_essential("groceries") is True
    assert is_essential("food") is False
    assert is_essential("transfer_out") is False

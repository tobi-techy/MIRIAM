"""Golden tests for the Stage 3 bank-statement pipeline.

Each fixture asserts real financial facts (type, currency, balances,
transaction count/dates/amounts/directions, reconciliation status) — not
just "result is not None". Documents are real PDF bytes for the native
path; OCR is a deterministic double, so no live provider is used.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from decimal import Decimal

import pytest

from miriam_agent.documents.pipeline import PipelineConfig, process_document
from tests.fixtures.documents import statements as fx

GOLDEN = {
    "gtb": {
        "text": fx.GTB_DEBIT_CREDIT,
        "currency": "NGN",
        "opening": "250000.00",
        "closing": "1109650.00",
        "count": 9,
        "credits": "912000.00",
        "debits": "52350.00",
        "reconciled": True,
    },
    "access": {
        "text": fx.ACCESS_SINGLE_AMOUNT,
        "currency": "NGN",
        "opening": "1250000.00",
        "closing": "1490000.00",
        "count": 5,
        "credits": "300000.00",
        "debits": "60000.00",
        "reconciled": True,
    },
    "zenith": {
        "text": fx.ZENITH_WITHDRAWAL_DEPOSIT_USD,
        "currency": "USD",
        "opening": "5000.00",
        "closing": "6171.25",
        "count": 5,
        "credits": "2512.75",
        "debits": "1341.50",
        "reconciled": True,
    },
    "uba_multiline": {
        "text": fx.UBA_MULTILINE_NO_BALANCE,
        "currency": "NGN",
        "opening": None,
        "closing": None,
        "count": 3,
        "credits": "400000.00",
        "debits": "43500.00",
        "reconciled": None,  # reconciliation skipped: no balances
    },
    "fidelity_mismatch": {
        "text": fx.FIDELITY_MISMATCH,
        "currency": "NGN",
        "opening": "100000.00",
        "closing": "69000.00",
        "count": 4,
        "credits": "5000.00",
        "debits": "40500.00",
        "reconciled": False,
    },
}


async def _run(text: str, **cfg_kwargs):
    pdf = fx.make_pdf(text)
    return await process_document(
        pdf, document_id="doc-test", config=PipelineConfig(**cfg_kwargs)
    )


@pytest.mark.parametrize("name", sorted(GOLDEN))
async def test_bank_statement_golden(name):
    expect = GOLDEN[name]
    run = await _run(expect["text"])
    result = run.result
    assert result.document_type == "bank_statement", run.stages
    assert result.schema_version == "1.0"
    stmt = result.data.raw["statement"]
    assert stmt["currency"] == expect["currency"]
    assert stmt["opening_balance"] == expect["opening"]
    assert stmt["closing_balance"] == expect["closing"]
    assert stmt["transaction_count"] == expect["count"], stmt["transactions"]
    if expect["credits"] is not None:
        assert Decimal(stmt["total_credits"]) == Decimal(expect["credits"])
    if expect["debits"] is not None:
        assert Decimal(stmt["total_debits"]) == Decimal(expect["debits"])
    if expect["reconciled"] is None:
        assert result.validation.status == "unknown"
        assert result.validation.reconciled is False
    else:
        assert result.validation.reconciled is expect["reconciled"], (
            result.validation.checks,
            result.validation.errors,
        )
    assert run.method == "native_pdf"


async def test_gtb_transaction_details_are_exact():
    run = await _run(fx.GTB_DEBIT_CREDIT)
    txns = run.result.data.raw["statement"]["transactions"]
    first = txns[0]
    assert first["date"] == "2026-08-02"
    assert first["description"].startswith("POS PURCHASE CHICKEN REPUBLIC")
    assert first["amount"] == "8500.00"
    assert first["direction"] == "debit"
    assert first["balance_after"] == "241500.00"
    credits = [t for t in txns if t["direction"] == "credit"]
    assert [t["amount"] for t in credits] == ["450000.00", "12000.00", "450000.00"]
    assert credits[1]["description"].startswith("REFUND FROM JUMIA")


async def test_access_signed_amount_direction_is_parsed():
    """A single signed AMOUNT column: the sign decides the direction.

    Amounts are reported in the pipeline's canonical 2-decimal form, the same
    as every other fixture ("8500.00", "300000.00"). This test previously
    expected the unformatted "12500"/"300000", which no other assertion in the
    suite agrees with -- the value was correct and the expectation was not.
    """
    run = await _run(fx.ACCESS_SINGLE_AMOUNT)
    txns = run.result.data.raw["statement"]["transactions"]
    assert txns[0]["direction"] == "debit"
    assert txns[0]["amount"] == "12500.00"
    assert txns[1]["direction"] == "credit"
    assert txns[1]["amount"] == "300000.00"


async def test_multiline_description_is_joined():
    run = await _run(fx.UBA_MULTILINE_NO_BALANCE)
    txns = run.result.data.raw["statement"]["transactions"]
    assert "NIGERIA REF 993201" in txns[0]["description"]


async def test_mismatch_is_reported_not_corrected():
    """An inconsistent statement is flagged, never silently "fixed".

    The difference is arithmetic on the fixture's own numbers: opening
    100,000.00 + credits 5,000.00 - debits 40,500.00 = 64,500.00, against a
    stated closing of 69,000.00. The expectation here used to read 3,500.00,
    which matches no combination of the fixture's figures; the parser's
    4,500.00 is correct.
    """
    run = await _run(fx.FIDELITY_MISMATCH)
    val = run.result.validation
    assert val.status == "invalid"
    assert Decimal(val.difference) == Decimal("4500.00")
    assert any(c.name == "statement_balance" and not c.passed for c in val.checks)
    assert "opening + credits - debits does not equal closing balance" in val.errors
    txns = run.result.data.raw["statement"]["transactions"]
    assert len(txns) == 4  # rows untouched


async def test_invoice_is_not_forced_to_statement():
    run = await _run(fx.INVOICE_SAMPLE)
    assert run.result.document_type == "invoice"
    assert (
        run.result.data.raw["statement"] if "statement" in run.result.data.raw else True
    )
    assert run.result.data.raw.get("statement") is None

"""Deterministic document classification (Stage 3).

Rule-based keyword scoring over extracted text — the same vocabulary as
Go's ``RuleClassifier`` so both planes agree. LLM fallback is a Stage 4+
concern; when uncertain this returns ``unknown`` rather than forcing a type.
"""

from __future__ import annotations

from miriam_agent.documents.models import DocumentKind, ExtractedText

_STATEMENT = (
    "opening balance",
    "closing balance",
    "available balance",
    "account number",
    "statement period",
    "transaction date",
    "value date",
    "balance b/f",
    "balance c/f",
)
_RECEIPT = (
    "subtotal",
    "total",
    "tax",
    "vat",
    "cash",
    "change",
    "receipt",
    "qty",
    "cashier",
    "thank you",
    "amount due",
)
_INVOICE = (
    "invoice",
    "invoice number",
    "invoice no",
    "due date",
    "bill to",
    "purchase order",
    "po number",
    "payment terms",
)


def _score(lower: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for kw in keywords if kw in lower)


def classify(text: ExtractedText) -> tuple[DocumentKind, float]:
    """Return (document_type, confidence). Confidence is a coarse score."""
    lower = text.full_text.lower()
    stmt = _score(lower, _STATEMENT)
    rcpt = _score(lower, _RECEIPT)
    inv = _score(lower, _INVOICE)
    if stmt >= 2 and stmt >= rcpt and stmt >= inv:
        return "bank_statement", min(0.95, 0.6 + 0.1 * stmt)
    if inv >= 2 and inv > rcpt:
        return "invoice", min(0.9, 0.6 + 0.1 * inv)
    if rcpt >= 2:
        return "receipt", min(0.9, 0.6 + 0.1 * rcpt)
    if stmt >= 1 and rcpt == 0 and inv == 0:
        return "bank_statement", 0.55
    if inv >= 1 and rcpt == 0:
        return "invoice", 0.55
    if rcpt >= 1:
        return "receipt", 0.55
    return "unknown", 0.0

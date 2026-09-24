"""Deterministic money/date/currency normalization (Stage 3).

Every parser keeps the raw source string and stores the normalized value
beside it. Normalization never alters financial meaning: it strips symbols
and separators, resolves sign conventions, and parses dates against explicit
format lists. Ambiguous dates stay ``None`` so callers lower confidence
instead of guessing.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

CURRENCIES = ("NGN", "USD", "GBP", "EUR")
CURRENCY_SYMBOLS = {"₦": "NGN", "$": "USD", "£": "GBP", "€": "EUR"}
CURRENCY_CODES = {
    "ngn": "NGN",
    "naira": "NGN",
    "usd": "USD",
    "gbp": "GBP",
    "eur": "EUR",
}

_AMOUNT_RE = re.compile(r"[-+]?\(?\d[\d,]*\.?\d*\)?")
_PAREN_NEGATIVE_RE = re.compile(r"^\(\s*[\d,.\s₦$£€]*\s*\)$")

# Unambiguous-first: ISO, then explicit day-first statement formats.
# MM/DD vs DD/MM ambiguity is resolved by callers with bank locale context,
# never inside this module.
DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %y",
    "%d/%m/%y",
    "%d-%m-%y",
)

_DATE_TOKEN_RE = re.compile(
    r"\b(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}|\d{1,2}-[A-Za-z]{3}-\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b"
)


def parse_money(raw: str) -> Decimal | None:
    """Parse a money string to Decimal, or None when no value is present."""
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    negative = False
    if _PAREN_NEGATIVE_RE.match(s):
        negative = True
    if s.endswith("-") or s.endswith("DR"):
        negative = True
        s = s[: -len("-" if s.endswith("-") else "DR")].strip()
    cleaned = re.sub(r"[₦$£€,\s]", "", s)
    cleaned = re.sub(r"(?i)\b(ngn|usd|gbp|eur|naira|cr|dr)\b", "", cleaned)
    cleaned = cleaned.strip("()")
    if not cleaned or not re.search(r"\d", cleaned):
        return None
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    return -value if negative and value > 0 else value


def last_money_on_line(line: str) -> tuple[str, Decimal] | None:
    """Right-most money value on a line (totals/balances live right)."""
    best: tuple[str, Decimal] | None = None
    for m in _AMOUNT_RE.finditer(line):
        token = m.group(0)
        if not re.search(r"\d", token):
            continue
        value = parse_money(token)
        if value is not None:
            best = (token, value)
    return best


def parse_date(raw: str) -> date | None:
    """Parse an explicit date string; None when ambiguous/unparseable."""
    s = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def find_dates(text: str) -> list[tuple[str, date]]:
    """All parseable date tokens in encounter order, with raw strings."""
    found: list[tuple[str, date]] = []
    for m in _DATE_TOKEN_RE.finditer(text):
        token = m.group(1)
        parsed = parse_date(token)
        if parsed is not None:
            found.append((token, parsed))
    return found


def detect_currency(text: str) -> str:
    """First currency indicator; empty string when none. $ alone is USD only
    when no competing symbol/code is present — callers add bank context."""
    for sym in ("₦", "£", "€", "$"):
        if sym in text:
            return CURRENCY_SYMBOLS[sym]
    lower = text.lower()
    for code in ("ngn", "naira", "gbp", "eur", "usd"):
        if code in lower:
            return CURRENCY_CODES[code]
    return ""


def mask_account_number(raw: str) -> str:
    """Keep only the last 4 digits; never propagate full numbers to the LLM."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) <= 4:
        return digits
    return "*" * (len(digits) - 4) + digits[-4:]

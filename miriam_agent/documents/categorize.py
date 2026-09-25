"""Flat spend buckets for a parsed bank-statement line.

Mirrors the narration rules in the enrichment sidecar
(services/enrichment/src/spend_rules.py) and the Go statement normalizer.
Miriam's own PDF reader does not call that sidecar, so a statement she
reads in-process still needs a category.

Parity owner: enrichment sidecar. When adding a rule here, add the same
rule there (and vice versa); tests/test_statement_categories.py pins the
shared Nigerian narrations so a drift fails loudly instead of splitting
dashboards silently. ``transfer_out``/``savings``/``loan`` are movement,
not consumption; only ``salary`` is income.
"""

from __future__ import annotations

import re

_RULES: list[tuple[re.Pattern[str], str, bool]] = [
    (re.compile(r"\b(bet9ja|sportybet|betking|1xbet|betway|nairabet|msport|bangbet)\b", re.I), "betting", True),
    (re.compile(r"\b(salary|payroll|wages?)\b", re.I), "salary", True),
    (re.compile(r"\b(sms\s*alert|stamp\s*duty|account\s*maintenance|vat\s+on|commission|card\s+maintenance|transfer\s+charge)\b", re.I), "fees", True),
    (re.compile(r"\b(atm|cash\s+withdrawal|atm\s*wdl|atm\s+cash)\b", re.I), "atm", True),
    (re.compile(r"\b(airtime|data\s+bundle|data\s+recharge|mtn|glo|airtel|9\s*mobile)\b", re.I), "airtime", True),
    (re.compile(r"\b(ikedc|aedc|ekedc|ibedc|phed|kedco|eedc|jedc|phcn|nepa|lawma)\b", re.I), "utilities", True),
    (re.compile(r"\b(dstv|gotv|startimes|showmax|netflix|spotify|youtube\s*premium)\b", re.I), "subscription", True),
    (re.compile(r"\b(rent|landlord)\b", re.I), "rent", True),
    (re.compile(r"\b(shoprite|spar|justrite|ebeano|game\s+store|prince\s+ebean|supermarket|grocer\w*)\b", re.I), "groceries", True),
    (re.compile(r"\b(chicken\s+republic|kfc|mr\s+biggs|dominos?|pizza\s+hut|coldstone|bukka|chowdeck|glovo|restaurant|eatery)\b", re.I), "food", False),
    (re.compile(r"\b(uber|bolt|indrive|in-?drive|taxify|fuel|filling\s+station|totalenergies|nnpc|oando)\b", re.I), "transport", False),
    (re.compile(r"\b(pharmacy|healthplus|medplus|hospital|clinic|laboratory|\blab\b)\b", re.I), "health", True),
    (re.compile(r"\b(tuition|school\s+fee|university|waec|jamb|neco)\b", re.I), "education", True),
    (re.compile(r"\b(piggyvest|cowrywise|stash|savings\s+deposit)\b", re.I), "savings", True),
    (re.compile(r"\b(loan\s+repay|loan\s+deduction|loan\s+repayment)\b", re.I), "loan", True),
    (re.compile(r"\b(nip\s+credit|transfer\s+from|received\s+from|inward\s+transfer)\b", re.I), "transfer_in", True),
    (re.compile(r"\b(nip|trf\s+to|transfer\s+to|sent\s+to|funds?\s+transfer)\b", re.I), "transfer_out", True),
]

_MOVEMENT = {"transfer_in", "transfer_out", "salary", "savings", "loan"}
_ESSENTIAL = {"groceries", "utilities", "health", "education", "rent", "airtime"}

_LABELS = {
    "food": "Eating out",
    "groceries": "Groceries",
    "transport": "Transport",
    "utilities": "Utilities",
    "entertainment": "Entertainment",
    "shopping": "Shopping",
    "health": "Health",
    "education": "Education",
    "rent": "Rent",
    "transfer_in": "Transfers in",
    "transfer_out": "Transfers out",
    "salary": "Salary",
    "airtime": "Airtime and data",
    "betting": "Betting",
    "subscription": "Subscriptions",
    "savings": "Savings",
    "loan": "Loan payments",
    "fees": "Bank fees",
    "atm": "Cash withdrawals",
    "other": "Other",
}


def categorize_narration(description: str, direction: str | None = None) -> str:
    """Return a flat spend bucket for one narration."""
    text = (description or "").strip()
    for pattern, bucket, _overrides in _RULES:
        if pattern.search(text):
            return bucket
    if (direction or "").lower() == "credit":
        return "transfer_in"
    return "other"


def category_label(bucket: str) -> str:
    return _LABELS.get(bucket, "Other")


def spend_kind(bucket: str) -> str:
    """consumption, movement, or income. Movement is not spending."""
    if bucket == "salary":
        return "income"
    if bucket in _MOVEMENT:
        return "movement"
    return "consumption"


def is_essential(bucket: str) -> bool:
    return bucket in _ESSENTIAL

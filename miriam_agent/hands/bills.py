"""Airbills bill payments parsed from the user's words.

Airtime, data, electricity, cable, betting, and transport are Airbills
settlements. They are never NGN-to-crypto onramps, even when the sentence
says "buy" and "naira".
"""

from __future__ import annotations

import re
from decimal import Decimal

from miriam_agent.hands.state import ProposedAction

_PHONE_RE = re.compile(r"(?:\+234\d{10}|0\d{10})")
_METER_RE = re.compile(r"\b(\d{10,13})\b")

# Longer phrases first so "data plan" wins over a stray "data".
_CATEGORIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("airtime", "recharge"), "airtime"),
    (("data plan", "data bundle", "buy data"), "data"),
    (("electricity", "nepa", "phcn", "disco", "meter"), "electricity"),
    (("startimes", "dstv", "gotv", "cable"), "cable"),
    (("betting",), "betting"),
    (("transport",), "transport"),
)


def bill_category(text: str) -> str:
    """Airbills category named by the sentence, or empty when it is not a bill."""
    lowered = (text or "").casefold()
    for words, category in _CATEGORIES:
        if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words):
            return category
    return ""


def _recipient(text: str, category: str) -> str:
    phone = _PHONE_RE.search(text or "")
    if phone and category in ("airtime", "data"):
        return phone.group(0)
    if category in ("electricity", "cable", "betting", "transport"):
        for match in _METER_RE.finditer(text or ""):
            # Skip a bare amount that is not a meter or smartcard.
            if len(match.group(1)) >= 10:
                return match.group(1)
    return ""


def parse_bill_utterance(text: str) -> ProposedAction | None:
    """Turn 'buy 500 naira airtime for 0803…' into an Airbills bill action."""
    from miriam_agent.hands.transfer import parse_amount

    category = bill_category(text)
    if not category:
        return None
    amount = parse_amount((text or "").casefold())
    if amount is None or amount <= 0:
        return None
    recipient = _recipient(text or "", category)
    if not recipient:
        return None
    return ProposedAction(
        type="bill",
        amount=amount if isinstance(amount, Decimal) else Decimal(str(amount)),
        counterparty=recipient,
        sleeve="spendable",
        raw=text,
        source="user",
    )

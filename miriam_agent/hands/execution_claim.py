"""Layer 1 - HANDS. Deterministic execution narration and assertion.

The egress gate (``judgment/gates.py``) is model-based and fail-open: it
catches invented facts in prose, but it does not know the numbers that Hands
actually committed. This module is the hard, model-free complement: given the
:class:`Receipt` of an executed movement, it states the only facts a completion
message may contain, and it verifies a draft states them.

This is the "no hallucinated done!" rail. Voice may only narrate an execution
in the terms this module derives from the receipt -- never a number the model
invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from miriam_agent.hands.audit import Receipt


@dataclass(frozen=True)
class ExecutionAssertion:
    """Whether a draft states the executed movement correctly."""

    ok: bool
    expected: str
    found_amount: bool
    reason: str = ""

    @property
    def applicable(self) -> bool:
        return bool(self.expected)


def execution_facts(receipt: Receipt) -> str:
    """The canonical, receipt-derived narration of an executed movement.

    Empty when the receipt does not represent an executed money movement (no
    executed status, or no amount), so a "nothing happened" result cannot be
    narrated as a completion.
    """
    if receipt is None or receipt.status != "executed" or receipt.amount is None:
        return ""
    amount = f"{receipt.amount:.2f}"
    currency = (receipt.currency or "").strip()
    who = (receipt.counterparty or receipt.sleeve or "").strip()
    parts = ["executed", amount, currency] if currency else ["executed", amount]
    line = " ".join(parts)
    return f"{line} to {who}" if who else line


def _amount_variants(amount: Decimal) -> set[str]:
    """Every reasonable rendering of the amount a draft might contain."""
    variants = {
        f"{amount:.2f}",
        f"{amount:.1f}",
        str(amount.normalize()),
    }
    if amount == amount.to_integral_value():
        variants.add(str(int(amount)))
    return variants


def verify_execution_narration(draft: str, receipt: Receipt) -> ExecutionAssertion:
    """Check that a completion message states the executed amount.

    Returns ``ok=False`` when the receipt says money moved but the draft does
    not contain any rendering of that exact amount -- the signature of a
    hallucinated confirmation. Currency is checked loosely (a draft that says
    "$20" for a 20.00 USD receipt is fine); the amount itself must match.
    """
    expected = execution_facts(receipt)
    if not expected:
        return ExecutionAssertion(ok=True, expected="", found_amount=True)

    variants = _amount_variants(receipt.amount)
    found = any(v in (draft or "") for v in variants)
    reason = "" if found else "amount_missing"
    return ExecutionAssertion(
        ok=found, expected=expected, found_amount=found, reason=reason
    )


__all__ = [
    "ExecutionAssertion",
    "execution_facts",
    "verify_execution_narration",
]

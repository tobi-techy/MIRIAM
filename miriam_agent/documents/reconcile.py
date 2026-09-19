"""Deterministic reconciliation over Decimal (Stage 3).

Two checks, no LLM arithmetic:

1. statement_balance: opening + credits - debits == closing (explicit,
   configurable tolerance for bank rounding).
2. running_balance: previous balance_after +/- txn == current
   balance_after, for consecutive txns that both carry balances.

A mismatch never mutates data — it flags checks, records the Decimal
difference, and lowers confidence downstream.
"""

from __future__ import annotations

from decimal import Decimal

from miriam_agent.documents.models import ReconCheck, ReconResult, StatementExtraction


def reconcile(ext: StatementExtraction, *, tolerance: Decimal) -> ReconResult:
    checks: list[ReconCheck] = []
    errors: list[str] = []
    if ext.opening_balance is None or ext.closing_balance is None:
        return ReconResult(status="skipped", difference=Decimal("0.00"), checks=[], errors=["missing opening or closing balance"])
    credits = ext.total_credits if ext.total_credits is not None else Decimal("0")
    debits = ext.total_debits if ext.total_debits is not None else Decimal("0")
    if ext.total_credits is None and ext.total_debits is None:
        return ReconResult(status="skipped", difference=Decimal("0.00"), checks=[], errors=["no transactions with amounts"])
    expected = ext.opening_balance.normalized + credits - debits
    diff = (expected - ext.closing_balance.normalized).copy_abs()
    if diff <= tolerance:
        checks.append(ReconCheck(name="statement_balance", passed=True, message=f"opening + credits - debits = closing within {tolerance}"))
        status: str = "reconciled"
    else:
        checks.append(ReconCheck(name="statement_balance", passed=False, message=f"expected {expected}, closing {ext.closing_balance.normalized}"))
        errors.append("opening + credits - debits does not equal closing balance")
        status = "mismatch"
    running = _running_balance_checks(ext, tolerance)
    checks.extend(running)
    if any(not c.passed for c in running):
        errors.append("running balance sequence has breaks")
        status = "mismatch"
    return ReconResult(status=status, difference=diff, checks=checks, errors=errors)  # type: ignore[arg-type]


def _running_balance_checks(ext: StatementExtraction, tolerance: Decimal) -> list[ReconCheck]:
    out: list[ReconCheck] = []
    prev: Decimal | None = None
    for txn in ext.transactions:
        if txn.balance_after is None or txn.amount is None or txn.direction is None:
            prev = txn.balance_after.normalized if txn.balance_after else prev
            continue
        if prev is None:
            prev = txn.balance_after.normalized
            continue
        if txn.direction == "credit":
            expected = prev + txn.amount.normalized
        else:
            expected = prev - txn.amount.normalized
        drift = (expected - txn.balance_after.normalized).copy_abs()
        if drift <= tolerance:
            out.append(ReconCheck(name="running_balance", passed=True))
        else:
            out.append(ReconCheck(name="running_balance", passed=False, message=f"row {txn.line_index}: expected {expected}, got {txn.balance_after.normalized}"))
        prev = txn.balance_after.normalized
    return out

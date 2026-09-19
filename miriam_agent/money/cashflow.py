"""Stage 3 — the Conscious Spending Plan split (Ramit Sethi, adapted).

Splits take-home into five honest lines in the user's own currency:

    fixed  |  debt  |  savings  |  investments  |  guilt-free

The healthy *shape* is 50-60 / 10-20 / 5-10 / 20-30 fixed-invest-save-guilt-free,
but this module never forces it. It takes the numbers the safety stack actually
produced and reports them, because a percentage table imported from a US
professional's blog onto a naira informal earner is worse than no table at all
(``R-AUDIT-4``).

Two behaviours carried over from Sethi that matter more than the percentages:

  - **Guilt-free is non-zero outside a crisis** (``R-SETHI-2``). A plan with a
    0% fun line gets abandoned, and an abandoned plan protects nothing.
  - **When fixed costs exceed 60%, say so plainly** (``R-SETHI-3``). That is a
    cost-cut or an income-raise, not a distribution problem, and pretending a
    prettier pie chart fixes it is the kind of advice that wastes a year.

The residual is always assigned to guilt-free rather than dropped, so the split
reconciles to take-home exactly. Nothing is silently absorbed.
"""

from __future__ import annotations

from decimal import Decimal

from miriam_agent.money.formatting import format_amount, format_pct, join_sentences
from miriam_agent.money.safety import SafetyStack
from miriam_agent.money.schema import AllocationBook, CashflowSplit

# Above this share of income, fixed costs are the problem (``R-SETHI-3``).
_FIXED_CEILING_SHARE = Decimal("0.60")


def build_cashflow(stack: SafetyStack, book: AllocationBook) -> CashflowSplit:
    """Turn the safety stack's allocation into the cashflow table.

    The stack owns how much is available; the book owns how much of it is
    genuinely *investable* (it is the only engine that knows the horizon). Money
    the stack freed up but the horizon disqualified -- a goal inside three years
    -- is routed to savings rather than investments, so the table says where the
    money actually goes instead of showing it as a market position.
    """
    currency = stack.currency
    income = stack.monthly_income
    fixed = stack.monthly_fixed
    debt = stack.debt_extra
    investments = book.investable_surplus
    savings = stack.buffer_contribution + max(
        Decimal("0"), stack.investable_surplus - book.investable_surplus
    )
    note_parts: list[str] = []

    if book.short_horizon and stack.investable_surplus > 0:
        note_parts.append(
            "the goal is close enough that this money is held as savings rather "
            "than invested, so it is there on the date you need it"
        )

    # The residual is guilt-free by definition: every other line has already
    # taken its claim. Assigning it here (rather than recomputing it) is what
    # makes ``total() == income`` exact instead of merely close.
    guilt_free = income - fixed - debt - savings - investments

    if guilt_free < 0:
        # Only reachable if a caller hands us a split the stack did not produce.
        # Clamp and say so rather than emitting a negative line.
        note_parts.append(
            "the split did not reconcile to take-home; the shortfall has been "
            "reported rather than hidden"
        )
        guilt_free = Decimal("0")

    crisis = stack.crisis
    if crisis:
        note_parts.append(
            "this is a crisis month: the guilt-free floor is lifted and every "
            "spare unit goes at the problem"
        )
    elif guilt_free < income * Decimal("0.05"):
        note_parts.append(
            "guilt-free spending is thin this month; the plan protects a small "
            "amount on purpose so it survives contact with real life"
        )

    cost_ratio = (fixed / income) if income > 0 else Decimal("0")
    if cost_ratio > _FIXED_CEILING_SHARE:
        note_parts.append(
            f"fixed costs are {format_pct(cost_ratio * 100)} of take-home. That is "
            "above the 60% ceiling, and it is a cost-cut or an income-raise, not "
            "something a better split can fix"
        )

    split = CashflowSplit(
        fixed=fixed,
        debt=debt,
        savings=savings,
        investments=investments,
        guilt_free=guilt_free,
        currency=currency,
        crisis=crisis,
        note=join_sentences(note_parts),
    )
    split.shares = _shares(split, income)
    return split


def _shares(split: CashflowSplit, income: Decimal) -> dict[str, float]:
    """The same plan as proportions, for channels that render bars not amounts."""
    total = income if income > 0 else Decimal("1")
    return {
        "fixed": round(float(split.fixed / total), 3),
        "debt": round(float(split.debt / total), 3),
        "savings": round(float(split.savings / total), 3),
        "investments": round(float(split.investments / total), 3),
        "guilt_free": round(float(split.guilt_free / total), 3),
    }


def render_split(split: CashflowSplit, income: Decimal) -> list[str]:
    """The split as plan lines, one per line, in currency with its share."""
    rows = [
        ("Fixed costs (incl. debt minimums)", split.fixed),
        ("Debt attack (above minimums)", split.debt),
        ("Savings (buffer)", split.savings),
        ("Investments", split.investments),
        ("Guilt-free", split.guilt_free),
    ]
    total = income if income > 0 else Decimal("1")
    out: list[str] = []
    for label, amount in rows:
        share = format_pct((amount / total) * 100)
        out.append(f"{label}: {format_amount(amount, split.currency)} ({share})")
    out.append(
        f"Total: {format_amount(split.total(), split.currency)} of "
        f"{format_amount(income, split.currency)} take-home"
    )
    return out

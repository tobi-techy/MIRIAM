"""Deterministic allocation engine (spec §11): "your money needs a default."

Given what Miriam knows, this engine divides one period of income into four
envelopes in hard currency amounts:

  - ``everyday`` -- what life costs (essentials, plus a capped allowance when
    discretionary spending is on record)
  - ``safety``   -- the buffer build plus the debt attack
  - ``future``   -- what goes toward the long term (only when the base can
    carry it)
  - ``flexible`` -- the unassigned remainder, decided with the user

Never fixed percentages. The shares adapt to surplus, buffer gap, debt
pressure, income volatility and goal horizon, and every adaptation lands in
``rationale`` so Miriam can explain *why* in plain words. The LLM narrates;
it never computes (§2, §29).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from miriam_agent.financial.diagnosis import Diagnosis
from miriam_agent.financial.profile import FinancialProfile, horizon_months

_MONTHLY_FACTOR = {"daily": 30.4375, "weekly": 4.345, "monthly": 1.0, "yearly": 1 / 12}


class Allocation(BaseModel):
    """One period's money, given a default (spec §11)."""

    model_config = ConfigDict(extra="forbid")

    income: float = 0.0
    currency: str = "NGN"
    frequency: str = "monthly"
    everyday: float = 0.0
    safety: float = 0.0
    debt_attack: float = 0.0
    future: float = 0.0
    flexible: float = 0.0
    surplus: float = 0.0
    buffer_target: float | None = None
    buffer_gap: float | None = None
    months_to_fill_buffer: float | None = None
    shares: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: list[str] = Field(default_factory=list)
    next_step: str = ""

    def totals_check(self, tolerance: float = 1.0) -> bool:
        """The envelopes must add back to income (spec §29: testable).

        ``debt_attack`` is its own envelope -- a naira that attacks debt cannot
        also build the buffer -- so it counts in the reconciliation too.
        """
        return (
            abs(
                (
                    self.everyday
                    + self.safety
                    + self.debt_attack
                    + self.future
                    + self.flexible
                )
                - self.income
            )
            <= tolerance
        )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


# ---------------------------------------------------------------------------
# Resolution: one shared reading of the profile, like the diagnosis engine
# ---------------------------------------------------------------------------


def _monthly(profile: FinancialProfile, field: str) -> float | None:
    amount = profile.money(field)
    if amount is None:
        return None
    if field != "income_amount":
        return amount
    frequency = str(profile.value("income_frequency") or "monthly").casefold()
    return amount * _MONTHLY_FACTOR.get(frequency, 1.0)


def _round(value: float) -> float:
    """Whole units of currency. Miriam speaks ₦50,000, never ₦49,999.37."""
    return round(max(0.0, value))


def _finalize_shares(alloc: Allocation) -> None:
    """The same plan as proportions, for channels that render bars not naira."""
    total = alloc.income if alloc.income > 0 else 1.0
    alloc.shares = {
        "everyday": round(alloc.everyday / total, 3),
        "safety": round(alloc.safety / total, 3),
        "debt_attack": round(alloc.debt_attack / total, 3),
        "future": round(alloc.future / total, 3),
        "flexible": round(alloc.flexible / total, 3),
    }


def _next_step(
    alloc: Allocation, diagnosis: Diagnosis | None, crushing_debt: bool
) -> str:
    """The one action key the Rail-value moment should lead with."""
    if crushing_debt:
        return "clear_high_cost_debt"
    if alloc.future > 0:
        return "automate_the_investment" if alloc.buffer_gap == 0 else "build_safety_first"
    if alloc.safety > 0:
        return "build_emergency_buffer"
    if diagnosis is not None and diagnosis.top_action():
        return diagnosis.top_action()
    return "record_income"


def calculate_allocation(
    profile: FinancialProfile,
    diagnosis: Diagnosis | None = None,
) -> Allocation:
    """Split one period's income into Everyday / Safety / Future / Flexible.

    Order of claims on the money (the priority list, not a percentage table):

      0. Debt service first, when debt is crushing (>= 6x monthly income).
         There is no "future" while the hole is being dug deeper.
      1. Everyday is floored at essentials (+ a capped allowance when
         discretionary spending is actually on record).
      2. Safety builds the buffer -- one month of essentials while the buffer
         is under a month (urgent), then up to half the remainder toward the
         full target (3 months steady, 6 volatile).
      3. Debt takes an attack slice while it is meaningful, sized by the
         diagnosis, never starving the buffer.
      4. Future gets a real share only when the base carries it (buffer past
         one month, horizon not short); otherwise it stays honest and small.
      5. What is left is flexible: unassigned on purpose, decided together
         rather than silently absorbed.

    The envelopes always reconcile to income (see totals_check), and when the
    outflow already exceeds income the plan says so plainly instead of
    inventing money.
    """
    currency = profile.currency()
    frequency = str(profile.value("income_frequency") or "monthly").casefold()
    income = _monthly(profile, "income_amount") or 0.0
    essentials = _monthly(profile, "essential_expenses") or 0.0
    discretionary = _monthly(profile, "discretionary_expenses") or 0.0
    debt_total = profile.money("debt") or 0.0
    buffer = profile.money("emergency_fund")
    if buffer is None:
        buffer = profile.money("savings") or 0.0
    volatile = profile.volatile_income()
    horizon = horizon_months(profile)

    alloc = Allocation(income=_round(income), currency=currency, frequency=frequency)
    assumptions: list[str] = []
    rationale: list[str] = []

    if profile.fact("emergency_fund") is None and profile.fact("savings") is not None:
        assumptions.append(
            "savings is treated as the buffer (no separate fund recorded)"
        )

    target_months = 6.0 if volatile else 3.0
    buffer_target = essentials * target_months if essentials > 0 else None
    buffer_gap = max(0.0, (buffer_target or 0.0) - buffer) if buffer_target else None

    if income <= 0:
        alloc.assumptions = ["no income recorded yet; amounts follow on payday"]
        alloc.next_step = "record_income"
        return alloc

    outflow = essentials + min(discretionary, income * 0.5)
    if outflow > income:
        alloc.everyday = _round(income)
        alloc.rationale = [
            f"recorded outflow ({outflow:,.0f}) already exceeds "
            f"income ({income:,.0f})",
            "everyday takes the whole income this period; there is no safe "
            "safety or future slice until the gap closes",
        ]
        alloc.assumptions = assumptions
        alloc.confidence = 0.8
        alloc.next_step = "fix_cashflow_gap"
        _finalize_shares(alloc)
        return alloc

    debt_months = debt_total / income if income > 0 else 0.0
    crushing_debt = debt_months >= 6.0

    everyday = essentials
    everyday_note = "essentials are protected first"
    if discretionary > 0 and not crushing_debt:
        allowance = min(discretionary, income * 0.15, max(0.0, income - essentials))
        if allowance > 0:
            everyday += allowance
            everyday_note = (
                f"essentials plus a capped allowance of {allowance:,.0f} "
                "(their own spending, acknowledged rather than banned)"
            )
    rationale.append(everyday_note)

    return _claim_remaining(
        alloc,
        profile,
        diagnosis,
        income=income,
        everyday=everyday,
        debt_total=debt_total,
        debt_months=debt_months,
        crushing_debt=crushing_debt,
        buffer=buffer,
        buffer_target=buffer_target,
        buffer_gap=buffer_gap,
        essentials=essentials,
        horizon=horizon,
        assumptions=assumptions,
        rationale=rationale,
    )


def _claim_remaining(
    alloc: Allocation,
    profile: FinancialProfile,
    diagnosis: Diagnosis | None,
    *,
    income: float,
    everyday: float,
    debt_total: float,
    debt_months: float,
    crushing_debt: bool,
    buffer: float,
    buffer_target: float | None,
    buffer_gap: float | None,
    essentials: float,
    horizon: int | None,
    assumptions: list[str],
    rationale: list[str],
) -> Allocation:
    """Safety, debt attack, future and flexible -- in that claim order, with one
    exception: crushing debt claims before safety (claim 0 of the plan)."""
    remaining = income - everyday
    safety = 0.0
    debt_attack = 0.0
    future = 0.0

    if crushing_debt:
        debt_attack = max(0.0, remaining * 0.6)
        safety = remaining - debt_attack
        rationale.append(
            f"debt is {debt_months:.0f}x income, so {debt_attack:,.0f} attacks it "
            f"and {safety:,.0f} still protects the month"
        )
        remaining = 0.0
    else:
        if buffer_gap is not None and buffer_gap > 0:
            if buffer < essentials:
                safety = min(remaining, max(essentials, buffer_gap))
                rationale.append(
                    f"safety takes {safety:,.0f} -- the buffer is under one month, "
                    "so it builds first"
                )
            else:
                safety = min(remaining * 0.5, buffer_gap)
                rationale.append(
                    f"safety takes {safety:,.0f} toward the {buffer_target:,.0f} buffer"
                )
            if debt_total > 0:
                # An urgent buffer build never gets to starve the debt attack
                # entirely: a hole being dug and a hole already dug are both
                # emergencies, so both stay funded.
                safety = min(safety, remaining * 0.7)
        remaining -= safety

        if debt_total > 0:
            pressured = diagnosis is not None and (
                "high_debt_burden" in diagnosis.problems()
            )
            debt_attack = min(remaining * (0.35 if pressured else 0.2), debt_total)
            if debt_attack > 0:
                rationale.append(
                    f"debt takes a {debt_attack:,.0f} attack slice while the "
                    "buffer keeps building"
                )
        remaining -= debt_attack

        if buffer >= essentials and horizon != 6 and remaining > 0:
            future = min(remaining * (0.6 if horizon in (24, 60) else 0.4), remaining)
            if future > 0:
                what = "the long-term goal" if horizon in (24, 60) else "the future"
                rationale.append(f"future takes {future:,.0f} toward {what}")
        elif remaining > 0 and (
            diagnosis is not None
            and {"investment_gap", "investment_ready"} & set(diagnosis.problems())
        ):
            future = min(remaining * 0.3, remaining)
            if future > 0:
                rationale.append(
                    f"future takes {future:,.0f} -- a starter slice while the base "
                    "finishes building"
                )
        remaining -= future

    flexible = max(0.0, remaining)
    if flexible > 0:
        rationale.append(
            f"flexible keeps {flexible:,.0f} -- unassigned on purpose, "
            "to decide together rather than leak away"
        )

    alloc.everyday = _round(everyday)
    alloc.safety = _round(safety)
    alloc.debt_attack = _round(debt_attack)
    alloc.future = _round(future)
    alloc.flexible = _round(flexible)
    # Rounding is cosmetic; the envelopes must still reconcile exactly.
    drift = (
        alloc.income
        - alloc.everyday
        - alloc.safety
        - alloc.debt_attack
        - alloc.future
        - alloc.flexible
    )
    alloc.flexible = _round(alloc.flexible + drift)

    outflow = essentials + min(
        profile.money("discretionary_expenses") or 0.0, income * 0.5
    )
    alloc.surplus = _round(income - outflow)
    alloc.buffer_target = _round(buffer_target) if buffer_target else None
    alloc.buffer_gap = _round(buffer_gap) if buffer_gap is not None else None
    if buffer_gap and safety > 0:
        alloc.months_to_fill_buffer = round(buffer_gap / safety, 1)
    alloc.assumptions = assumptions
    alloc.rationale = [r for r in rationale if r]
    alloc.next_step = _next_step(alloc, diagnosis, crushing_debt)
    alloc.confidence = _confidence(profile, diagnosis)
    _finalize_shares(alloc)
    return alloc


def _confidence(profile: FinancialProfile, diagnosis: Diagnosis | None) -> float:
    return round(
        0.6
        + 0.1 * (1 if profile.fact("income_amount") else 0)
        + 0.1 * (1 if profile.fact("essential_expenses") else 0)
        + 0.1 * (1 if profile.fact("emergency_fund") or profile.fact("savings") else 0)
        + 0.05 * (1 if diagnosis else 0),
        2,
    )
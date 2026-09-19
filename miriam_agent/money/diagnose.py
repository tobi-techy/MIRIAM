"""Stage 1 — diagnose the REAL problem, and say it bluntly.

One primary problem, chosen by first-match-wins over the ordered vocabulary in
``schema.PROBLEM_TYPES`` (the order is the brief's own enumeration, and it is
also the order of the safety stack: a bleed outranks everything, and a missing
buffer outranks expensive debt because the buffer is what stops the next shock
becoming new debt).

Mirrors ``financial/diagnosis.py``: ranked, evidence-backed, auditable. The
difference is scope -- that engine reads the conversational profile against 14
spec problems, this one answers the money pipeline's 9 with the local reference
data in hand, which is what makes ``inflation_erosion`` and ``under_earning``
answerable at all.

The ``blunt`` sentence is the first thing the user reads. It names amounts and
does not soften. If the plan is ugly, the diagnosis says it is ugly.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from miriam_agent.config.settings import get_settings
from miriam_agent.money.formatting import format_amount, format_pct
from miriam_agent.money.intake import IntakeProfile
from miriam_agent.money.reference import (
    CountryReference,
    ReferenceStatus,
    resolve_thresholds,
)
from miriam_agent.money.schema import Confidence, ProblemType

# Holding this much idle cash in a currency eroding at >= this rate is the
# inflation problem, not a diversification preference.
_EROSION_INFLATION_PCT = Decimal("10")

# Costs below this share of income are already lean, so a thin surplus is an
# income problem rather than a spending one (``R-SETHI-3``).
_LEAN_COST_RATIO = Decimal("0.60")

# A surplus under this share of income is too thin to invest (``readiness``).
_THIN_SURPLUS_RATIO = Decimal("0.15")

# Below this, a goal cannot absorb market risk (``R-BACH-2``).
_SHORT_HORIZON_MONTHS = 36

_AGGRESSION_CUES: tuple[str, ...] = (
    "crypto",
    "bitcoin",
    "memecoin",
    "meme coin",
    "altcoin",
    "leverage",
    "margin",
    "options",
    "day trad",
    "daytrad",
    "all in",
    "everything in",
    "100x",
    "moonshot",
    "yolo",
    "get rich",
    "quick",
)


class Diagnosis(BaseModel):
    """The ranked read: one primary problem, the evidence, the confidence."""

    model_config = ConfigDict(extra="forbid")

    problem_type: ProblemType
    blunt: str
    evidence: list[str] = Field(default_factory=list)
    confidence: Confidence = "low"
    scores: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)


class _Signals(BaseModel):
    """Everything the rules read, resolved once so each rule sees the same
    numbers and an absent value is a real ``None`` rather than a silent zero."""

    model_config = ConfigDict(extra="forbid")

    currency: str = ""
    income: Decimal | None = None
    fixed: Decimal | None = None
    variable: Decimal = Decimal("0")
    spend: Decimal | None = None
    surplus: Decimal | None = None
    surplus_ratio: Decimal | None = None
    cost_ratio: Decimal | None = None
    buffer: Decimal = Decimal("0")
    buffer_months: Decimal | None = None
    debt_total: Decimal = Decimal("0")
    fire_debts: list = Field(default_factory=list)
    max_apr: Decimal | None = None
    investments: Decimal = Decimal("0")
    horizon: int | None = None
    inflation_pct: Decimal = Decimal("0")
    erosion_pct: Decimal = Decimal("0")
    aggression: list[str] = Field(default_factory=list)
    volatile: bool = False
    missing: list[str] = Field(default_factory=list)


def _resolve(
    intake: IntakeProfile,
    reference: CountryReference,
    fire_apr_pct: Decimal,
) -> _Signals:
    sig = _Signals(
        currency=(intake.currency or reference.currency).upper(),
        income=intake.monthly_income(),
        fixed=intake.monthly_fixed(),
        variable=intake.monthly_variable() or Decimal("0"),
        buffer=intake.buffer_source(),
        investments=intake.existing_investments or Decimal("0"),
        horizon=intake.horizon_months(),
        inflation_pct=reference.inflation_pct,
        erosion_pct=reference.annual_erosion_pct(),
        volatile=intake.volatile_income(),
        missing=intake.missing_required(),
    )

    if sig.fixed is not None:
        sig.spend = sig.fixed + sig.variable
    if sig.income is not None and sig.spend is not None:
        sig.surplus = sig.income - sig.spend
        if sig.income > 0:
            sig.surplus_ratio = sig.surplus / sig.income
    if sig.income is not None and sig.fixed is not None and sig.income > 0:
        sig.cost_ratio = sig.fixed / sig.income
    if sig.fixed is not None and sig.fixed > 0:
        sig.buffer_months = sig.buffer / sig.fixed

    sig.debt_total = intake.total_debt()
    sig.fire_debts = [
        d for d in intake.debts if d.apr_pct is not None and d.apr_pct >= fire_apr_pct
    ]
    aprs = [d.apr_pct for d in intake.debts if d.apr_pct is not None]
    sig.max_apr = max(aprs) if aprs else None
    sig.aggression = _aggression_cues(intake)
    return sig


def _aggression_cues(intake: IntakeProfile) -> list[str]:
    """Cues that the user wants risk beyond their capacity.

    Read from the goal labels and the income type -- the only free text this
    object carries. Deterministic substring matching, so nothing is inferred
    from tone.
    """
    corpus = " ".join(
        [
            *(g.label for g in intake.goals),
            intake.income_type or "",
        ]
    ).casefold()
    return [cue for cue in _AGGRESSION_CUES if cue in corpus]


def _money(amount: Decimal | None, currency: str) -> str:
    return format_amount(amount, currency)


def diagnose(
    intake: IntakeProfile,
    *,
    reference: CountryReference,
    status: ReferenceStatus | None = None,
) -> Diagnosis:
    """Classify the primary money problem (``MONEY-RULES.md`` §4).

    First match wins, in the order of :data:`schema.PROBLEM_TYPES`. Returns the
    blunt sentence, the evidence behind it, and a confidence that falls whenever
    a load-bearing number is missing.
    """
    settings = get_settings()
    fire_apr, _judgment_apr = resolve_thresholds(
        reference,
        fire_default=Decimal(str(settings.MONEY_DEBT_FIRE_APR_PCT)),
        judgment_default=Decimal(str(settings.MONEY_DEBT_JUDGMENT_APR_PCT)),
    )
    sig = _resolve(intake, reference, fire_apr)
    cur = sig.currency

    assumptions = list(intake.assumptions)
    if status is not None:
        assumptions.append(status.note)

    scores: dict[str, float] = {name: 0.0 for name in _ORDER}

    # -- 1. data_gap -----------------------------------------------------
    if sig.missing:
        labels = {
            "income_amount": "your take-home pay",
            "fixed_costs": "your fixed monthly costs",
        }
        wanted = ", ".join(labels.get(f, f) for f in sig.missing)
        blunt = (
            f"I can't diagnose this yet: I don't have {wanted}. "
            "Give me those two numbers and I can tell you something true."
        )
        assumptions.append("no diagnosis attempted: the minimum inputs are not present")
        scores["data_gap"] = 1.0
        return Diagnosis(
            problem_type="data_gap",
            blunt=blunt,
            evidence=[f"missing: {', '.join(sig.missing)}"],
            confidence="low",
            scores=scores,
            assumptions=assumptions,
        )

    income = sig.income or Decimal("0")
    spend = sig.spend or Decimal("0")
    buffer_months = sig.buffer_months or Decimal("0")
    cost_ratio = sig.cost_ratio or Decimal("0")
    surplus = sig.surplus or Decimal("0")

    # The fire is worth naming inside any diagnosis that coexists with it: a
    # 35% APR loan is the fastest-moving number in the plan.
    fire_note = ""
    if sig.fire_debts:
        worth = sig.fire_debts[0]
        fire_note = (
            f" Separately, {_money(worth.balance, cur)} of debt at "
            f"{format_pct(worth.apr_pct, 1)} APR is a fire."
        )

    # -- 2. cashflow_bleed -----------------------------------------------
    if spend > income:
        gap = spend - income
        blunt = (
            f"You are spending {_money(spend, cur)} against {_money(income, cur)} "
            f"coming in. That is a {_money(gap, cur)} monthly bleed. Investing is off "
            f"table until that closes.{fire_note}"
        )
        scores["cashflow_bleed"] = 1.0
        return _result(
            "cashflow_bleed",
            blunt,
            [
                f"monthly inflow {_money(income, cur)}",
                f"monthly outflow {_money(spend, cur)}",
                f"monthly gap {_money(gap, cur)}",
            ],
            sig,
            assumptions,
            scores,
        )

    # -- 3. no_buffer ----------------------------------------------------
    if buffer_months < 1:
        blunt = (
            f"One bad month puts you under. You have "
            f"{_money(sig.buffer, cur)} set aside against "
            f"{_money(sig.fixed, cur)} of monthly essentials. That is "
            f"{buffer_months:.2f} of a month."
            f"{fire_note}"
        )
        scores["no_buffer"] = 1.0
        return _result(
            "no_buffer",
            blunt,
            [
                f"buffer {_money(sig.buffer, cur)}",
                f"monthly essentials {_money(sig.fixed, cur)}",
                "a single shock becomes new debt",
            ],
            sig,
            assumptions,
            scores,
        )

    # -- 4. high_interest_debt -------------------------------------------
    if sig.fire_debts:
        worst = max(sig.fire_debts, key=lambda d: d.apr_pct or Decimal("0"))
        blunt = (
            f"You are paying {format_pct(worst.apr_pct, 1)} APR on "
            f"{_money(sig.debt_total, cur)} of debt. No investment clears that "
            f"rate. Clearing this is the highest-return move available to you."
        )
        scores["high_interest_debt"] = 1.0
        return _result(
            "high_interest_debt",
            blunt,
            [
                f"total debt {_money(sig.debt_total, cur)}",
                f"highest rate {format_pct(worst.apr_pct, 1)} APR",
                f"fire threshold is {format_pct(fire_apr)}",
            ],
            sig,
            assumptions,
            scores,
        )

    # -- 5. inflation_erosion --------------------------------------------
    if (
        sig.investments <= 0
        and buffer_months >= 3
        and sig.erosion_pct >= _EROSION_INFLATION_PCT
    ):
        blunt = (
            f"Your money is safe from shocks and losing to inflation. "
            f"{_money(sig.buffer, cur)} sitting in {cur} cash is eroding at "
            f"roughly {format_pct(sig.erosion_pct)} a year, and nothing is "
            f"invested."
        )
        scores["inflation_erosion"] = 1.0
        return _result(
            "inflation_erosion",
            blunt,
            [
                f"{cur} inflation ~{format_pct(sig.inflation_pct)}",
                f"buffer {buffer_months:.1f} months held as cash",
                "no invested assets on record",
            ],
            sig,
            assumptions,
            scores,
        )

    # -- 6. goal_mismatch ------------------------------------------------
    if (
        sig.horizon is not None
        and sig.horizon < _SHORT_HORIZON_MONTHS
        and sig.investments > 0
    ):
        blunt = (
            f"You are holding {_money(sig.investments, cur)} in investments for a "
            f"goal that lands in about {sig.horizon} months. A market drawdown "
            f"would arrive exactly when you need the cash."
        )
        scores["goal_mismatch"] = 1.0
        return _result(
            "goal_mismatch",
            blunt,
            [
                f"goal horizon {sig.horizon} months",
                f"invested {_money(sig.investments, cur)}",
                "near-term money cannot absorb a drawdown",
            ],
            sig,
            assumptions,
            scores,
        )

    # -- 7. under_earning ------------------------------------------------
    if (
        cost_ratio <= _LEAN_COST_RATIO
        and sig.surplus_ratio is not None
        and sig.surplus_ratio < _THIN_SURPLUS_RATIO
        and not sig.fire_debts
    ):
        blunt = (
            f"There is no leak to plug. Fixed costs are already "
            f"{format_pct(cost_ratio * 100)} of income, and only "
            f"{format_pct(sig.surplus_ratio * 100)} is left over. The constraint "
            f"is what you earn, not what you spend."
        )
        scores["under_earning"] = 1.0
        return _result(
            "under_earning",
            blunt,
            [
                f"fixed costs {format_pct(cost_ratio * 100)} of income",
                f"surplus {format_pct(sig.surplus_ratio * 100)} of income",
                "costs are already lean",
            ],
            sig,
            assumptions,
            scores,
        )

    # -- 8. overconfidence_risk ------------------------------------------
    if sig.aggression:
        if sig.volatile or (intake.dependents or 0) > 0:
            why = "income moves around" if sig.volatile else "you have dependents"
            blunt = (
                f"You want to go hard, but {why}. That is what sets how much "
                f"risk you can actually carry, not how you feel about it. "
                f"Growth is capped and the plan says why."
            )
        else:
            blunt = (
                "You are reaching for a big, fast return. That is the position "
                "where people lose the money they cannot replace."
            )
        scores["overconfidence_risk"] = 1.0
        return _result(
            "overconfidence_risk",
            blunt,
            [f"stated appetite: {', '.join(sig.aggression)}"]
            + (["income is variable"] if sig.volatile else [])
            + ([f"{intake.dependents} dependents"] if intake.dependents else []),
            sig,
            assumptions,
            scores,
            extra_assumptions=[
                "capacity is scored separately from stated tolerance; appetite "
                "does not raise the growth cap"
            ],
        )

    # -- 9. idle_surplus -------------------------------------------------
    if sig.investments <= 0 and surplus > 0:
        long_horizon = sig.horizon is None or sig.horizon >= 84
        blunt = (
            f"You have {_money(surplus, cur)} a month doing nothing and a buffer "
            f"that holds. Nothing is invested"
            + (
                ", and nothing about this needs market risk."
                if not long_horizon
                else ", and you have time on your side."
            )
        )
        scores["idle_surplus"] = 1.0
        return _result(
            "idle_surplus",
            blunt,
            [
                f"buffer {buffer_months:.1f} months",
                f"monthly surplus {_money(surplus, cur)}",
                "no invested assets on record",
            ],
            sig,
            assumptions,
            scores,
        )

    # -- fallback: nothing acute. ---------------------------------------
    blunt = (
        "Nothing here is on fire: the buffer holds, there is no fire-rate debt, "
        "and money is already invested. The work is keeping it running and "
        "making sure the book still matches your horizon."
    )
    scores["idle_surplus"] = 0.4
    return _result(
        "idle_surplus",
        blunt,
        [
            f"buffer {buffer_months:.1f} months",
            f"invested {_money(sig.investments, cur)}",
            f"debt {_money(sig.debt_total, cur)}",
        ],
        sig,
        assumptions,
        scores,
    )


# The order rules are tried in. Kept next to the return sites so a reader can
# see the priority in one place (``MONEY-RULES.md`` §4).
_ORDER: tuple[str, ...] = (
    "data_gap",
    "cashflow_bleed",
    "no_buffer",
    "high_interest_debt",
    "inflation_erosion",
    "goal_mismatch",
    "under_earning",
    "overconfidence_risk",
    "idle_surplus",
)


def _result(
    problem: ProblemType,
    blunt: str,
    evidence: list[str],
    sig: _Signals,
    assumptions: list[str],
    scores: dict[str, float],
    *,
    extra_assumptions: list[str] | None = None,
) -> Diagnosis:
    if extra_assumptions:
        assumptions = [*assumptions, *extra_assumptions]
    return Diagnosis(
        problem_type=problem,
        blunt=blunt,
        evidence=evidence,
        confidence=_confidence(problem, sig),
        scores=scores,
        assumptions=assumptions,
    )


def _confidence(problem: str, sig: _Signals) -> Confidence:
    """How much we actually know, not how sure the sentence sounds.

    A diagnosis is only as good as the numbers under it. Missing APRs, an
    unknown country, or a missing buffer figure each knock it down; a diagnosis
    built on the minimum required fields alone never reaches ``high``.
    """
    if problem == "data_gap":
        return "low"
    points = 0
    if sig.income is not None:
        points += 1
    if sig.fixed is not None:
        points += 1
    if sig.surplus_ratio is not None:
        points += 1
    if sig.surplus is not None:
        points += 1
    if sig.buffer_months is not None:
        points += 1
    if not sig.missing:
        points += 1
    if sig.aggression or not sig.volatile:
        points += 1

    missing_aprs = bool(sig.debt_total > 0 and sig.max_apr is None)
    if missing_aprs:
        points -= 2

    if points >= 6:
        return "high"
    if points >= 4:
        return "medium"
    return "low"

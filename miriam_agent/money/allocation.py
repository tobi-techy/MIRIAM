"""Stage 4 — the 70/30 book on investable surplus, and the overrides.

David Bach's 70/30 is a durable default, not a law. This engine applies it and
then bends it for horizon and capacity, in the precedence order of
``MONEY-RULES.md`` ``R-BACH-2`` (first matching row wins):

    gated by safety            -> no book at all
    horizon < 3 years          -> cash-like only, not a market sleeve
    3-7 years                  -> 50/50 then 60/40
    7+ years, steady income    -> 70/30 default
    15+ years, high capacity   -> 80/20, only with recorded drawdown acceptance
    low capacity               -> growth capped, last word

**Capacity is scored, tolerance is not scored.** They are different questions:
capacity is what the user can afford to lose (time, job stability, dependents);
tolerance is how they feel about losing it. Only capacity moves the book. A user
who describes themselves as aggressive while carrying a variable income and
dependents gets capped anyway, and the plan says why -- that is the whole point
of separating the two fields in ``IntakeProfile``.

Every branch writes a ``reason`` and every deviation writes an ``overrides``
entry, so an unexplained book is impossible (``R-BACH-3``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from miriam_agent.money.formatting import format_pct, join_sentences
from miriam_agent.money.intake import IntakeProfile
from miriam_agent.money.reference import CountryReference
from miriam_agent.money.safety import SafetyStack
from miriam_agent.money.schema import AllocationBook

# Horizon boundaries, in months.
_SHORT_HORIZON = 36
_MID_HORIZON = 84
_LONG_HORIZON = 180

# Capacity ceilings on the growth sleeve. A low-capacity user is capped even when
# every horizon rule above would allow more.
_LOW_CAPACITY_CAP = Decimal("50")
_MEDIUM_CAPACITY_CAP = Decimal("70")


class Capacity(BaseModel):
    """How much risk the user can actually carry, scored from facts only."""

    model_config = ConfigDict(extra="forbid")

    score: int = 0
    band: Literal["low", "medium", "high"] = "low"
    factors: list[str] = Field(default_factory=list)


def score_capacity(intake: IntakeProfile) -> Capacity:
    """Score risk *capacity* from time, job stability, dependents and income.

    Deliberately ignores ``risk_tolerance``. Feelings are an input to the
    conversation, not to the arithmetic.

    A long horizon earns capacity, which is fair -- it is time to recover. But it
    must not be able to mask a fragile *month*: volatile pay combined with
    dependents or an unstable job is the exact combination that turns a market
    drawdown into a forced sale, so it floors the band at ``low`` regardless of
    how much time the user has.
    """
    capacity = Capacity()
    factors: list[str] = []
    score = 0

    horizon = intake.horizon_months()
    if horizon is None:
        factors.append("no goal horizon recorded")
    elif horizon >= _LONG_HORIZON:
        score += 2
        factors.append(f"long horizon ({horizon} months)")
    elif horizon >= _MID_HORIZON:
        score += 1
        factors.append(f"medium horizon ({horizon} months)")
    else:
        factors.append(f"short horizon ({horizon} months)")

    stability = (intake.job_stability or "").strip().casefold()
    if stability == "stable":
        score += 1
        factors.append("steady job")
    elif stability == "unstable":
        factors.append("unstable job")
    else:
        factors.append("job stability unknown")

    if intake.dependents:
        factors.append(f"{intake.dependents} dependents")
    else:
        score += 1
        factors.append("no dependents recorded")

    volatile = intake.volatile_income()
    if volatile:
        factors.append("variable income")
    else:
        score += 1
        factors.append("steady income")

    fragile = volatile and (bool(intake.dependents) or stability == "unstable")
    if fragile:
        factors.append(
            "fragile month: variable income with dependents or an unstable job"
        )

    capacity.score = score
    if fragile or score <= 1:
        capacity.band = "low"
    elif score >= 4:
        capacity.band = "high"
    else:
        capacity.band = "medium"
    capacity.factors = factors
    return capacity


def build_book(
    intake: IntakeProfile,
    stack: SafetyStack,
    reference: CountryReference,
) -> AllocationBook:
    """Choose the book for this user, with the reason and any overrides.

    ``investable_surplus`` on the returned book is the authoritative figure the
    plan uses: this is the only place that knows the horizon, so it is the only
    place that can decide whether the stack-approved surplus is genuinely
    investable or belongs in savings instead.
    """
    currency = stack.currency
    capacity = score_capacity(intake)
    horizon = intake.horizon_months()
    overrides: list[str] = []

    # -- gated by the safety stack --------------------------------------
    if not stack.investing_allowed:
        return AllocationBook(
            gated=True,
            growth_pct=Decimal("0"),
            defensive_pct=Decimal("100"),
            investable_surplus=Decimal("0"),
            rule_id="R-BACH-2:gated",
            overrides=["investing is not available yet"],
            reason=join_sentences(
                [
                    "There is no book yet",
                    *stack.blocked_reasons,
                    "Money held here stays in cash-like instruments until that "
                    "changes",
                ]
            ),
            growth_sleeve=[],
            defensive_sleeve=_defensive_sleeve(reference, currency, onchain=False),
        )

    # -- horizon rules ---------------------------------------------------
    if horizon is None:
        growth = Decimal("40")
        rule = "R-BACH-2:no-horizon"
        reason = (
            "No goal horizon is recorded, so this is held conservatively at 40/60. "
            "Tell me when the money is needed and the book can change."
        )
        overrides.append("horizon unknown -> conservative 40/60 until it is known")
    elif horizon < _SHORT_HORIZON:
        # Money needed inside three years does not belong in a market sleeve. It
        # is not "gated" -- the user is allowed to invest -- it is simply the
        # wrong instrument for the date.
        return AllocationBook(
            gated=False,
            short_horizon=True,
            growth_pct=Decimal("0"),
            defensive_pct=Decimal("100"),
            investable_surplus=Decimal("0"),
            rule_id="R-BACH-2:short-horizon",
            overrides=[
                f"horizon {horizon} months is inside the 3-year floor for markets"
            ],
            reason=(
                f"The goal lands in about {horizon} months. That is too near for a "
                "70/30 book, so this money is held in cash-like instruments "
                "instead. It keeps its date, which matters more than its return."
            ),
            growth_sleeve=[],
            defensive_sleeve=_defensive_sleeve(reference, currency, onchain=False),
        )
    elif horizon < _MID_HORIZON:
        growth = Decimal("50") if horizon < 60 else Decimal("60")
        rule = "R-BACH-2:mid-horizon"
        reason = (
            f"With roughly {horizon // 12} years to the goal, the book leans "
            "defensive. There is time to grow, but not enough to recover from a "
            "drawdown arriving at the wrong moment."
        )
    elif horizon < _LONG_HORIZON:
        if intake.volatile_income():
            growth = Decimal("60")
            rule = "R-BACH-2:mid-horizon-volatile"
            reason = (
                "The horizon supports 70/30, but the income does not: a variable "
                "pay means the buffer carries more, so the book carries less."
            )
            overrides.append("volatile income -> growth held at 60% not 70%")
        else:
            growth = Decimal("70")
            rule = "R-BACH-2:7y-default"
            reason = (
                "Seven years or more and steady income: this is the 70/30 default "
                "book. Growth does the work; 30% defensive is what stops a "
                "drawdown becoming a sale."
            )
    else:
        if capacity.band == "high" and intake.accepts_drawdown:
            growth = Decimal("80")
            rule = "R-BACH-2:15y-high-capacity"
            reason = (
                "A long horizon and high capacity allow 80/20, and you have "
                "explicitly accepted a 40% drawdown without selling."
            )
            overrides.append(
                "80/20 book used, on the basis of a recorded drawdown acceptance"
            )
        else:
            growth = Decimal("70")
            rule = "R-BACH-2:15y-default"
            reason = (
                "A long horizon keeps this at the 70/30 default. The extra growth "
                "of an 80/20 book is not worth taking without a recorded "
                "acceptance of a 40% drawdown."
            )
            if capacity.band == "high" and not intake.accepts_drawdown:
                overrides.append(
                    "high capacity, but 80/20 needs an explicit drawdown "
                    "acceptance that is not on record"
                )

    # -- the low-capacity cap gets the last word -------------------------
    cap = (
        _LOW_CAPACITY_CAP
        if capacity.band == "low"
        else _MEDIUM_CAPACITY_CAP if capacity.band == "medium" else Decimal("80")
    )
    if growth > cap:
        overrides.append(
            f"capacity is {capacity.band} ({'; '.join(capacity.factors)}), which "
            f"caps growth at {format_pct(cap)} regardless of stated appetite"
        )
        growth = cap

    defensive = Decimal("100") - growth
    onchain = _onchain_path(intake, stack)

    return AllocationBook(
        gated=False,
        growth_pct=growth,
        defensive_pct=defensive,
        investable_surplus=stack.investable_surplus,
        rule_id=rule,
        overrides=overrides,
        reason=reason,
        growth_sleeve=_growth_sleeve(growth, currency, onchain=onchain),
        defensive_sleeve=_defensive_sleeve(reference, currency, onchain=onchain),
    )


def _onchain_path(intake: IntakeProfile, stack: SafetyStack) -> bool:
    """Whether the onchain execution path is available *and* appropriate.

    Needs the user to be able to self-custody it. Kept separate from the book:
    the book is the same 70/30 idea either way, only the instrument changes.
    """
    return bool(intake.can_self_custody) and stack.investing_allowed


def _growth_sleeve(growth: Decimal, currency: str, *, onchain: bool) -> list[str]:
    """The growth sleeve in plain instrument terms, never a single name."""
    if growth <= 0:
        return []
    sleeve = [
        f"Broad, low-cost equity index fund ({currency} or the closest available "
        "equivalent) -- the default growth instrument"
    ]
    if onchain:
        sleeve.append(
            "Or a diversified onchain book mapped to the same growth share, "
            "reached through a multi-asset strategy -- never a single token"
        )
    return sleeve


def _defensive_sleeve(
    reference: CountryReference, currency: str, *, onchain: bool
) -> list[str]:
    """The defensive sleeve: things that hold their value in a bad month."""
    sleeve: list[str] = []
    for vehicle in reference.safety_vehicles:
        sleeve.append(f"{vehicle.name} ({vehicle.kind})")
    if onchain:
        sleeve.append(
            "High-quality stablecoins -- only because you already operate onchain "
            "and understand depeg risk. A depeg is a real loss, not a technicality"
        )
    if not sleeve:
        sleeve.append("Cash in an insured local deposit account")
    sleeve.append(
        "A volatile token is never part of this sleeve, whatever its recent "
        "performance"
    )
    return sleeve

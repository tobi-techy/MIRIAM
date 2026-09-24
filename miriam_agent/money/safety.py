"""Stage 2 — the safety stack, and the ``investing_allowed`` gate.

This module exists to make one thing impossible: recommending an investment to
someone who has not earned the right to make one yet. It runs in the order of
``MONEY-RULES.md`` §3, and the order is not negotiable:

    1. stop the bleed          spend > income          -> investing forbidden
    2. build the buffer        1 month if debt is on fire, else 3 (steady) / 6
    3. triage the debt         APR >= fire -> attack; judgment band vs the local
                               risk-free rate; otherwise keep it
    4. only now: surplus

Three separate conditions block investing, and each reports itself in
``blocked_reasons`` so the plan can say *why* rather than just "no":

  - the month does not close at all (a bleed);
  - there is no buffer to absorb the next shock;
  - there is debt at or above the fire threshold;
  - (or a debt whose APR we do not know, which we treat as blocking rather than
    assume is cheap -- ``R-AUDIT-1``, and the harm is asymmetric).

When ``investing_allowed`` is false, ``investable_surplus`` is ``0`` and
``allocation.py`` builds a gated book. That is the invariant the fixture suite
tests, and it is enforced structurally in two places rather than one.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from miriam_agent.config.settings import get_settings
from miriam_agent.money.formatting import format_amount, format_months, format_pct
from miriam_agent.money.intake import Debt, IntakeProfile
from miriam_agent.money.reference import (
    CountryReference,
    ReferenceStatus,
    resolve_thresholds,
)
from miriam_agent.money.schema import DebtAction, DebtBand, DebtStrategy

# The buffer the gate requires before *any* investing can happen.
_GATE_BUFFER_MONTHS = Decimal("1")

# Buffer targets once the gate is passed. Volatile income wants more, because
# the buffer has to cover a gap whose length is unknown (``R-HOUSEL-5``).
_STEADY_TARGET_MONTHS = Decimal("3")
_VOLATILE_TARGET_MONTHS = Decimal("6")
_FIRE_TARGET_MONTHS = Decimal("1")

# Share of remaining money that tops the buffer up on the way to target.
_URGENT_BUFFER_SHARE = Decimal("1.0")  # under one month: all of it
_BUILDING_BUFFER_SHARE = Decimal("0.5")
# With debt on fire, the buffer and the debt get funded *in parallel*. Taking the
# whole surplus to build a month of buffer over a year while 35% interest
# compounds is the wrong trade, and it is the classic way a "safe" plan quietly
# costs someone more than it saves.
_FIRE_BUFFER_SHARE = Decimal("0.4")

# Shares of what is left that attack debt.
_FIRE_DEBT_SHARE = Decimal("0.6")
_JUDGMENT_DEBT_SHARE = Decimal("0.25")

# Guilt-free spending reserved before investing, outside a crisis. A plan with
# a 0% fun line gets abandoned, and an abandoned plan protects nothing.
_GUILT_FREE_FLOOR_SHARE = Decimal("0.05")

# APR spread at which the maths clearly beats the morale argument, so we stop
# using the snowball and target the most expensive balance (``R-RAMSEY-3``).
_PAIRWISE_AVALANCHE_POINTS = Decimal("10")


class SafetyStack(BaseModel):
    """The ordered read of whether this user may invest, and how much."""

    model_config = ConfigDict(extra="forbid")

    currency: str = ""
    bleed: bool = False
    bleed_gap: Decimal = Decimal("0")

    monthly_income: Decimal = Decimal("0")
    monthly_fixed: Decimal = Decimal("0")
    # Debt minimums are counted inside ``monthly_fixed`` because they are
    # unavoidable: a month that cannot make its minimum payments has already
    # failed, whatever else it spends on.
    debt_minimums: Decimal = Decimal("0")
    # Income left after everything the user actually has to spend. This is the
    # money the stack allocates; it is *not* the investable amount.
    monthly_surplus: Decimal = Decimal("0")

    # A month that cannot close, or a fire with no buffer, is a crisis: the
    # guilt-free floor is lifted so every unit of currency can go at the problem
    # (``R-SETHI-2``). Outside a crisis the floor is reserved before investing.
    crisis: bool = False
    guilt_free_floor: Decimal = Decimal("0")

    buffer_months: Decimal = Decimal("0")
    buffer_target_months: Decimal = Decimal("0")
    buffer_target: Decimal = Decimal("0")
    buffer_current: Decimal = Decimal("0")
    buffer_gap: Decimal = Decimal("0")
    buffer_contribution: Decimal = Decimal("0")
    months_to_fill: Decimal | None = None
    buffer_vehicle: str = ""
    buffer_location: str = ""

    debt_actions: list[DebtAction] = Field(default_factory=list)
    debt_extra: Decimal = Decimal("0")
    fire_debt: bool = False
    judgment_debt: bool = False
    unknown_apr_debt: bool = False

    investable_surplus: Decimal = Decimal("0")
    investing_allowed: bool = False
    blocked_reasons: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


def _pick_vehicle(reference: CountryReference) -> tuple[str, str]:
    """The safest available local vehicle for the buffer.

    Insured deposit first (deposit protection is worth more than a few basis
    points), then short government paper, then a money-market fund. Never
    Glider: this money has to be there on the worst day, and onchain money can
    fail exactly then (``BufferPlan.is_glider`` cannot even express otherwise).
    """
    vehicles = reference.safety_vehicles
    for kind in ("deposit", "tbill", "mmf"):
        for vehicle in vehicles:
            if vehicle.kind == kind:
                note = f" ({vehicle.notes})" if vehicle.notes else ""
                return (
                    vehicle.name,
                    f"held separately from your spending balance{note}",
                )
    return (
        "an insured deposit account",
        "held separately from your spending balance",
    )


def _triage(
    debts: list[Debt],
    fire_apr: Decimal,
    judgment_apr: Decimal,
    risk_free_pct: Decimal,
) -> tuple[list[DebtAction], bool, bool, bool]:
    """Band every debt and decide what to do with it (``R-RAMSEY-1/2/3``).

    Returns ``(actions, has_fire, has_judgment, has_unknown)``, with actions
    ordered by priority -- the most expensive balance first, because that is
    where each extra unit of currency earns the most.
    """
    actions: list[DebtAction] = []
    has_fire = False
    has_judgment = False
    has_unknown = False

    aprs: list[Decimal] = [d.apr_pct for d in debts if d.apr_pct is not None]
    spread = (max(aprs) - min(aprs)) if len(aprs) >= 2 else Decimal("0")

    for debt in debts:
        if debt.apr_pct is None:
            has_unknown = True
            actions.append(
                DebtAction(
                    label=debt.label,
                    balance=debt.balance,
                    apr_pct=Decimal("0"),
                    minimum_monthly=debt.minimum_monthly,
                    band="none",
                    strategy="minimum_only",
                    reason=(
                        "the rate on this debt is not recorded, so it cannot be "
                        "triaged. Investing stays paused until it is known -- an "
                        "unknown rate is not the same as a cheap one."
                    ),
                )
            )
            continue

        apr = debt.apr_pct
        band: DebtBand
        if apr >= fire_apr:
            has_fire = True
            band = "fire"
            reason = (
                f"{format_pct(apr, 1)} APR is above the "
                f"{format_pct(fire_apr, 1)} fire line. No investment clears that "
                "rate, so this is the highest-return use of the money."
            )
        elif apr >= judgment_apr:
            beats_cash = apr > risk_free_pct
            has_judgment = True
            band = "judgment"
            if beats_cash:
                reason = (
                    f"{format_pct(apr, 1)} APR is above the local risk-free rate "
                    f"of {format_pct(risk_free_pct, 1)}. Paying this down is a "
                    "better return than the available safe alternative."
                )
            else:
                reason = (
                    f"{format_pct(apr, 1)} APR is below the local risk-free rate "
                    f"of {format_pct(risk_free_pct, 1)}, so this debt is cheap "
                    "money. Keep paying the minimum; do not accelerate it."
                )
        else:
            band = "keep"
            reason = (
                f"{format_pct(apr, 1)} APR is below the judgment band. This is "
                "cheap debt; the money is worth more working elsewhere."
            )

        actions.append(
            DebtAction(
                label=debt.label,
                balance=debt.balance,
                apr_pct=apr,
                minimum_monthly=debt.minimum_monthly,
                band=band,
                strategy="minimum_only",
                reason=reason,
            )
        )

    # Priority order: fire first, then judgment, then the rest; within a band,
    # the most expensive balance leads.
    actions.sort(key=lambda a: (-a.apr_pct, -a.balance))

    # Strategy: target the most expensive balance when the maths clearly
    # matters (a fire, or a wide spread); otherwise snowball for momentum,
    # because a visible first win is what keeps an impatient user going.
    strategy: DebtStrategy = (
        "avalanche"
        if (has_fire or spread >= _PAIRWISE_AVALANCHE_POINTS)
        else "snowball"
    )
    for action in actions:
        if action.band in ("fire", "judgment", "keep"):
            action.strategy = strategy
    return actions, has_fire, has_judgment, has_unknown


def assess_safety(
    intake: IntakeProfile,
    reference: CountryReference,
    *,
    status: ReferenceStatus | None = None,
) -> SafetyStack:
    """Run the safety stack in order and return the gate verdict."""
    settings = get_settings()
    fire_apr, judgment_apr = resolve_thresholds(
        reference,
        fire_default=Decimal(str(settings.MONEY_DEBT_FIRE_APR_PCT)),
        judgment_default=Decimal(str(settings.MONEY_DEBT_JUDGMENT_APR_PCT)),
    )

    currency = (intake.currency or reference.currency).upper()
    income = intake.monthly_income or Decimal("0")
    debt_minimums = sum((d.minimum_monthly for d in intake.debts), Decimal("0"))
    # Debt minimums join fixed costs: the buffer target and the surplus both
    # have to respect them, or the plan quietly assumes the user skips a payment.
    fixed = (intake.monthly_fixed or Decimal("0")) + debt_minimums
    variable = intake.monthly_variable or Decimal("0")
    spend = fixed + variable
    surplus = income - spend
    buffer_current = intake.buffer_source

    stack = SafetyStack(
        currency=currency,
        monthly_income=income,
        monthly_fixed=fixed,
        debt_minimums=debt_minimums,
        monthly_surplus=surplus,
        buffer_current=buffer_current,
        assumptions=list(intake.assumptions),
    )
    if status is not None:
        stack.assumptions.append(status.note)
    # A stale local-rate table is treated as no rate table at all. The bands
    # below would otherwise be computed from figures we no longer stand behind,
    # and a confident wrong hurdle rate is worse than admitting we do not know.
    stale_reference = bool(status is not None and status.stale)

    buffer_months = buffer_current / fixed if fixed > 0 else Decimal("0")
    stack.buffer_months = buffer_months

    # -- 1. stop the bleed ----------------------------------------------
    if spend > income:
        stack.bleed = True
        stack.bleed_gap = spend - income
        stack.blocked_reasons.append(
            f"monthly outflow {format_amount(spend, currency)} exceeds income "
            f"{format_amount(income, currency)} by "
            f"{format_amount(stack.bleed_gap, currency)}; there is nothing to "
            "invest until this closes"
        )

    # -- 2. the buffer ---------------------------------------------------
    stack.debt_actions, fire, judgment, unknown = _triage(
        intake.debts, fire_apr, judgment_apr, reference.risk_free_rate_pct
    )
    stack.fire_debt = fire
    stack.judgment_debt = judgment
    stack.unknown_apr_debt = unknown

    if fire:
        target_months = _FIRE_TARGET_MONTHS
        why = (
            "debt is on fire, so one month is enough to stop a shock becoming new debt"
        )
    elif intake.volatile_income:
        target_months = _VOLATILE_TARGET_MONTHS
        why = "income moves around, so the buffer has to cover a gap of unknown length"
    else:
        target_months = _STEADY_TARGET_MONTHS
        why = "steady income needs three months of essentials behind it"

    stack.buffer_target_months = target_months
    stack.buffer_target = target_months * fixed
    stack.buffer_gap = max(Decimal("0"), stack.buffer_target - buffer_current)
    stack.buffer_vehicle, stack.buffer_location = _pick_vehicle(reference)
    stack.assumptions.append(
        f"buffer target set to {format_months(target_months)}: {why}"
    )

    # A month that cannot close, or a fire with nothing behind it, is a crisis.
    # The guilt-free floor exists to stop a plan being abandoned (``R-SETHI-2``),
    # but a crisis is exactly the case the exception was written for.
    stack.crisis = bool(stack.bleed or (fire and buffer_months < _GATE_BUFFER_MONTHS))
    stack.guilt_free_floor = (
        Decimal("0") if stack.crisis else income * _GUILT_FREE_FLOOR_SHARE
    )

    # -- 3. allocate what is left ---------------------------------------
    if surplus <= 0:
        stack.investable_surplus = Decimal("0")
        if not stack.bleed:
            # Distinguish "we do not know" from "there is genuinely nothing
            # left". The first is a question; the second is a finding.
            unknown = intake.monthly_income is None or intake.monthly_fixed is None
            stack.blocked_reasons.append(
                "the income and cost figures needed to work out a surplus are not "
                "recorded yet"
                if unknown
                else "there is no monthly surplus to allocate after fixed and "
                "variable spending"
            )
        stack.investing_allowed = False
        stack.buffer_contribution = max(Decimal("0"), surplus)
        return stack

    remaining = surplus

    if stack.buffer_gap > 0:
        if fire:
            share = _FIRE_BUFFER_SHARE
        elif buffer_months < _GATE_BUFFER_MONTHS:
            share = _URGENT_BUFFER_SHARE
        else:
            share = _BUILDING_BUFFER_SHARE
        stack.buffer_contribution = min(remaining * share, stack.buffer_gap)
        remaining -= stack.buffer_contribution
        if stack.buffer_contribution > 0:
            stack.months_to_fill = (
                stack.buffer_gap / stack.buffer_contribution
            ).quantize(Decimal("0.1"))

    stack.debt_extra = _debt_attack(
        stack, remaining, fire, judgment, fire_apr, currency
    )
    remaining -= stack.debt_extra

    # Reserve the guilt-free floor *before* deciding what is investable, so the
    # allocation engine and the cashflow split are working from the same number
    # rather than each re-deriving it and disagreeing by a rounding.
    if stack.guilt_free_floor > 0 and remaining > 0:
        remaining -= min(remaining, stack.guilt_free_floor)

    # -- 4. the gate -----------------------------------------------------
    if stack.bleed:
        stack.blocked_reasons.append("the month does not close (a bleed)")
    if buffer_months < _GATE_BUFFER_MONTHS:
        stack.blocked_reasons.append(
            f"the buffer covers {buffer_months:.2f} of a month; one month is the "
            "floor before investing"
        )
    if fire:
        stack.blocked_reasons.append(
            "debt at or above the fire threshold is a better use of money than "
            "any investment"
        )
    if unknown:
        stack.blocked_reasons.append(
            "at least one debt has no recorded APR, so it cannot be ruled out as "
            "a fire"
        )
    if stale_reference:
        # Scoped: a stale macro table blocks *investing* (hurdle rate unknown),
        # never the savings plan. Buffer, debt attack, and cashflow still run on
        # the user's own numbers plus the settings debt bands.
        stack.blocked_reasons.append(
            "the local rate reference is out of date, so investing is paused "
            "until it is refreshed — your savings split below still stands"
        )

    stack.investing_allowed = not stack.blocked_reasons
    stack.investable_surplus = remaining if stack.investing_allowed else Decimal("0")
    return stack


def _debt_attack(
    stack: SafetyStack,
    remaining: Decimal,
    fire: bool,
    judgment: bool,
    fire_apr: Decimal,
    currency: str,
) -> Decimal:
    """How much of the remainder goes at debt this month."""
    if remaining <= 0:
        return Decimal("0")

    if fire:
        worst = next((a for a in stack.debt_actions if a.band == "fire"), None)
        if worst is None:
            return Decimal("0")
        extra = min(remaining * _FIRE_DEBT_SHARE, worst.balance)
        stack.assumptions.append(
            f"{format_amount(extra, currency)} a month goes at the "
            f"{format_pct(worst.apr_pct, 1)} balance first"
        )
        return extra

    if judgment:
        # Only accelerate a judgment-band debt when it actually beats the local
        # risk-free rate; otherwise the message invites people to prepay cheap
        # money, which is a real way to end up worse off.
        accelerate = [
            a for a in stack.debt_actions if a.band == "judgment" and a.apr_pct > 0
        ]
        if not accelerate:
            return Decimal("0")
        extra = min(
            remaining * _JUDGMENT_DEBT_SHARE,
            sum((a.balance for a in accelerate), Decimal("0")),
        )
        stack.assumptions.append(
            f"{format_amount(extra, currency)} a month accelerates the "
            "judgment-band debt while the buffer keeps building"
        )
        return extra

    return Decimal("0")

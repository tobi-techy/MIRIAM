"""Stage 0 — intake. Normalize a user into an :class:`IntakeProfile`.

The pipeline's contract with the rest of the system is this object. Anything
that can produce one can run the whole money pipeline, which is what keeps the
engines pure and the tests cheap.

Two rules matter more than the field list:

  - **A missing number is ``None``, never a zero.** A silent zero is how a
    pipeline ends up telling someone with unrecorded income that they are
    overspending. Absent values stay absent and become a ``data_gap``.
  - **Everything assumed is recorded.** Each bridge appends to ``assumptions``,
    so the plan can label the guess instead of presenting it as fact
    (``R-AUDIT-1``).

Money is ``Decimal`` internally. The existing profile layer stores floats, so
the bridges convert through ``str`` rather than ``float`` to avoid inheriting
binary rounding artefacts.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Fields without which no diagnosis is meaningful (``diagnose`` → ``data_gap``).
REQUIRED_FIELDS: tuple[str, ...] = ("income_amount", "fixed_costs")

_FREQUENCY_FACTOR: dict[str, Decimal] = {
    "daily": Decimal("30.4375"),
    "weekly": Decimal("4.345"),
    "biweekly": Decimal("2.1725"),
    "fortnightly": Decimal("2.1725"),
    "monthly": Decimal("1"),
    "yearly": Decimal("0.0833333"),
    "annual": Decimal("0.0833333"),
    "annually": Decimal("0.0833333"),
}

# Horizon buckets used by the legacy profile layer, in months.
_HORIZON_BUCKETS: dict[str, int] = {"short": 6, "medium": 24, "long": 60}

_VOLATILE_VALUES = frozenset(
    {
        "variable",
        "volatile",
        "irregular",
        "unpredictable",
        "freelance",
        "seasonal",
        "commission",
        "lumpy",
        "inconsistent",
        "mixed",
    }
)


def _dec(value: Any) -> Decimal | None:
    """A tolerant amount → Decimal, or ``None``.

    Rejects bools (``True`` is not 1 naira) and anything non-numeric, and goes
    through ``str`` so a float like 500000.0 does not carry its representation
    error into the plan.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


def to_monthly(amount: Decimal | None, frequency: str | None) -> Decimal | None:
    """Normalize an amount to a monthly figure.

    Income arrives in whatever cadence the user speaks ("500k a week"); every
    engine downstream works monthly, so the conversion happens once, here.
    """
    if amount is None:
        return None
    factor = _FREQUENCY_FACTOR.get((frequency or "monthly").strip().casefold())
    if factor is None:
        return amount
    return amount * factor


class Debt(BaseModel):
    """One liability. APR is required for triage; without it the band is unknown."""

    model_config = ConfigDict(extra="forbid")

    label: str = "debt"
    balance: Decimal
    apr_pct: Decimal | None = None
    minimum_monthly: Decimal = Decimal("0")


class Goal(BaseModel):
    """A named goal with a date or a horizon."""

    model_config = ConfigDict(extra="forbid")

    label: str
    target_amount: Decimal | None = None
    horizon_months: int | None = None
    priority: int = 1


class IntakeProfile(BaseModel):
    """Everything Stage 0 collects or infers.

    ``risk_capacity`` and ``risk_tolerance`` are deliberately separate fields.
    Capacity is what the user can afford to lose (time, job stability,
    dependents); tolerance is how they feel about it. The allocation engine is
    driven by capacity and merely bounded by tolerance -- a user who "feels
    aggressive" on a single volatile income does not get a growth sleeve
    because of it.
    """

    model_config = ConfigDict(extra="forbid")

    # Location
    country: str | None = None
    currency: str | None = None
    age: int | None = None
    dependents: int | None = None

    # Income
    income_amount: Decimal | None = None
    income_frequency: str = "monthly"
    income_volatility: str | None = None  # steady | variable
    income_type: str | None = None

    # Outflow
    fixed_costs: Decimal | None = None
    variable_spend: Decimal | None = None

    # Balance sheet
    debts: list[Debt] = Field(default_factory=list)
    cash_on_hand: Decimal | None = None
    emergency_fund: Decimal | None = None
    existing_investments: Decimal | None = None

    # Goals
    goals: list[Goal] = Field(default_factory=list)

    # Capacity vs tolerance (kept apart on purpose)
    job_stability: str | None = None  # stable | unstable
    risk_tolerance: Literal["low", "medium", "high"] | None = None
    accepts_drawdown: bool = False
    can_self_custody: bool | None = None

    assumptions: list[str] = Field(default_factory=list)

    # -- derived reads --------------------------------------------------

    @property
    def monthly_income(self) -> Decimal | None:
        return to_monthly(self.income_amount, self.income_frequency)

    @property
    def monthly_fixed(self) -> Decimal | None:
        return self.fixed_costs

    @property
    def monthly_variable(self) -> Decimal | None:
        return self.variable_spend

    @property
    def volatile_income(self) -> bool:
        """True when income is not steady. Unknown counts as volatile.

        Unknown is treated as volatile because the failure modes are asymmetric:
        assuming steady income understates the buffer a lumpy earner needs, and
        that is the direction that hurts.
        """
        if self.income_volatility is None:
            return True
        return self.income_volatility.strip().casefold() in _VOLATILE_VALUES

    @property
    def buffer_source(self) -> Decimal:
        """The liquid money that counts toward the buffer."""
        if self.emergency_fund is not None:
            return self.emergency_fund
        if self.cash_on_hand is not None:
            return self.cash_on_hand
        return Decimal("0")

    @property
    def horizon_months(self) -> int | None:
        """The longest goal horizon. ``None`` when no goal has a horizon."""
        horizons = [g.horizon_months for g in self.goals if g.horizon_months]
        return max(horizons) if horizons else None

    @property
    def total_debt(self) -> Decimal:
        return sum((d.balance for d in self.debts), Decimal("0"))

    @property
    def missing_required(self) -> list[str]:
        """Required fields that are still absent (drives ``data_gap``)."""
        missing: list[str] = []
        if self.monthly_income is None:
            missing.append("income_amount")
        if self.fixed_costs is None:
            missing.append("fixed_costs")
        return missing

    def with_assumption(self, note: str) -> IntakeProfile:
        """Return a copy with one more recorded assumption."""
        if note in self.assumptions:
            return self
        return self.model_copy(update={"assumptions": [*self.assumptions, note]})


class AccountsSnapshot(BaseModel):
    """Balances read from a connected source rather than stated by the user.

    Connected balances outrank anything the user typed, because a ledger is
    evidence and a memory is not. Keeping them in a separate object, rather than
    writing straight into the profile, is what lets the merge be explicit and the
    provenance be recorded on the plan.
    """

    model_config = ConfigDict(extra="forbid")

    currency: str | None = None
    cash: Decimal | None = None
    investments: Decimal | None = None
    debts: list[Debt] = Field(default_factory=list)
    source: str = "ledger"


def apply_accounts(
    intake: IntakeProfile, accounts: AccountsSnapshot | dict[str, Any] | None
) -> IntakeProfile:
    """Fold connected balances into the profile, recording what came from where.

    Only fields the snapshot actually carries are overridden, so a partial sync
    cannot silently blank a number the user gave us. Every override lands in
    ``assumptions``, so the plan says which figures came from a connection rather
    than from the person.
    """
    if accounts is None:
        return intake
    if isinstance(accounts, dict):
        raw = dict(accounts)
        raw["debts"] = [_coerce_debt(d) for d in raw.get("debts") or []]
        snapshot = AccountsSnapshot.model_validate(raw)
    else:
        snapshot = accounts

    updates: dict[str, Any] = {}
    notes: list[str] = []

    if snapshot.currency and not intake.currency:
        updates["currency"] = snapshot.currency.upper()
    if snapshot.cash is not None:
        updates["cash_on_hand"] = snapshot.cash
        notes.append(f"cash of {snapshot.cash} read from {snapshot.source}")
    if snapshot.investments is not None:
        updates["existing_investments"] = snapshot.investments
        notes.append(
            f"investments of {snapshot.investments} read from {snapshot.source}"
        )
    if snapshot.debts:
        updates["debts"] = snapshot.debts
        notes.append(f"{len(snapshot.debts)} debt(s) read from {snapshot.source}")

    if not updates:
        return intake
    merged = intake.model_copy(update=updates)
    for note in notes:
        merged = merged.with_assumption(note)
    return merged


# ---------------------------------------------------------------------------
# Bridges from the existing layers
# ---------------------------------------------------------------------------


def from_financial_profile(
    profile: Any,
    *,
    country: str | None = None,
    currency: str | None = None,
    age: int | None = None,
) -> IntakeProfile:
    """Build an :class:`IntakeProfile` from ``financial.profile.FinancialProfile``.

    Reuse, not re-derivation: the existing layer already normalizes what the
    user said and carries provenance for it, so this only reshapes it.

    What the existing profile genuinely does not hold -- country, age, per-debt
    APR, job stability, self-custody -- stays ``None`` and is recorded as an
    assumption rather than guessed. That is what lets the pipeline answer
    ``data_gap`` honestly instead of inventing a 20% APR to run on.
    """
    from miriam_agent.financial.profile import FinancialProfile

    if not isinstance(profile, FinancialProfile):
        raise TypeError("from_financial_profile expects a FinancialProfile")

    assumptions: list[str] = []
    debts: list[Debt] = []

    debt_total = profile.money("debt")
    if debt_total is not None and debt_total > 0:
        debts.append(Debt(label="recorded debt", balance=Decimal(str(debt_total))))
        assumptions.append(
            "debt is recorded as a single balance with no APR; the triage band "
            "cannot be determined until the rate is known"
        )

    urgency = profile.value("goal_horizon")
    horizon = _HORIZON_BUCKETS.get(str(urgency).strip().casefold()) if urgency else None
    goals: list[Goal] = []
    goal_label = profile.value("financial_goal")
    if isinstance(goal_label, str) and goal_label.strip():
        goals.append(Goal(label=goal_label.strip(), horizon_months=horizon, priority=1))

    fund = profile.money("emergency_fund")
    cash = profile.money("savings")
    if fund is None and cash is not None:
        assumptions.append(
            "savings is treated as the buffer (no separate emergency fund recorded)"
        )

    volatility = profile.value("income_volatility")
    intake = IntakeProfile(
        country=country,
        currency=(currency or profile.currency()).upper(),
        age=age,
        dependents=(
            int(profile.value("financial_dependents"))
            if profile.value("financial_dependents") is not None
            else None
        ),
        income_amount=_dec(profile.money("income_amount")),
        income_frequency=str(profile.value("income_frequency") or "monthly"),
        income_volatility=(str(volatility) if volatility is not None else None),
        income_type=(
            str(profile.value("income_type"))
            if profile.value("income_type") is not None
            else None
        ),
        fixed_costs=_dec(profile.money("essential_expenses")),
        variable_spend=_dec(profile.money("discretionary_expenses")),
        debts=debts,
        cash_on_hand=_dec(cash),
        emergency_fund=_dec(fund),
        existing_investments=_dec(profile.money("current_investments")),
        goals=goals,
        assumptions=assumptions,
    )

    if intake.country is None:
        intake = intake.with_assumption(
            "country is unknown from the profile; local inflation and rates are "
            "not localized"
        )
    if intake.buffer_source == 0 and cash is None and fund is None:
        intake = intake.with_assumption(
            "no cash or buffer on record; treated as no buffer rather than assumed"
        )
    return intake


def from_onboarding_state(
    state: Any,
    *,
    country: str | None = None,
    currency: str | None = None,
    age: int | None = None,
) -> IntakeProfile:
    """Build an :class:`IntakeProfile` from conversational onboarding state.

    Delegates to the existing ``profile_from_onboarding_state`` projection and
    then to :func:`from_financial_profile`, so the conversational arc and the
    money pipeline agree on what the user said without a second extractor.
    """
    from miriam_agent.financial.profile import profile_from_onboarding_state

    profile = profile_from_onboarding_state(state)
    intake = from_financial_profile(
        profile, country=country, currency=currency, age=age
    )

    goal_meta = getattr(state, "goal_meta", None) or {}
    if isinstance(goal_meta, dict) and intake.goals:
        cost = _dec(goal_meta.get("estimated_cost"))
        if cost is not None:
            intake.goals[0].target_amount = cost
    return intake


def from_mapping(data: dict[str, Any]) -> IntakeProfile:
    """Build an :class:`IntakeProfile` from a plain dict (fixtures, API bodies).

    Debt entries accept either a nested list or a flat balance, because both
    shapes arrive in practice. Unknown keys are rejected by the model so a
    drifting fixture fails loudly instead of silently dropping a number.
    """
    payload = dict(data)
    payload["debts"] = [_coerce_debt(d) for d in payload.get("debts") or []]
    payload["goals"] = [_coerce_goal(g) for g in payload.get("goals") or []]
    return IntakeProfile.model_validate(payload)


def _coerce_debt(raw: Any) -> Any:
    if isinstance(raw, Debt):
        return raw
    if not isinstance(raw, dict):
        return raw
    return {
        "label": raw.get("label") or raw.get("name") or "debt",
        "balance": raw.get("balance"),
        "apr_pct": raw.get("apr_pct", raw.get("apr")),
        "minimum_monthly": raw.get("minimum_monthly", raw.get("minimum") or 0),
    }


def _coerce_goal(raw: Any) -> Any:
    if isinstance(raw, Goal):
        return raw
    if not isinstance(raw, dict):
        return raw
    horizon = raw.get("horizon_months")
    if horizon is None and raw.get("horizon"):
        horizon = _HORIZON_BUCKETS.get(str(raw["horizon"]).strip().casefold())
    return {
        "label": raw.get("label") or raw.get("name") or "goal",
        "target_amount": raw.get("target_amount"),
        "horizon_months": horizon,
        "priority": raw.get("priority", 1),
    }

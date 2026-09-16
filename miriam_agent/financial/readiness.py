"""Investment readiness engine (spec §15).

Investing is introduced when it is *earned*, not when it is asked for. This
module is the gate: it returns one of five statuses, the reason, and the next
step. The LLM may explain the verdict; it can never change one.

Ordered checks -- the first that fires wins:

  REVIEW_REQUIRED      contradictory evidence, unsupported jurisdiction, or an
                       unverified identity the policy layer must confirm
  NOT_READY            no real surplus: investing would be borrowing
  BUILD_SAFETY_FIRST   surplus exists but the base does not (buffer, debt)
  READY_TO_START       the base holds and the horizon is not short
  READY_TO_AUTOMATE    ready, *and* the behaviour is already automatic
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from miriam_agent.financial.diagnosis import Diagnosis
from miriam_agent.financial.profile import FinancialProfile, horizon_months

NOT_READY = "NOT_READY"
BUILD_SAFETY_FIRST = "BUILD_SAFETY_FIRST"
READY_TO_START = "READY_TO_START"
READY_TO_AUTOMATE = "READY_TO_AUTOMATE"
REVIEW_REQUIRED = "REVIEW_REQUIRED"

STATUSES: tuple[str, ...] = (
    NOT_READY,
    BUILD_SAFETY_FIRST,
    READY_TO_START,
    READY_TO_AUTOMATE,
    REVIEW_REQUIRED,
)

_NEXT_STEP: dict[str, str] = {
    NOT_READY: "fix_cashflow_gap",
    BUILD_SAFETY_FIRST: "build_emergency_buffer",
    READY_TO_START: "investment_education",
    READY_TO_AUTOMATE: "set_up_the_investment",
    REVIEW_REQUIRED: "review_account",
}

# The thresholds the diagnosis and allocation engines already use, restated so
# all three engines agree on what "a base" means.
_MIN_SURPLUS_RATIO = 0.15
_MIN_BUFFER_MONTHS = 3.0
_CRUSHING_DEBT_MONTHS = 6.0


class Readiness(BaseModel):
    """The investment-readiness verdict (spec §15)."""

    model_config = ConfigDict(extra="forbid")

    status: str
    reason: str
    recommended_next_step: str
    blockers: list[str] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)
    eligible_products: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


def _buffer_months(profile: FinancialProfile) -> float | None:
    essentials = profile.money("essential_expenses") or 0.0
    if essentials <= 0:
        return None
    buffer = profile.money("emergency_fund")
    if buffer is None:
        buffer = profile.money("savings") or 0.0
    return buffer / essentials


def _eligible_products(status: str) -> list[str]:
    if status == READY_TO_AUTOMATE:
        return ["education", "starter_portfolio", "auto_invest"]
    if status == READY_TO_START:
        return ["education", "starter_portfolio"]
    return []


def assess_readiness(
    profile: FinancialProfile,
    diagnosis: Diagnosis | None = None,
    *,
    kyc_verified: bool | None = None,
    jurisdiction: str | None = None,
    supported_jurisdictions: tuple[str, ...] | None = None,
    limits: dict[str, Any] | None = None,
) -> Readiness:
    """The investment verdict for this user, right now (spec §15).

    ``kyc_verified`` / ``jurisdiction`` / ``limits`` come from the Go backend
    (identity and policy are Go's, never Miriam's). An unknown KYC state is
    treated as *unverified* -- the gate fails closed, never open.
    """
    income = profile.money("income_amount")
    essentials = profile.money("essential_expenses") or 0.0
    debt = profile.money("debt") or 0.0
    surplus_ratio = None
    if income and income > 0:
        surplus_ratio = (income - essentials) / income
    buffer_months = _buffer_months(profile)
    debt_months = debt / income if income else None
    horizon = horizon_months(profile)
    volatile = profile.volatile_income()

    blockers: list[str] = []
    signals = {
        "income": income,
        "essential_expenses": essentials or None,
        "surplus_ratio": round(surplus_ratio, 3) if surplus_ratio is not None else None,
        "buffer_months": round(buffer_months, 2) if buffer_months is not None else None,
        "debt_income_months": round(debt_months, 2) if debt_months is not None else None,
        "income_volatility": "variable" if volatile else "steady",
        "goal_horizon_months": horizon,
        "kyc_verified": bool(kyc_verified),
        "jurisdiction": jurisdiction,
        "limits_available": bool(limits),
    }

    def verdict(
        status: str, reason: str, extra_blockers: list[str] | None = None
    ) -> Readiness:
        return Readiness(
            status=status,
            reason=reason,
            recommended_next_step=_NEXT_STEP[status],
            blockers=[*blockers, *(extra_blockers or [])],
            signals=signals,
            eligible_products=_eligible_products(status),
        )

    # 1. Review: evidence that contradicts itself, or an identity/policy gate.
    contradictory = (
        surplus_ratio is not None
        and surplus_ratio < 0
        and (profile.money("savings") or 0) > (income or 0)
    )
    if contradictory:
        blockers.append("savings exceed income while outflow exceeds income")
    unsupported = (
        jurisdiction is not None
        and supported_jurisdictions is not None
        and jurisdiction.upper() not in {j.upper() for j in supported_jurisdictions}
    )
    if unsupported:
        blockers.append(f"jurisdiction {jurisdiction} is not supported")
    if kyc_verified is False:
        blockers.append("identity verification is not complete")

    if contradictory:
        return verdict(REVIEW_REQUIRED, "this needs a human look before investing")
    if unsupported:
        return verdict(REVIEW_REQUIRED, "jurisdiction is not supported for investing")
    if kyc_verified is False:
        return verdict(REVIEW_REQUIRED, "finish identity verification before investing")

    # 2. Not ready: no surplus to invest.
    if not income or surplus_ratio is None or surplus_ratio <= 0:
        return verdict(
            NOT_READY,
            "there is no surplus yet, so investing would mean borrowing",
            ["no positive surplus"],
        )
    if surplus_ratio < _MIN_SURPLUS_RATIO:
        return verdict(
            NOT_READY,
            "the surplus is too thin to invest without hurting the month",
            ["surplus under 15% of income"],
        )

    # 3. Build safety first: the base is not there yet.
    if debt_months is not None and debt_months >= _CRUSHING_DEBT_MONTHS:
        return verdict(
            BUILD_SAFETY_FIRST,
            "debt this size is a better use of money than investing",
            ["debt at or above 6x monthly income"],
        )
    if buffer_months is None or buffer_months < _MIN_BUFFER_MONTHS:
        months = f"{buffer_months:.1f}" if buffer_months is not None else "unknown"
        return verdict(
            BUILD_SAFETY_FIRST,
            f"the buffer covers {months} months; three is the floor before investing",
            ["emergency buffer below three months"],
        )
    if volatile and buffer_months < _MIN_BUFFER_MONTHS * 2:
        return verdict(
            BUILD_SAFETY_FIRST,
            "income moves around, so the buffer wants six months before investing",
            ["volatile income wants a six-month buffer"],
        )

    # 4. Ready to start -- unless the horizon or the debt says otherwise.
    if horizon == 6:
        return verdict(
            BUILD_SAFETY_FIRST,
            "the goal is too near-term to expose to markets",
            ["goal horizon is short"],
        )
    if (
        diagnosis is not None
        and "high_debt_burden" in diagnosis.problems()
        and debt_months is not None
        and debt_months >= 3
    ):
        return verdict(
            BUILD_SAFETY_FIRST,
            "debt still takes a real bite of income; clear it down first",
            ["debt at or above 3x monthly income"],
        )

    # 5. Ready to automate: ready, and the habit already runs.
    saving = str(profile.value("saving_behavior") or "").casefold()
    investing = str(profile.value("investing_behavior") or "").casefold()
    if (saving == "automatic" or investing == "active") and kyc_verified is not False:
        return verdict(READY_TO_AUTOMATE, "the base holds and the habit already runs")

    return verdict(
        READY_TO_START,
        "surplus, buffer and horizon line up; starting small is reasonable",
    )
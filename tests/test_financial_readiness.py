"""Investment readiness gate (§15) and policy/eligibility engine (§18).

Both are fail-closed: an unknown KYC state, an unreported limit or a missing
balance blocks the action rather than waving it through. The LLM can narrate
these verdicts; it can never change one.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.financial.eligibility import evaluate_investment_action  # noqa: E402
from miriam_agent.financial.profile import FinancialProfile, extract_profile  # noqa: E402
from miriam_agent.financial.readiness import (  # noqa: E402
    BUILD_SAFETY_FIRST,
    NOT_READY,
    READY_TO_AUTOMATE,
    READY_TO_START,
    REVIEW_REQUIRED,
    STATUSES,
    assess_readiness,
)


def _profile(text=None, **facts):
    if text:
        return extract_profile(text)
    profile = FinancialProfile()
    for field, value in facts.items():
        profile.set_fact(field, value, source="conversation", confidence=0.9)
    return profile


def _healthy(**overrides):
    facts = {
        "income_amount": 1_000_000,
        "essential_expenses": 400_000,
        "emergency_fund": 1_500_000,
        "goal_horizon": "long",
        "financial_goal": "build wealth",
    }
    facts.update(overrides)
    return _profile(**facts)


_LIMITS = {
    "kyc_tier": "tier_2",
    "can_invest": True,
    "max_transaction_usd": "5000.00",
    "max_daily_usd": "10000.00",
}


# -----------------------------------------------------------------------
# Readiness: the ladder
# -----------------------------------------------------------------------


def test_statuses_are_a_closed_set():
    assert set(STATUSES) == {
        NOT_READY,
        BUILD_SAFETY_FIRST,
        READY_TO_START,
        READY_TO_AUTOMATE,
        REVIEW_REQUIRED,
    }


def test_no_surplus_is_not_ready():
    verdict = assess_readiness(
        _profile(income_amount=300_000, essential_expenses=300_000)
    )
    assert verdict.status == NOT_READY
    assert verdict.recommended_next_step == "fix_cashflow_gap"
    assert "no positive surplus" in verdict.blockers


def test_thin_surplus_is_not_ready():
    verdict = assess_readiness(
        _profile(income_amount=500_000, essential_expenses=470_000)
    )
    assert verdict.status == NOT_READY


def test_thin_buffer_builds_safety_first():
    verdict = assess_readiness(
        _profile(
            income_amount=1_000_000,
            essential_expenses=400_000,
            emergency_fund=200_000,
        )
    )
    assert verdict.status == BUILD_SAFETY_FIRST
    assert verdict.recommended_next_step == "build_emergency_buffer"
    assert verdict.eligible_products == []


def test_crushing_debt_blocks_before_the_buffer():
    verdict = assess_readiness(
        _profile(
            income_amount=500_000,
            essential_expenses=200_000,
            emergency_fund=600_000,
            debt=4_000_000,
        )
    )
    assert verdict.status == BUILD_SAFETY_FIRST
    assert any("6x" in b for b in verdict.blockers)


def test_healthy_base_is_ready_to_start():
    verdict = assess_readiness(_healthy())
    assert verdict.status == READY_TO_START
    assert verdict.recommended_next_step == "investment_education"
    assert "starter_portfolio" in verdict.eligible_products


def test_automatic_saving_promotes_to_automate():
    verdict = assess_readiness(_healthy(saving_behavior="automatic"))
    assert verdict.status == READY_TO_AUTOMATE
    assert "auto_invest" in verdict.eligible_products


def test_short_horizon_blocks_even_a_healthy_base():
    verdict = assess_readiness(_healthy(goal_horizon="short"))
    assert verdict.status == BUILD_SAFETY_FIRST
    assert "goal horizon is short" in verdict.blockers
    assert "near-term" in verdict.reason


def test_volatile_income_wants_a_six_month_buffer():
    verdict = assess_readiness(
        _healthy(income_volatility="variable", emergency_fund=1_400_000)
    )
    assert verdict.status == BUILD_SAFETY_FIRST
    assert any("six-month" in b for b in verdict.blockers)


def test_unknown_kyc_is_unverified_and_blocks():
    verdict = assess_readiness(_healthy(), kyc_verified=False)
    assert verdict.status == REVIEW_REQUIRED
    assert verdict.recommended_next_step == "review_account"


def test_unsupported_jurisdiction_blocks():
    verdict = assess_readiness(
        _healthy(), jurisdiction="US", supported_jurisdictions=("NG", "GH")
    )
    assert verdict.status == REVIEW_REQUIRED
    assert any("US" in b for b in verdict.blockers)


def test_supported_jurisdiction_does_not_block():
    verdict = assess_readiness(
        _healthy(), jurisdiction="NG", supported_jurisdictions=("NG", "GH")
    )
    assert verdict.status in (READY_TO_START, READY_TO_AUTOMATE)


def test_contradictory_evidence_needs_a_human():
    verdict = assess_readiness(
        _profile(income_amount=200_000, essential_expenses=400_000, savings=1_000_000)
    )
    assert verdict.status == REVIEW_REQUIRED


def test_signals_carry_the_evidence():
    verdict = assess_readiness(_healthy(), kyc_verified=True)
    assert verdict.signals["surplus_ratio"] == 0.6
    assert verdict.signals["buffer_months"] == 3.75
    assert verdict.signals["kyc_verified"] is True


# -----------------------------------------------------------------------
# Eligibility: the policy verdict
# -----------------------------------------------------------------------


def test_clean_action_is_allowed():
    decision = evaluate_investment_action(
        4_000,
        _LIMITS,
        kyc_verified=True,
        jurisdiction="NG",
        supported_jurisdictions=("NG",),
        available_balance=85_000,
        idempotency_key="idem-1",
    )
    assert decision.allowed is True
    assert decision.reasons == []
    assert decision.checks["kyc"] == "pass"
    assert decision.checks["max_transaction"] == "pass"
    assert decision.limits_used["max_transaction"] == 5000.0


def test_amount_above_the_limit_is_denied():
    decision = evaluate_investment_action(600_000, _LIMITS, kyc_verified=True)
    assert decision.allowed is False
    assert any("per-transaction" in r for r in decision.reasons)


def test_insufficient_balance_is_denied():
    decision = evaluate_investment_action(
        20_000, _LIMITS, kyc_verified=True, available_balance=5_000
    )
    assert decision.allowed is False
    assert any("exceeds the available balance" in r for r in decision.reasons)


def test_ineligible_account_is_denied():
    decision = evaluate_investment_action(
        1_000, {"can_invest": False, "max_transaction_usd": "5000"}, kyc_verified=True
    )
    assert decision.allowed is False
    assert any("not enabled" in r for r in decision.reasons)


def test_unsupported_asset_is_denied():
    decision = evaluate_investment_action(
        1_000,
        _LIMITS,
        kyc_verified=True,
        asset={"symbol": "DOGE", "tradable": False},
    )
    assert decision.allowed is False
    assert any("not tradable" in r for r in decision.reasons)


def test_unknown_kyc_fails_closed():
    decision = evaluate_investment_action(1_000, _LIMITS, kyc_verified=None)
    assert decision.allowed is False
    assert decision.checks["kyc"] == "unknown"


def test_missing_limits_fails_closed():
    decision = evaluate_investment_action(1_000, None, kyc_verified=True)
    assert decision.allowed is False
    assert decision.checks["limits"] == "unavailable"


def test_missing_idempotency_is_recorded():
    decision = evaluate_investment_action(1_000, _LIMITS, kyc_verified=True)
    assert decision.checks["idempotency"] == "missing"


def test_unreported_limit_is_unknown_not_zero():
    """A limit the backend did not report must never read as 'unlimited'."""
    decision = evaluate_investment_action(1_000, {"can_invest": True}, kyc_verified=True)
    assert decision.checks["max_transaction"] == "unknown"
    assert "max_transaction" not in decision.limits_used


def test_string_amounts_from_go_are_parsed():
    decision = evaluate_investment_action(
        4_000,
        {"max_transaction_usd": "5000.00", "max_daily_usd": "10000.00"},
        kyc_verified=True,
    )
    assert decision.checks["max_transaction"] == "pass"


def test_negative_amount_is_denied_and_auditable():
    decision = evaluate_investment_action(-5, None, kyc_verified=False)
    assert decision.allowed is False
    assert decision.checks["amount_positive"] == "fail"
    assert decision.checks["kyc"] == "fail"
    assert decision.to_dict()["reasons"]
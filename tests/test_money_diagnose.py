"""The 9-way diagnosis classifier, one case per problem type.

The ordering is the brief's own enumeration (``MONEY-RULES.md`` §4) and the
tie-breaks matter as much as the individual rules: a bleed outranks everything,
and a missing buffer outranks expensive debt because the buffer is what stops
the next shock becoming new debt.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.money import assess_safety, diagnose, from_mapping, lookup

HEALTHY = {
    "income_amount": "5000",
    "income_frequency": "monthly",
    "income_volatility": "steady",
    "fixed_costs": "2000",
    "variable_spend": "500",
    "cash_on_hand": "20000",
    "job_stability": "stable",
    "dependents": 0,
}


def dx(**overrides):
    country = overrides.pop("country", "US")
    currency = overrides.pop("currency", "USD")
    payload = {"country": country, "currency": currency, **HEALTHY, **overrides}
    profile = from_mapping(payload)
    reference, status = lookup(profile.country)
    return diagnose(profile, reference=reference, status=status)


def test_data_gap_when_the_minimum_inputs_are_absent():
    result = dx(income_amount=None, fixed_costs=None)
    assert result.problem_type == "data_gap"
    assert result.confidence == "low"
    assert "can't diagnose" in result.blunt


def test_cashflow_bleed_when_spend_exceeds_income():
    result = dx(income_amount="1000", fixed_costs="800", variable_spend="400")
    assert result.problem_type == "cashflow_bleed"
    assert "bleed" in result.blunt


def test_bleed_outranks_a_fire_debt():
    """The most urgent ordering claim in the diagnosis.

    A month that does not close cannot service anything, so the bleed is the
    primary problem even when a 40% loan is also sitting there.
    """
    result = dx(
        income_amount="1000",
        fixed_costs="800",
        variable_spend="400",
        debts=[
            {
                "label": "card",
                "balance": "9000",
                "apr_pct": "40",
                "minimum_monthly": "300",
            }
        ],
    )
    assert result.problem_type == "cashflow_bleed"
    assert "40.0% APR" in result.blunt


def test_no_buffer_outranks_expensive_debt():
    """Buffer before debt, because the buffer stops the next debt."""
    result = dx(
        cash_on_hand="0",
        debts=[
            {
                "label": "card",
                "balance": "9000",
                "apr_pct": "35",
                "minimum_monthly": "300",
            }
        ],
    )
    assert result.problem_type == "no_buffer"
    # The fire is still named, because it is the fastest-moving number.
    assert "35.0% APR" in result.blunt


def test_high_interest_debt_once_the_buffer_holds():
    result = dx(
        debts=[
            {
                "label": "card",
                "balance": "9000",
                "apr_pct": "29",
                "minimum_monthly": "300",
            }
        ]
    )
    assert result.problem_type == "high_interest_debt"
    assert "29.0% APR" in result.blunt
    assert any("fire threshold" in line for line in result.evidence)


def test_inflation_erosion_for_idle_cash_in_a_high_inflation_currency():
    """The case that makes imported US advice wrong."""
    result = dx(country="NG", currency="NGN", existing_investments="0")
    assert result.problem_type == "inflation_erosion"
    assert "eroding" in result.blunt
    assert any("inflation" in line for line in result.evidence)


def test_goal_mismatch_when_market_money_is_needed_soon():
    result = dx(
        existing_investments="5000",
        goals=[{"label": "deposit", "horizon_months": 12}],
    )
    assert result.problem_type == "goal_mismatch"
    assert "12 months" in result.blunt


def test_under_earning_when_costs_are_lean_and_income_is_the_constraint():
    result = dx(
        income_amount="2000",
        fixed_costs="1100",
        variable_spend="650",
        cash_on_hand="3000",
        existing_investments="0",
    )
    assert result.problem_type == "under_earning"
    assert "what you earn" in result.blunt


def test_overconfidence_risk_from_stated_appetite():
    result = dx(
        income_volatility="variable",
        job_stability="unstable",
        goals=[{"label": "all in on crypto", "horizon_months": 120}],
        risk_tolerance="high",
    )
    assert result.problem_type == "overconfidence_risk"
    assert any("appetite" in line for line in result.evidence)


def test_idle_surplus_when_the_foundation_holds_and_nothing_is_invested():
    result = dx(existing_investments="0")
    assert result.problem_type == "idle_surplus"
    assert "Nothing is invested" in result.blunt


def test_a_healthy_plan_still_produces_a_diagnosis_and_evidence():
    result = dx(existing_investments="50000")
    assert result.problem_type in {
        "idle_surplus",
        "under_earning",
        "goal_mismatch",
    }
    assert result.evidence
    assert result.confidence in {"high", "medium", "low"}


def test_every_diagnosis_carries_evidence():
    for overrides in (
        {"income_amount": None, "fixed_costs": None},
        {"income_amount": "1000", "fixed_costs": "800", "variable_spend": "400"},
        {"cash_on_hand": "0"},
        {"existing_investments": "0"},
    ):
        assert dx(**overrides).evidence


# ---------------------------------------------------------------------------
# Debt triage bands
# ---------------------------------------------------------------------------


def test_judgment_band_compares_against_the_local_risk_free_rate():
    """The 8-15% band is a judgment call, and the local rate is what decides it.

    In a high-rate currency 12% is cheap money; in a low-rate one it is not.
    """
    profile = from_mapping(
        {
            **HEALTHY,
            "country": "NG",
            "currency": "NGN",
            "debts": [
                {
                    "label": "loan",
                    "balance": "500000",
                    "apr_pct": "12",
                    "minimum_monthly": "20000",
                }
            ],
        }
    )
    reference, _ = lookup(profile.country)
    stack = assess_safety(profile, reference)
    loan = stack.debt_actions[0]
    assert loan.band == "judgment"
    assert "risk-free rate" in loan.reason
    # 12% against a 19% risk-free rate: pay it slowly, do not accelerate it.
    assert "below the local risk-free rate" in loan.reason


def test_fire_band_targets_the_most_expensive_balance_first():
    profile = from_mapping(
        {
            **HEALTHY,
            "debts": [
                {
                    "label": "cheap",
                    "balance": "10000",
                    "apr_pct": "6",
                    "minimum_monthly": "100",
                },
                {
                    "label": "expensive",
                    "balance": "2000",
                    "apr_pct": "32",
                    "minimum_monthly": "80",
                },
            ],
        }
    )
    reference, _ = lookup(profile.country)
    stack = assess_safety(profile, reference)
    assert stack.debt_actions[0].label == "expensive"
    assert stack.debt_actions[0].band == "fire"
    assert stack.debt_actions[0].strategy == "avalanche"


def test_a_cheap_debt_is_left_alone():
    profile = from_mapping(
        {
            **HEALTHY,
            "debts": [
                {
                    "label": "student",
                    "balance": "12000",
                    "apr_pct": "4",
                    "minimum_monthly": "150",
                }
            ],
        }
    )
    reference, _ = lookup(profile.country)
    stack = assess_safety(profile, reference)
    assert stack.debt_actions[0].band == "keep"
    assert stack.debt_extra == 0


def test_high_interest_debt_blocks_investing():
    profile = from_mapping(
        {
            **HEALTHY,
            "debts": [
                {
                    "label": "card",
                    "balance": "9000",
                    "apr_pct": "29",
                    "minimum_monthly": "300",
                }
            ],
        }
    )
    reference, _ = lookup(profile.country)
    stack = assess_safety(profile, reference)
    assert stack.investing_allowed is False
    assert any("fire threshold" in r for r in stack.blocked_reasons)


def test_fire_threshold_is_configurable_not_hardcoded():
    """Thresholds come from settings so they cannot drift from config."""
    from miriam_agent.config.settings import get_settings

    settings = get_settings()
    assert Decimal(str(settings.MONEY_DEBT_FIRE_APR_PCT)) == Decimal("15.0")
    assert Decimal(str(settings.MONEY_DEBT_JUDGMENT_APR_PCT)) == Decimal("8.0")

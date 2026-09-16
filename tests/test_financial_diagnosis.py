"""Deterministic financial diagnosis engine (spec §9).

Miriam does not describe money, she diagnoses it: one primary problem, ranked
secondaries, and a confidence she can defend. Every rule is a pure function of
the structured profile (+ optional ledger health, + optional spoken text), so
the same financial life always yields the same read -- and a test can falsify
any of it.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.financial.diagnosis import (  # noqa: E402
    PROBLEMS,
    PRIORITY_FOR_PROBLEM,
    SECONDARY_FLOOR,
    diagnose,
    missing_for_diagnosis,
    ready_to_diagnose,
)
from miriam_agent.financial.profile import FinancialProfile, extract_profile  # noqa: E402


# -----------------------------------------------------------------------
# The spec's own scenarios
# -----------------------------------------------------------------------


def _rich(**facts):
    """A profile built the honest way: said out loud, then extracted."""
    profile = FinancialProfile()
    for field, fact in facts.items():
        value = fact["value"] if isinstance(fact, dict) else fact
        profile.set_fact(field, value, source="conversation", confidence=0.9)
    return profile


def test_spec_aha_scenario_names_the_system_problem():
    """§10: steady income, money that disappears anyway -- progress that is
    'whatever survives the month' is a missing system, not weak discipline."""
    profile = extract_profile(
        "I make about 500k every month and spend maybe 350k. "
        "Honestly i do not track my spending and i never save."
    )
    diagnosis = diagnose(profile, goal_expected=False)
    assert diagnosis.primary_problem == "no_financial_system"
    assert "inconsistent_saving" in diagnosis.secondary_problems
    assert "lack_of_emergency_buffer" in diagnosis.secondary_problems
    assert diagnosis.priorities[0] == "create_default_allocation"
    assert not diagnosis.is_positive
    assert diagnosis.evidence  # a claim Miriam narrates must be traceable


def test_negative_cashflow_is_a_cashflow_imbalance():
    profile = _rich(income_amount=400_000, essential_expenses=500_000)
    diagnosis = diagnose(profile)
    assert diagnosis.primary_problem == "cashflow_imbalance"
    assert diagnosis.scores["cashflow_imbalance"] >= 0.9


def test_thin_surplus_is_a_cashflow_gap_too():
    profile = _rich(income_amount=500_000, essential_expenses=490_000)
    diagnosis = diagnose(profile)
    assert "cashflow_imbalance" in diagnosis.problems()


def test_crushing_debt_is_the_primary_problem():
    profile = _rich(income_amount=300_000, debt=3_000_000)
    diagnosis = diagnose(profile)
    assert diagnosis.primary_problem == "high_debt_burden"
    assert diagnosis.top_action() == "clear_high_cost_debt"


def test_irregular_income_names_volatility():
    profile = extract_profile("my income is irregular, i make about 500k monthly")
    diagnosis = diagnose(profile)
    assert diagnosis.primary_problem == "income_volatility"
    assert diagnosis.scores["income_volatility"] >= 0.8


def test_strong_foundation_reads_as_ready_and_positive():
    profile = extract_profile(
        "i earn 1200000 monthly, essentials are 600000, "
        "i have 2000000 saved as an emergency fund, "
        "no debt, goal is a house in mowe, for the long term"
    )
    diagnosis = diagnose(profile)
    assert diagnosis.primary_problem in (
        "strong_financial_foundation",
        "investment_ready",
    )
    assert diagnosis.is_positive is True
    assert "investment_gap" in diagnosis.secondary_problems or (
        "investment_ready" in diagnosis.problems()
    )
    assert diagnosis.confidence >= 0.5


# -----------------------------------------------------------------------
# Readiness neighbours (share diagnosis signals, are not readiness verdicts)
# -----------------------------------------------------------------------


def test_investor_without_a_foundation_is_flagged_early():
    """Eagerness to invest combined with a weak base is a finding in itself
    (§9 not_yet_investment_ready), long before the formal readiness gate."""
    profile = extract_profile(
        "i invest in stocks and i want to invest more, "
        "i make 200k and spend 190k, no savings"
    )
    diagnosis = diagnose(profile)
    assert "not_yet_investment_ready" in diagnosis.problems()
    assert diagnosis.top_action() == PRIORITY_FOR_PROBLEM[diagnosis.primary_problem]


def test_idle_cash_is_surfaced_on_a_fat_buffer():
    profile = _rich(
        income_amount=1_000_000,
        essential_expenses=400_000,
        emergency_fund=3_000_000,
        investment_experience="none",
    )
    diagnosis = diagnose(profile)
    assert "idle_cash" in diagnosis.problems()


def test_investment_gap_fires_when_the_base_is_built():
    profile = _rich(
        income_amount=1_000_000,
        essential_expenses=500_000,
        emergency_fund=2_000_000,
        investment_experience="none",
        goal_horizon="long",
        financial_goal="build wealth",
    )
    diagnosis = diagnose(profile)
    assert "investment_gap" in diagnosis.problems()


# -----------------------------------------------------------------------
# Goal handling follows the flow, not the engine (§4)
# -----------------------------------------------------------------------


def test_no_goal_at_the_aha_is_not_a_finding():
    """GOAL_DISCOVERY comes after FIRST_AHA, so a missing goal at the aha is
    simply the next question -- it must not win the read."""
    profile = _rich(income_amount=500_000, essential_expenses=400_000)
    early = diagnose(profile, goal_expected=False)
    assert early.primary_problem != "unclear_financial_goals"
    late = diagnose(profile, goal_expected=True)
    assert "unclear_financial_goals" in late.problems()


def test_a_vague_goal_is_a_finding_even_after_discovery():
    profile = _rich(
        income_amount=500_000,
        essential_expenses=300_000,
        financial_goal="save more",
    )
    diagnosis = diagnose(profile)
    assert "unclear_financial_goals" in diagnosis.problems()
    assert "define_goal_and_horizon" in diagnosis.priorities


# -----------------------------------------------------------------------
# Contracts the rest of the system depends on
# -----------------------------------------------------------------------


def test_vocabulary_is_closed_and_actions_are_total():
    for problem in PROBLEMS:
        assert problem in PRIORITY_FOR_PROBLEM, problem
    assert len(set(PROBLEMS)) == len(PROBLEMS)


def test_secondaries_respect_the_floor_and_cap():
    profile = extract_profile("i make 500k, i spend 450k")
    diagnosis = diagnose(profile, max_secondary=2)
    assert len(diagnosis.secondary_problems) <= 2
    for code in diagnosis.secondary_problems:
        assert diagnosis.scores[code] >= SECONDARY_FLOOR
        assert code != diagnosis.primary_problem


def test_minimum_fields_gate_the_aha():
    empty = FinancialProfile()
    assert ready_to_diagnose(empty) is False
    assert set(missing_for_diagnosis(empty)) == {
        "income_amount",
        "essential_expenses",
    }
    partial = extract_profile("i make 500k")
    assert ready_to_diagnose(partial) is False
    full = extract_profile("i make 500k and spend 350k")
    assert ready_to_diagnose(full) is True


def test_health_evidence_moves_the_read():
    """The same spoken life, seen through the ledger, must sharpen the engine
    rather than be averaged away."""
    spoken = "i make about 500k every month and spend maybe 450k"
    health = {
        "score": 30,
        "status": "needs_attention",
        "savings_rate_pct": 4.0,
        "budget_status": "over_budget",
    }
    without_ledger = diagnose(extract_profile(spoken), goal_expected=False)
    with_ledger = diagnose(extract_profile(spoken), health=health, goal_expected=False)
    assert with_ledger.scores["cashflow_imbalance"] >= without_ledger.scores[
        "cashflow_imbalance"
    ]
    assert (
        with_ledger.scores["excessive_discretionary_spending"]
        >= without_ledger.scores["excessive_discretionary_spending"]
    )


def test_spoken_leaks_corroborate_but_never_invent():
    """In production the conductor's full corpus feeds extraction, so a spoken
    behavior ("overspending") lands in the profile as a fact -- and only then
    can the same words corroborate it further. The ``text`` parameter may push
    an existing numeric read higher, but it can never conjure a problem the
    profile does not support at all (provenance, not vibes)."""
    combined = "i make 500k a month, my rent is 200k. i keep overspending on stuff"
    profile = extract_profile(combined)
    assert profile.value("spending_behavior") == "discretionary_heavy"
    base = diagnose(profile)
    assert "excessive_discretionary_spending" in base.problems()
    # Corroboration lifts the same read within the same sentence.
    boosted = diagnose(profile, text="i keep overspending on stuff")
    assert boosted.scores[
        "excessive_discretionary_spending"
    ] >= base.scores["excessive_discretionary_spending"]

    # Against a thin profile with no related base, the same words invent
    # nothing: the corroboration needs something to corroborate.
    thin = extract_profile("i make 500k")
    same_words = diagnose(thin, text="i keep overspending on stuff")
    assert same_words.scores["excessive_discretionary_spending"] == 0.0
    assert same_words.primary_problem != "excessive_discretionary_spending"


def test_weekly_income_is_compared_monthly():
    """Frequency normalization keeps the surplus arithmetic honest."""
    profile = extract_profile("i make 2m a week but my essentials are 30m a month")
    diagnosis = diagnose(profile)
    assert "cashflow_imbalance" in diagnosis.problems()
    assert profile.value("income_frequency") == "weekly"


def test_same_life_same_read():
    profile = extract_profile("i make 500k and spend 350k, i never save")
    first = diagnose(profile).to_dict()
    second = diagnose(profile, text="i never save").to_dict()
    assert first["primary_problem"] == second["primary_problem"]
    for result in (diagnose(profile), diagnose(profile, goal_expected=False)):
        assert 0.3 <= result.confidence <= 0.95
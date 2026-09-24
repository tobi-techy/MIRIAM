"""Structured financial profile + deterministic money extraction (spec §6, §7).

Two things must be true for Miriam to reason over money:
  1. Every value carries provenance (value/source/confidence/timestamp) so a
     stale guess can never overwrite what the user actually said.
  2. Free-form money talk normalizes deterministically, so the numbers she
     narrates are computed in one testable place -- never by the LLM.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.financial.profile import (  # noqa: E402
    FinancialProfile,
    detect_currency,
    extract_money_facts,
    extract_profile,
    horizon_months,
    profile_from_onboarding_state,
)

# -----------------------------------------------------------------------
# Extraction: amounts
# -----------------------------------------------------------------------


def test_spec_example_income_and_expenses():
    """The spec's §6 example, end to end."""
    facts = extract_money_facts("I make about 500k every month and spend maybe 350k.")
    assert facts["income_amount"].value == 500_000
    assert facts["income_amount"].currency == "NGN"
    assert facts["income_amount"].confidence >= 0.9
    assert facts["income_frequency"].value == "monthly"
    assert facts["essential_expenses"].value == 350_000


def test_two_amounts_in_one_sentence_route_independently():
    """The nearer cue wins, so neither amount steals the other's field."""
    facts = extract_money_facts("i earn 800000 monthly and my rent is 250000")
    assert facts["income_amount"].value == 800_000
    assert facts["essential_expenses"].value == 250_000


def test_amount_variants_normalize():
    for text, expected in [
        ("i make 500000", 500_000),
        ("i make 500,000", 500_000),
        ("i make 500k", 500_000),
        ("i make 1.5m a year", 1_500_000),
        ("i make 2 million per month", 2_000_000),
        ("i make half a million", 500_000),
        ("i make two hundred thousand", 200_000),
    ]:
        facts = extract_money_facts(text)
        assert facts["income_amount"].value == expected, text


def test_spelled_amount_is_lower_confidence_than_digits():
    """'half a million' is a real answer but a vaguer one -- §6 asks Miriam to
    confirm it rather than treat it as exact."""
    spelled = extract_money_facts("i make half a million")
    digits = extract_money_facts("i make 500k")
    assert spelled["income_amount"].confidence < digits["income_amount"].confidence


def test_year_is_not_an_amount():
    facts = extract_money_facts("i want to go to japan in 2027")
    assert "income_amount" not in facts
    assert "essential_expenses" not in facts


def test_uncued_number_is_not_promoted_to_a_fact():
    """A bare number with no money cue is not a financial fact."""
    facts = extract_money_facts("i have 3 brothers")
    assert "income_amount" not in facts


def test_savings_debt_and_buffer_are_extracted():
    facts = extract_money_facts(
        "i save about 50k a month, i owe 200k on my credit card, "
        "and my emergency fund is 100k"
    )
    assert facts["savings"].value == 50_000
    assert facts["debt"].value == 200_000
    assert facts["emergency_fund"].value == 100_000


# -----------------------------------------------------------------------
# Extraction: currency
# -----------------------------------------------------------------------


def test_currency_detection():
    assert detect_currency("i make 500k naira") == ("NGN", False)
    assert detect_currency("i make $5000") == ("USD", False)
    assert detect_currency("i make \u20a6500k") == ("NGN", False)
    assert detect_currency("i make 500k") == ("NGN", True)


def test_currency_default_is_recorded_as_an_assumption():
    """§10: never present a guess as a figure the user gave."""
    facts = extract_money_facts("i make 500k")
    assert "none named" in facts["income_currency"].note
    assert facts["income_currency"].confidence < 0.9


def test_named_currency_is_high_confidence():
    facts = extract_money_facts("i make $5000 a month")
    assert facts["income_currency"].value == "USD"
    assert facts["income_amount"].currency == "USD"


# -----------------------------------------------------------------------
# Extraction: cadence and qualitative reads
# -----------------------------------------------------------------------


def test_frequency_detection():
    for text, expected in [
        ("i make 500k every month", "monthly"),
        ("i make 500k per month", "monthly"),
        ("i make 500k a week", "weekly"),
        ("i make 500k weekly", "weekly"),
        ("i make 12m a year", "yearly"),
    ]:
        facts = extract_money_facts(text)
        assert facts["income_frequency"].value == expected, text


def test_volatile_income_is_read_from_plain_words():
    for text in (
        "my income is irregular",
        "commissions are lumpy",
        "i freelance",
        "it varies a lot",
    ):
        facts = extract_money_facts(text)
        assert facts["income_volatility"].value == "variable", text


def test_steady_income_is_read_too():
    facts = extract_money_facts("i have a steady salary")
    assert facts["income_volatility"].value == "steady"


def test_experience_horizon_and_behaviors():
    facts = extract_money_facts(
        "i've never invested, i want this for retirement, "
        "and i don't track my spending"
    )
    assert facts["investment_experience"].value == "none"
    assert facts["goal_horizon"].value == "long"
    assert facts["spending_behavior"].value == "untracked"


def test_dependents_are_counted_from_clear_phrases():
    facts = extract_money_facts("i send money home to my parents every month")
    assert facts["financial_dependents"].value == 2


def test_empty_text_yields_nothing():
    assert extract_money_facts("") == {}
    assert extract_money_facts("   ") == {}


# -----------------------------------------------------------------------
# Profile: merge policy (provenance beats recency)
# -----------------------------------------------------------------------


def test_first_write_always_lands():
    profile = FinancialProfile()
    assert profile.set_fact("income_amount", 500_000, source="conversation") is True
    assert profile.money("income_amount") == 500_000


def test_user_correction_beats_a_confident_inference():
    profile = FinancialProfile()
    profile.set_fact("income_amount", 400_000, source="inferred", confidence=0.99)
    profile.set_fact("income_amount", 500_000, source="user_correction", confidence=0.5)
    assert profile.money("income_amount") == 500_000


def test_weaker_source_never_overwrites_a_stronger_one():
    profile = FinancialProfile()
    profile.set_fact("income_amount", 500_000, source="statement", confidence=0.9)
    applied = profile.set_fact("income_amount", 999, source="inferred", confidence=1.0)
    assert applied is False
    assert profile.money("income_amount") == 500_000


def test_higher_confidence_wins_within_the_same_source():
    profile = FinancialProfile()
    profile.set_fact("income_amount", 400_000, source="conversation", confidence=0.5)
    profile.set_fact("income_amount", 500_000, source="conversation", confidence=0.9)
    assert profile.money("income_amount") == 500_000


def test_same_value_refreshes_without_downgrading_provenance():
    """Re-reading the same number must never weaken what we already know."""
    profile = FinancialProfile()
    profile.set_fact("income_amount", 500_000, source="statement", confidence=0.95)
    applied = profile.set_fact(
        "income_amount", "500000", source="conversation", confidence=0.4
    )
    assert applied is False
    fact = profile.fact("income_amount")
    assert fact.source == "statement"
    assert fact.confidence == 0.95
    assert profile.money("income_amount") == 500_000


def test_blank_values_are_never_stored():
    profile = FinancialProfile()
    assert profile.set_fact("income_amount", None) is False
    assert profile.set_fact("income_amount", "") is False
    assert profile.set_fact("income_amount", "   ") is False
    assert profile.fact("income_amount") is None


def test_unknown_field_is_rejected():
    profile = FinancialProfile()
    assert profile.set_fact("not_a_field", 1) is False


def test_missing_reports_only_what_we_lack():
    profile = FinancialProfile()
    profile.set_fact("income_amount", 500_000)
    assert profile.missing(["income_amount", "essential_expenses"]) == [
        "essential_expenses"
    ]


def test_volatile_income_helper():
    profile = FinancialProfile()
    profile.set_fact("income_volatility", "variable")
    assert profile.volatile_income is True
    steady = FinancialProfile()
    steady.set_fact("income_volatility", "steady")
    assert steady.volatile_income is False


def test_currency_defaults_to_ngn():
    assert FinancialProfile().currency() == "NGN"
    profile = FinancialProfile()
    profile.set_fact("income_currency", "usd")
    assert profile.currency() == "USD"


def test_round_trip_preserves_provenance():
    profile = extract_profile("i make 500k a month, i owe 200k")
    restored = FinancialProfile.from_dict(profile.to_dict())
    assert restored.money("income_amount") == 500_000
    source = profile.fact("income_amount")
    assert restored.fact("income_amount").confidence == source.confidence
    assert restored.fact("income_amount").note == source.note


def test_from_dict_is_fail_open():
    """Schema drift in stored memory must never break the conversation."""
    restored = FinancialProfile.from_dict(
        {
            "income_amount": {"value": 500_000},
            "unknown_field": {"value": 1},
            "broken": "not-a-dict",
        }
    )
    assert restored.money("income_amount") == 500_000
    assert restored.fact("unknown_field") is None


def test_merge_folds_a_second_profile_in():
    left = extract_profile("i make 500k a month")
    right = extract_profile("i owe 200k")
    left.merge(right)
    assert left.money("debt") == 200_000
    assert left.money("income_amount") == 500_000


# -----------------------------------------------------------------------
# Bridge: onboarding state -> structured profile
# -----------------------------------------------------------------------


class _FakeState:
    def __init__(self, **kwargs):
        self.learned = kwargs.pop("learned", {})
        self.money_moment = kwargs.pop("money_moment", "")
        self.goal = kwargs.pop("goal", "")
        self.document_summary = kwargs.pop("document_summary", None)
        self.goal_meta = kwargs.pop("goal_meta", {})


def test_bridge_reads_amounts_out_of_free_form_facts():
    state = _FakeState(
        learned={"cashflow": "about 500k every month", "debt": "i owe 200k"},
        money_moment="the month always runs out before payday",
        goal="stop being broke all the time",
    )
    profile = profile_from_onboarding_state(state)
    assert profile.money("income_amount") == 500_000
    assert profile.money("debt") == 200_000
    assert profile.value("financial_goal") == "stop being broke all the time"


def test_bridge_treats_a_statement_as_the_strongest_evidence():
    state = _FakeState(
        learned={"income": "maybe 400k"},
        document_summary="salary income 500000 monthly, rent 250000",
    )
    profile = profile_from_onboarding_state(state)
    fact = profile.fact("income_amount")
    assert fact.source == "statement"
    assert fact.money() == 500_000


def test_bridge_derives_horizon_from_the_target_date():
    state = _FakeState(goal="buy a house", goal_meta={"target_date": "2032"})
    profile = profile_from_onboarding_state(state)
    assert profile.value("goal_horizon") == "long"


def test_bridge_is_safe_on_an_empty_state():
    assert profile_from_onboarding_state(_FakeState()).known() == {}


def test_bridge_is_safe_on_a_foreign_object():
    """A test double with no attributes at all must not raise."""

    class _Nothing:
        pass

    assert profile_from_onboarding_state(_Nothing()).known() == {}


def test_horizon_months_maps_buckets():
    profile = FinancialProfile()
    assert horizon_months(profile) is None
    profile.set_fact("goal_horizon", "medium")
    assert horizon_months(profile) == 24

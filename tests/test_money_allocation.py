"""The 70/30 override table, and the capacity cap that overrides appetite.

Each row of ``R-BACH-2`` gets its own case so the precedence is pinned: horizon
first, capacity last, and the low-capacity cap has the final word.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

import pytest

from miriam_agent.money import assess_safety, build_book, from_mapping, lookup
from miriam_agent.money.allocation import score_capacity


def intake(**overrides):
    base = {
        "country": "GB",
        "currency": "GBP",
        "income_amount": "5000",
        "income_frequency": "monthly",
        "income_volatility": "steady",
        "fixed_costs": "2000",
        "variable_spend": "500",
        "cash_on_hand": "10000",
        "job_stability": "stable",
        "dependents": 0,
    }
    base.update(overrides)
    return from_mapping(base)


def book_for(**overrides):
    profile = intake(**overrides)
    reference, _provenance = lookup(profile.country)
    stack = assess_safety(profile, reference)
    return build_book(profile, stack, reference), stack


# ---------------------------------------------------------------------------
# Horizon rules
# ---------------------------------------------------------------------------


def test_horizon_under_three_years_is_cash_like_not_a_book():
    """Short horizon is *not* gated: the user may invest, the date just forbids it.

    The distinction matters because the money still exists -- it belongs in
    savings, not in a market sleeve.
    """
    book, stack = book_for(goals=[{"label": "deposit", "horizon_months": 24}])
    assert stack.investing_allowed is True
    assert book.gated is False
    assert book.short_horizon is True
    assert book.growth_pct == 0
    assert book.investable_surplus == 0
    assert book.growth_sleeve == []
    assert book.rule_id == "R-BACH-2:short-horizon"


def test_three_to_five_years_is_fifty_fifty():
    book, _ = book_for(goals=[{"label": "deposit", "horizon_months": 48}])
    assert book.growth_pct == Decimal("50")
    assert book.defensive_pct == Decimal("50")


def test_five_to_seven_years_is_sixty_forty():
    book, _ = book_for(goals=[{"label": "deposit", "horizon_months": 72}])
    assert book.growth_pct == Decimal("60")
    assert book.defensive_pct == Decimal("40")


def test_seven_plus_years_with_steady_income_is_the_seventy_thirty_default():
    book, _ = book_for(goals=[{"label": "retirement", "horizon_months": 120}])
    assert book.growth_pct == Decimal("70")
    assert book.defensive_pct == Decimal("30")
    assert book.rule_id == "R-BACH-2:7y-default"
    assert book.overrides == []


def test_volatile_income_holds_seven_plus_years_at_sixty():
    """A variable pay means the buffer carries more, so the book carries less."""
    book, _ = book_for(
        income_volatility="variable",
        goals=[{"label": "retirement", "horizon_months": 120}],
    )
    assert book.growth_pct == Decimal("60")
    assert any("volatile income" in o for o in book.overrides)


def test_unknown_horizon_is_conservative():
    book, _ = book_for()
    assert book.growth_pct == Decimal("40")
    assert book.rule_id == "R-BACH-2:no-horizon"
    assert any("horizon unknown" in o for o in book.overrides)


# ---------------------------------------------------------------------------
# Long horizon and the 80/20 ceiling
# ---------------------------------------------------------------------------


def test_fifteen_plus_years_with_high_capacity_and_acceptance_reaches_eighty_twenty():
    book, _ = book_for(
        goals=[{"label": "retirement", "horizon_months": 300}],
        accepts_drawdown=True,
    )
    assert book.growth_pct == Decimal("80")
    assert book.rule_id == "R-BACH-2:15y-high-capacity"
    assert any("drawdown acceptance" in o for o in book.overrides)


def test_eighty_twenty_requires_the_acceptance_not_just_capacity():
    """A long horizon and high capacity alone are not enough."""
    book, _ = book_for(goals=[{"label": "retirement", "horizon_months": 300}])
    assert book.growth_pct == Decimal("70")
    assert any("drawdown" in o for o in book.overrides)


# ---------------------------------------------------------------------------
# Capacity gets the last word
# ---------------------------------------------------------------------------


def test_low_capacity_caps_growth_even_when_the_horizon_would_allow_more():
    book, _ = book_for(
        income_volatility="variable",
        job_stability="unstable",
        dependents=2,
        goals=[{"label": "retirement", "horizon_months": 300}],
    )
    assert book.growth_pct == Decimal("50"), book.overrides
    assert any("caps growth at 50%" in o for o in book.overrides)


def test_stated_appetite_does_not_raise_the_cap():
    """Appetite is an input to the conversation, not to the arithmetic."""
    cautious, _ = book_for(
        income_volatility="variable",
        job_stability="unstable",
        dependents=2,
        risk_tolerance="low",
        goals=[{"label": "retirement", "horizon_months": 300}],
    )
    aggressive, _ = book_for(
        income_volatility="variable",
        job_stability="unstable",
        dependents=2,
        risk_tolerance="high",
        goals=[{"label": "retirement", "horizon_months": 300}],
    )
    assert cautious.growth_pct == aggressive.growth_pct


def test_capacity_scores_stability_dependents_and_income():
    high = score_capacity(
        intake(
            goals=[{"label": "retirement", "horizon_months": 300}],
            job_stability="stable",
            dependents=0,
        )
    )
    low = score_capacity(
        intake(
            goals=[{"label": "soon", "horizon_months": 24}],
            job_stability="unstable",
            dependents=3,
        )
    )
    assert high.band == "high"
    assert low.band == "low"


def test_a_long_horizon_cannot_mask_a_fragile_month():
    """Time to recover does not help if next month is the problem."""
    fragile = score_capacity(
        intake(
            income_volatility="variable",
            job_stability="unstable",
            dependents=2,
            goals=[{"label": "retirement", "horizon_months": 300}],
        )
    )
    assert fragile.band == "low"
    assert any("fragile month" in factor for factor in fragile.factors)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_a_gated_stack_produces_no_book_at_all():
    book, stack = book_for(cash_on_hand="0")
    assert stack.investing_allowed is False
    assert book.gated is True
    assert book.growth_pct == 0
    assert book.investable_surplus == 0
    assert book.growth_sleeve == []


def test_fire_debt_gates_the_book_even_with_a_full_buffer():
    book, stack = book_for(
        cash_on_hand="30000",
        debts=[
            {
                "label": "card",
                "balance": "2000",
                "apr_pct": "29",
                "minimum_monthly": "60",
            }
        ],
    )
    assert stack.fire_debt is True
    assert stack.investing_allowed is False
    assert book.gated is True


def test_unknown_apr_gates_instead_of_being_assumed_cheap():
    """An unrecorded rate is not the same as a cheap one."""
    book, stack = book_for(
        cash_on_hand="30000",
        debts=[{"label": "loan", "balance": "2000", "minimum_monthly": "60"}],
    )
    assert stack.unknown_apr_debt is True
    assert stack.investing_allowed is False
    assert book.gated is True
    assert any("APR" in reason for reason in stack.blocked_reasons)


# ---------------------------------------------------------------------------
# Sleeves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("horizon", (48, 72, 120, 300))
def test_growth_sleeve_is_broad_and_never_a_single_name(horizon):
    book, _ = book_for(goals=[{"label": "goal", "horizon_months": horizon}])
    assert book.growth_sleeve
    assert any("Broad, low-cost equity index" in item for item in book.growth_sleeve)


def test_defensive_sleeve_refuses_to_call_a_volatile_token_defensive():
    book, _ = book_for(goals=[{"label": "goal", "horizon_months": 120}])
    joined = " ".join(book.defensive_sleeve).casefold()
    assert "never part of this sleeve" in joined


def test_weights_always_sum_to_one_hundred():
    for horizon in (48, 72, 120, 300):
        book, _ = book_for(goals=[{"label": "goal", "horizon_months": horizon}])
        if book.growth_pct == 0:
            continue
        assert book.growth_pct + book.defensive_pct == Decimal("100")

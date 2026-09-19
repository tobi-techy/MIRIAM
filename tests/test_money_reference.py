"""Reference data honesty: dated, labelled, and fail-closed when stale.

The local inflation and rate figures are the one place a wrong number would not
look like a bug. These tests pin the three mechanisms that keep it honest.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

import pytest

from miriam_agent.money import assess_safety, from_mapping, lookup, resolve_thresholds
from miriam_agent.money.reference import (
    MAX_AGE_DAYS,
    REFERENCE_AS_OF,
    CountryReference,
    available_countries,
    reference_status,
)

CURATED = available_countries()

# A date comfortably past the staleness window, for the fail-closed tests.
STALE_DAY = date.fromisoformat(REFERENCE_AS_OF) + timedelta(days=MAX_AGE_DAYS + 10)


def _intake(**overrides):
    base = {
        "country": "US",
        "currency": "USD",
        "income_amount": "9000",
        "fixed_costs": "4200",
        "variable_spend": "1600",
        "cash_on_hand": "38000",
        "job_stability": "stable",
        "dependents": 0,
        "goals": [{"label": "retirement", "horizon_months": 120}],
    }
    base.update(overrides)
    return from_mapping(base)


# ---------------------------------------------------------------------------
# Dated and labelled
# ---------------------------------------------------------------------------


def test_every_row_is_dated():
    for code in CURATED:
        ref, _status = lookup(code)
        assert ref.as_of, f"{code} has no as-of date"
        assert date.fromisoformat(ref.as_of), f"{code} as-of is not a date"


def test_unsourced_rows_are_labelled_placeholder_not_official():
    """A placeholder may not read like a sourced feed."""
    for code in CURATED:
        ref, status = lookup(code)
        if ref.sourced:
            continue
        assert "PLACEHOLDER" in status.note, f"{code} hides its provenance"
        assert "not a live feed" in status.note or "verify" in status.note


def test_nigeria_and_the_us_carry_the_full_set():
    """The two countries the brief requires: inflation, rate, fire threshold."""
    for code in ("NG", "US"):
        ref, _status = lookup(code)
        assert ref.inflation_pct > 0
        assert ref.risk_free_rate_pct > 0
        assert ref.fire_apr_pct is not None
        assert ref.judgment_apr_pct is not None
        assert ref.safety_vehicles, f"{code} has nowhere safe to park a buffer"


def test_high_rate_currency_has_a_higher_fire_threshold():
    """A punitive rate in a high-rate currency is not the same number."""
    ng, _ = lookup("NG")
    us, _ = lookup("US")
    assert ng.fire_apr_pct > us.fire_apr_pct


# ---------------------------------------------------------------------------
# Staleness fails closed
# ---------------------------------------------------------------------------


def test_a_fresh_row_is_not_stale():
    _ref, status = lookup("NG", today=date.fromisoformat(REFERENCE_AS_OF))
    assert status.stale is False
    assert status.age_days == 0
    assert status.trusted is True


def test_a_row_past_ninety_days_is_stale_and_says_so():
    future = date.fromisoformat(REFERENCE_AS_OF) + timedelta(days=MAX_AGE_DAYS + 1)
    _ref, status = lookup("NG", today=future)
    assert status.stale is True
    assert status.trusted is False
    assert "treated as unknown" in status.note
    assert "investing is refused" in status.note


def test_stale_reference_fails_closed_and_blocks_investing():
    """Local rates we no longer stand behind cannot drive an investing decision."""
    profile = _intake(country="US", currency="USD")
    ref, status = lookup("US", today=STALE_DAY)
    stack = assess_safety(profile, ref, status=status)

    assert status.stale is True
    assert stack.investing_allowed is False
    assert any("out of date" in reason for reason in stack.blocked_reasons)


def test_a_fresh_reference_does_not_block_investing():
    profile = _intake()
    ref, status = lookup("US", today=date.fromisoformat(REFERENCE_AS_OF))
    stack = assess_safety(profile, ref, status=status)
    assert status.stale is False
    assert stack.investing_allowed is True


def test_stale_reference_downgrades_the_plan_confidence():
    from miriam_agent.money import build_money_plan

    plan = build_money_plan(_intake(), today=STALE_DAY)
    assert plan.confidence == "low"
    assert plan.is_investing() is False


# ---------------------------------------------------------------------------
# Unknown country
# ---------------------------------------------------------------------------


def test_an_unknown_country_is_flagged_as_unmatched():
    _ref, status = lookup("ZZ")
    assert status.matched is False
    assert status.stale is False
    assert "no local reference" in status.note


def test_an_unknown_country_falls_back_to_settings_thresholds():
    """A country we know nothing about should not get a confident local band."""
    ref, _status = lookup("ZZ")
    assert ref.fire_apr_pct is None and ref.judgment_apr_pct is None
    fire, judgment = resolve_thresholds(
        ref, fire_default=Decimal("15"), judgment_default=Decimal("8")
    )
    assert fire == Decimal("15")
    assert judgment == Decimal("8")


def test_a_known_country_overrides_the_settings_thresholds():
    ref, _status = lookup("NG")
    fire, judgment = resolve_thresholds(
        ref, fire_default=Decimal("15"), judgment_default=Decimal("8")
    )
    assert fire == Decimal("25")
    assert judgment == Decimal("10")


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def test_supplied_rates_bypass_the_table_and_say_so():
    supplied = CountryReference(
        country_code="NG",
        currency="NGN",
        inflation_pct=Decimal("30"),
        risk_free_rate_pct=Decimal("22"),
        sourced=True,
        source_note="central bank print",
    )
    ref, status = lookup("NG", overrides=supplied)
    assert ref.inflation_pct == Decimal("30")
    assert status.trusted is True
    assert "supplied directly" in status.note


def test_reference_status_survives_a_bad_date():
    broken = CountryReference(
        country_code="US",
        currency="USD",
        as_of="not-a-date",
        inflation_pct=Decimal("3"),
        risk_free_rate_pct=Decimal("4"),
    )
    status = reference_status(broken)
    assert status.stale is True
    assert status.trusted is False


@pytest.mark.parametrize("code", CURATED)
def test_every_row_has_an_erosion_figure(code):
    ref, _status = lookup(code)
    assert ref.annual_erosion_pct() >= 0

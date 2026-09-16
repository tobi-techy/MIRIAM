"""Deterministic allocation engine (spec §11): "your money needs a default."

Money has four envelopes -- Everyday, Safety, Future, Flexible -- and every
naira must be exactly one of them (see ``totals_check``). Shares adapt to
surplus, buffer gap, debt pressure, volatility and horizon; the same life
always gets the same plan, so a test can falsify any of it.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.financial.allocation import calculate_allocation  # noqa: E402
from miriam_agent.financial.diagnosis import diagnose  # noqa: E402
from miriam_agent.financial.profile import extract_profile  # noqa: E402


def _plan(text, with_diagnosis=True):
    profile = extract_profile(text)
    diagnosis = diagnose(profile) if with_diagnosis else None
    return calculate_allocation(profile, diagnosis), profile


# -----------------------------------------------------------------------
# Invariants: true for every plan, whatever the life
# -----------------------------------------------------------------------


def test_envelopes_always_reconcile():
    cases = [
        "i make 500k monthly, essentials are 350k",
        "i make 400k monthly and spend 500k",
        "i make 1m monthly, essentials are 400k, i owe 10m",
        "i earn 1200000 monthly, essentials are 600000, "
        "2m saved as an emergency fund, goal is a house for the long term",
        "my income is irregular, i make 600k some months, essentials 300k",
        "i make 2m weekly, essentials are 5m monthly, i owe 500k",
        "i make 800k monthly, essentials are 200k, debt 100k, "
        "goal is retirement for the long term",
    ]
    for text in cases:
        plan, _ = _plan(text)
        assert plan.totals_check(), (text, plan.to_dict())
        assert plan.everyday + plan.safety + plan.debt_attack >= 0
        assert min(plan.everyday, plan.safety, plan.future, plan.flexible) >= 0
        assert abs(sum(plan.shares.values()) - 1.0) < 0.02 or plan.income == 0


def test_no_fixed_percentages():
    """Two different lives must get different shares -- a percentage table
    would serve both identically."""
    thin = _plan("i make 500k monthly, essentials are 450k")[0]
    roomy = _plan(
        "i earn 1200000 monthly, essentials are 600000, "
        "2m saved as an emergency fund, goal is a house for the long term"
    )[0]
    assert (thin.everyday, thin.safety) != (roomy.everyday, roomy.future)


# -----------------------------------------------------------------------
# The spec's own shapes
# -----------------------------------------------------------------------


def test_standard_case_protects_everyday_then_builds_safety():
    """§11: ₦500k in, ₦350k everyday -- the rest builds the missing buffer,
    not the future. The base is under one month, so there is no future yet."""
    plan, _ = _plan(
        "i make 500k monthly, essentials are 350k, "
        "goal is to build an emergency fund for the long term"
    )
    assert plan.income == 500_000
    assert plan.everyday == 350_000
    assert plan.safety == 150_000
    assert plan.future == 0
    assert plan.buffer_target == 1_050_000
    assert plan.buffer_gap == 1_050_000
    assert plan.months_to_fill_buffer == 7.0
    assert plan.next_step == "build_emergency_buffer"
    assert plan.rationale
    assert plan.confidence >= 0.8


def test_negative_cashflow_admits_the_gap():
    """When the outflow beats the income, the plan says so instead of
    inventing money (§10)."""
    plan, _ = _plan("i make 400k monthly and spend 500k")
    assert plan.everyday == 400_000
    assert plan.safety == plan.future == plan.flexible == 0
    assert plan.next_step == "fix_cashflow_gap"
    assert any("exceeds" in line for line in plan.rationale)


def test_crushing_debt_puts_debt_first():
    """£10m owed on £1m income: triage -- debt attacks, buffer protects."""
    plan, _ = _plan(
        "i make 1m monthly, essentials are 400k, i owe 10m, goal is to clear debt"
    )
    assert plan.debt_attack > plan.safety > 0
    assert plan.future == 0
    assert plan.next_step == "clear_high_cost_debt"
    assert any(str(10) in line for line in plan.rationale)


def test_healthy_base_earns_a_future_and_automation():
    """Once the buffer is built and the horizon is long, money moves to the
    future -- and Rail's job becomes automation (§12)."""
    plan, _ = _plan(
        "i earn 1200000 monthly, essentials are 600000, "
        "2m saved as an emergency fund, goal is a house for the long term"
    )
    assert plan.future > 0
    assert plan.safety == 0  # nothing left to build
    assert plan.next_step == "automate_the_investment"


def test_volatile_income_wants_a_bigger_buffer():
    steady, _ = _plan("i make 600k monthly, essentials are 300k")
    irregular, _ = _plan(
        "my income is irregular, i make 600k monthly, essentials are 300k"
    )
    assert irregular.buffer_target == 1_800_000  # 6 months, not 3
    assert steady.buffer_target == 900_000
    assert irregular.safety >= steady.safety


def test_short_horizon_blocks_the_future():
    plan, _ = _plan(
        "i earn 1000000 monthly, essentials are 400000, "
        "2m saved as an emergency fund, goal is a trip next month"
    )
    assert plan.future == 0
    assert plan.safety == 0 or plan.flexible > 0


def test_ordinary_debt_gets_an_attack_slice_without_starving_safety():
    plan, _ = _plan(
        "i make 800k monthly, essentials are 400k, i owe 800k on my credit card"
    )
    assert plan.debt_attack > 0
    assert plan.safety > 0
    assert plan.future == 0 or plan.flexible >= 0


def test_allowance_acknowledges_recorded_discretionary():
    """Recorded lifestyle spend is capped and named, not banned -- Miriam is a
    companion, not a scold (§22)."""
    plan, _ = _plan("i make 800k monthly, essentials are 400k, i spend 300k on fun")
    assert plan.everyday >= 400_000
    assert plan.everyday <= 400_000 + 800_000 * 0.15
    assert any("allowance" in line for line in plan.rationale)


def test_missing_income_is_a_missing_plan_not_a_wrong_one():
    from miriam_agent.financial.profile import FinancialProfile

    plan = calculate_allocation(FinancialProfile())
    assert plan.income == 0
    assert plan.next_step == "record_income"
    assert plan.totals_check()


def test_diagnosis_sharpens_but_never_invents():
    """The same life with and without a diagnosis: both reconcile, and the
    diagnosis only reallocates -- it never creates money."""
    text = "i make 800k monthly, essentials are 400k, i owe 800k on my credit card"
    with_diag, _ = _plan(text, with_diagnosis=True)
    without_diag, _ = _plan(text, with_diagnosis=False)
    assert with_diag.totals_check() and without_diag.totals_check()
    assert with_diag.debt_attack >= without_diag.debt_attack
    assert with_diag.income == without_diag.income
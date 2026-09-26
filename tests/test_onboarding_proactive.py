"""Regression tests for the proactive savings-plan wiring in onboarding."""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

def test_readiness_needs_income_and_fixed():
    from miriam_agent.onboarding.money_bridge import money_readiness
    from miriam_agent.onboarding.state import OnboardingState
    ready, missing = money_readiness(OnboardingState({}))
    assert ready is False
    assert "income_amount" in missing
    partial = OnboardingState({"learned": {"income": "I earn 500k every month"}, "interview_turns": 1})
    ready, missing = money_readiness(partial)
    assert ready is False and "fixed_costs" in missing
    full = OnboardingState({"learned": {"income": "I earn 500k every month", "fixed": "rent and food take 350k"}, "goal": "build buffer", "interview_turns": 2})
    ready, missing = money_readiness(full)
    assert ready is True and missing == []

def test_gap_question_is_single():
    from miriam_agent.onboarding.money_bridge import gap_question, PLAN_CTA_TAPS
    q = gap_question(["income_amount"])
    assert q.count("?") == 1 and "month" in q.lower()
    assert "Build my savings plan" in PLAN_CTA_TAPS

def test_money_plan_builds_real_amounts():
    from miriam_agent.onboarding.money_bridge import build_money_plan_dict
    from miriam_agent.onboarding.state import OnboardingState
    state = OnboardingState({"learned": {"income": "I earn 500k every month", "fixed": "rent and food take 350k"}, "goal": "build buffer", "interview_turns": 4})
    plan = build_money_plan_dict(state)
    assert plan is not None
    assert plan["cashflow"]["fixed"] != "0"
    assert plan["cashflow"]["savings"] != "0"
    assert plan["currency"] == "NGN"
    assert plan["actions_90d"] and plan["automation_rules"] and plan["disclaimer"]

def test_money_plan_text_carries_amounts():
    from miriam_agent.onboarding.money_bridge import build_money_plan_dict, render_money_plan_text
    from miriam_agent.onboarding.state import OnboardingState
    state = OnboardingState({"learned": {"income": "I earn 500k every month", "fixed": "rent and food take 350k"}, "goal": "build buffer", "interview_turns": 4})
    text = render_money_plan_text(build_money_plan_dict(state))
    assert "350" in text and "150" in text and "lock this in" in text.lower()

def test_state_migration_v2_to_v3():
    from miriam_agent.onboarding.state import OnboardingState, _migrate
    migrated = _migrate({"schema_version": 2, "stage": "interview"})
    assert migrated["schema_version"] == 4
    assert migrated["money_plan"] is None and migrated["money_gap"] == [] and migrated["money_ready"] is False
    assert migrated["asked_gaps"] == [] and migrated["last_poll_options"] == []
    assert OnboardingState(migrated).money_plan is None

def test_reference_env_override_is_sourced():
    import os
    os.environ["MONEY_REF_COUNTRY"] = "NG"
    os.environ["MONEY_REF_CURRENCY"] = "NGN"
    os.environ["MONEY_REF_INFLATION_PCT"] = "24.0"
    os.environ["MONEY_REF_RISK_FREE_PCT"] = "19.0"
    os.environ["MONEY_REF_AS_OF"] = "2026-09-24"
    try:
        from miriam_agent.money import reference_from_env
        ref = reference_from_env()
        assert ref is not None and ref.sourced is True
        assert ref.country_code == "NG"
    finally:
        for k in ("MONEY_REF_COUNTRY","MONEY_REF_CURRENCY","MONEY_REF_INFLATION_PCT","MONEY_REF_RISK_FREE_PCT","MONEY_REF_AS_OF"):
            os.environ.pop(k, None)

def test_reference_env_absent_returns_none():
    import os
    for k in ("MONEY_REF_INFLATION_PCT","MONEY_REF_RISK_FREE_PCT"):
        os.environ.pop(k, None)
    from miriam_agent.money import reference_from_env
    assert reference_from_env() is None

def test_stale_reference_blocks_investing_not_savings():
    from datetime import date, timedelta
    from miriam_agent.money import assess_safety, from_mapping, lookup
    from miriam_agent.money.reference import MAX_AGE_DAYS, REFERENCE_AS_OF
    stale = date.fromisoformat(REFERENCE_AS_OF) + timedelta(days=MAX_AGE_DAYS + 10)
    profile = from_mapping({"country":"NG","currency":"NGN","income_amount":"500000","fixed_costs":"350000"})
    ref, status = lookup("NG", today=stale)
    stack = assess_safety(profile, ref, status=status)
    assert status.stale is True and stack.investing_allowed is False
    assert any("savings split" in r for r in stack.blocked_reasons)
    assert stack.buffer_contribution > 0  # savings still computed

def test_completion_receipt_carries_amounts():
    from miriam_agent.onboarding.completion import automated_completion_text, resume_payload
    from miriam_agent.onboarding.money_bridge import build_money_plan_dict
    from miriam_agent.onboarding.state import OnboardingState
    s = OnboardingState({"learned": {"income": "I earn 500k every month", "fixed": "rent and food take 350k"}, "goal": "build buffer", "interview_turns": 4, "stage": "plan_consent"})
    s.money_plan = build_money_plan_dict(s)
    receipt = automated_completion_text(s)
    assert "350" in receipt and "150" in receipt
    payload = resume_payload(s)
    assert payload["has_money_plan"] is True
    assert "lock it in" in payload["next_recommended_step"]

def test_intake_derived_reads_are_properties():
    from miriam_agent.money import from_mapping
    p = from_mapping({"income_amount":"500000","fixed_costs":"350000"})
    assert not callable(p.monthly_income) and p.monthly_income is not None
    assert not callable(p.missing_required) and p.missing_required == []

def test_salary_and_numbered_pay_rhythm_are_understood():
    from miriam_agent.onboarding.money_bridge import (
        CADENCE_TAPS,
        absorb_reply,
        gap_question,
        gap_taps,
        money_readiness,
    )
    from miriam_agent.onboarding.state import OnboardingState

    income_q = gap_question(["income_amount"])
    assert income_q.count("?") == 1
    assert len(income_q) <= 60
    assert gap_taps(["income_amount"]) == ()

    state = OnboardingState()
    state.stage = "interview"
    absorb_reply(state, "Between $50 or less", poll_title=income_q)
    ready, missing = money_readiness(state)
    assert ready is False
    assert "income_amount" not in missing
    assert "50" in state.learned["income"]

    cadence_q = gap_question(["income_frequency"])
    assert len(cadence_q) <= 60
    assert gap_taps(["income_frequency"]) == CADENCE_TAPS
    state.last_poll_options = list(CADENCE_TAPS)
    absorb_reply(state, "1", poll_title=cadence_q)
    assert state.learned["pay_rhythm"] == "weekly"
    assert "income_frequency" not in money_readiness(state)[1]

    absorb_reply(state, "rent is $20", poll_title=gap_question(["fixed_costs"]))
    ready, missing = money_readiness(state)
    assert ready is True and missing == []


def test_zero_invest_slice_does_not_pretend_to_buy():
    from miriam_agent.onboarding.completion import automated_completion_text
    from miriam_agent.onboarding.money_bridge import render_money_plan_text
    from miriam_agent.onboarding.state import OnboardingState

    plan = {
        "currency": "USD",
        "cashflow": {"fixed": 0, "debt": 0, "savings": 0, "investments": 0, "guilt_free": 50},
        "automation_rules": ["Automate the transfers themselves, not the intention to make them."],
        "disclaimer": "",
    }
    state = OnboardingState({"learned": {"income": "$50"}, "money_plan": plan})
    text = automated_completion_text(state)
    assert "placeholder" in text.lower()
    assert "buys the Rail Stock Sleeve" not in text
    spoken = render_money_plan_text(plan)
    assert "Nothing goes to stocks yet" in spoken


def test_plan_text_names_the_stock_sleeve_only_when_it_is_funded():
    from miriam_agent.onboarding.money_bridge import build_money_plan_dict, render_money_plan_text
    from miriam_agent.onboarding.state import OnboardingState

    state = OnboardingState({"learned": {"income": "I earn 500k every month", "fixed": "rent and food take 350k"}, "goal": "build buffer", "interview_turns": 4})
    text = render_money_plan_text(build_money_plan_dict(state))
    assert "Nothing goes to stocks yet" in text
    funded = render_money_plan_text({"currency": "USD", "cashflow": {"fixed": 20, "debt": 0, "savings": 10, "investments": 15, "guilt_free": 5}, "disclaimer": ""})
    assert "Rail Stock Sleeve" in funded and "Apple" in funded and "Tesla" in funded


def test_same_gap_is_not_a_reason_to_keep_polling():
    from miriam_agent.onboarding.money_bridge import cap_should_present, mark_gap_asked
    from miriam_agent.onboarding.state import OnboardingState

    state = OnboardingState({"money_moment": "money vanishes", "interview_turns": 6})
    assert cap_should_present(state, "Investment") is False
    mark_gap_asked(state, "income_amount")
    mark_gap_asked(state, "income_frequency")
    mark_gap_asked(state, "fixed_costs")
    assert cap_should_present(state, "Investment") is True


def test_naija_flavor_in_deterministic_copy():
    from miriam_agent.onboarding.completion import automated_completion_text, draft_completion_text
    from miriam_agent.onboarding.money_bridge import build_money_plan_dict, gap_question, render_money_plan_text
    from miriam_agent.onboarding.state import OnboardingState
    state = OnboardingState({"learned": {"income": "I earn 500k every month", "fixed": "rent and food take 350k"}, "goal": "build buffer", "interview_turns": 4})
    state.money_plan = build_money_plan_dict(state)
    assert "e don set" in automated_completion_text(state).lower()
    assert "no wahala" in draft_completion_text().lower()
    assert "no wahala" in gap_question(["income_amount"]).lower()
    assert "sharp sharp" in render_money_plan_text(state.money_plan).lower()

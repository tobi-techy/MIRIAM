"""Money pipeline fixtures, end to end. These are the merge gates.

The headline assertion is the ordering invariant: **no fixture may receive an
investment recommendation before the buffer and debt rules have fired.** If that
test passes while a gated fixture has a growth sleeve or a Glider action, the
build is wrong.

Everything here is deterministic and offline: no network, no LLM, no database.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

import pytest

from miriam_agent.money import (
    GliderState,
    build_money_plan,
    explain_money_plan,
    from_mapping,
    render_plan,
    render_spoken,
)
from miriam_agent.money.schema import MoneyPlan
from miriam_agent.money.text import has_em_dash, scrub_voice

FIXTURES = Path(__file__).parent / "fixtures" / "money"

# Every fixture, so invariants are asserted across the whole set rather than on
# whichever one a test author happened to remember.
ALL_FIXTURES = (
    "lagos_freelancer",
    "bleed",
    "invest_it_all",
    "missing_income",
    "us_w2_idle_cash",
    "crypto_everything",
    "fragile_long_horizon",
    "glider_drifted",
)

# The merge gates: which fixtures must refuse to invest, and which may not.
MUST_NOT_INVEST = (
    "lagos_freelancer",
    "bleed",
    "invest_it_all",
    "missing_income",
)


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _glider_state(fixture: dict) -> GliderState | None:
    if fixture.get("portfolio") or fixture.get("positions"):
        return GliderState(
            portfolio=fixture.get("portfolio"),
            positions=fixture.get("positions") or [],
        )
    return None


def run(name: str):
    """The fixture, its reasoning trace, and its plan."""
    fixture = load(name)
    state = _glider_state(fixture)
    pipeline = explain_money_plan(fixture["intake"], glider_state=state)
    plan = build_money_plan(fixture["intake"], glider_state=state)
    return fixture, pipeline, plan


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_gated_plan_never_recommends_an_investment(name):
    """When the safety stack refuses there is no book and no Glider action."""
    _fixture, pipeline, plan = run(name)
    if pipeline.safety.investing_allowed:
        pytest.skip("fixture is not gated; the gated invariant does not apply")

    assert plan.surplus_monthly == 0, f"{name} has investable surplus while gated"
    assert plan.book.growth_pct == 0, f"{name} carries a growth sleeve while gated"
    assert plan.book.growth_sleeve == [], f"{name} names growth instruments"
    assert plan.glider.kind == "none", f"{name} has a Glider action while gated"
    assert plan.glider.draft is None
    assert plan.is_investing() is False


@pytest.mark.parametrize("name", MUST_NOT_INVEST)
def test_merge_gate_cannot_invest(name):
    """The four fixtures that must never reach investing, with a stated reason."""
    _fixture, pipeline, plan = run(name)
    assert pipeline.safety.investing_allowed is False
    assert plan.book.gated is True
    assert plan.book.growth_sleeve == []
    assert plan.glider.kind == "none"
    assert plan.glider.blocked_reason, "a refusal must say why"


def test_merge_gate_us_surplus_reaches_a_seventy_thirty_draft():
    _fixture, pipeline, plan = run("us_w2_idle_cash")
    assert pipeline.safety.investing_allowed is True
    assert plan.book.growth_pct == Decimal("70")
    assert plan.surplus_monthly > 0
    assert plan.glider.kind == "draft"
    assert plan.glider.draft is not None
    assert plan.glider.draft.submitted is False, "a draft is never submitted"


def test_merge_gate_fragile_capacity_is_capped():
    _fixture, pipeline, plan = run("fragile_long_horizon")
    assert pipeline.safety.investing_allowed is True
    assert plan.book.growth_pct == Decimal("50")
    assert any("caps growth" in o for o in plan.book.overrides)


def test_merge_gate_drifted_portfolio_is_monitored():
    _fixture, _pipeline, plan = run("glider_drifted")
    assert plan.glider.kind == "monitor"
    assert plan.glider.portfolio_id == "a1b2c3d4"
    assert (
        plan.glider.draft is None
    ), "an existing portfolio gets monitored, not replaced"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_plan_always_carries_a_disclaimer(name):
    _fixture, _pipeline, plan = run(name)
    assert plan.disclaimer
    assert "not a licensed financial adviser" in plan.disclaimer.lower()


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_cashflow_reconciles_to_take_home(name):
    """Money is accounted for: the split adds back to what came in."""
    _fixture, _pipeline, plan = run(name)
    assert plan.cashflow.total() == plan.monthly_take_home
    assert set(plan.cashflow.shares) == {
        "fixed",
        "debt",
        "savings",
        "investments",
        "guilt_free",
    }


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_plan_has_kill_switches_and_a_change_list(name):
    _fixture, _pipeline, plan = run(name)
    assert plan.kill_switches
    assert plan.what_would_change


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_em_dash_reaches_the_user(name):
    """House rule: no em dashes, in the structured plan or the spoken form."""
    _fixture, _pipeline, plan = run(name)
    assert not has_em_dash(render_plan(plan))
    assert not has_em_dash(render_spoken(plan))
    assert not has_em_dash(plan.diagnosis)
    for note in plan.assumptions + plan.automation_rules + plan.kill_switches:
        assert not has_em_dash(note), note


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_spoken_form_stays_short(name):
    """Chat default is the diagnosis plus one move, inside 15-60 words."""
    _fixture, _pipeline, plan = run(name)
    words = render_spoken(plan).split()
    assert 5 <= len(words) <= 60, f"{name} spoke {len(words)} words"
    assert "Next:" in render_spoken(plan)


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_plan_renders_every_section(name):
    _fixture, _pipeline, plan = run(name)
    text = render_plan(plan)
    for section in (
        "1. DIAGNOSIS",
        "4. CASHFLOW SPLIT",
        "7. INVESTABLE SURPLUS AND BOOK",
        "8. GLIDER ACTION",
        "12. WHAT WOULD CHANGE THIS PLAN",
    ):
        assert section in text


def test_scrub_voice_replaces_dashes():
    assert scrub_voice("a \u2014 b") == "a - b"
    assert scrub_voice("\u201cquoted\u201d") == '"quoted"'
    assert scrub_voice("wait\u2026 and more") == "wait... and more"


# ---------------------------------------------------------------------------
# Lagos: the case the build is judged on
# ---------------------------------------------------------------------------


def test_lagos_diagnosis_is_blunt_and_true():
    _fixture, pipeline, plan = run("lagos_freelancer")
    assert pipeline.diagnosis.problem_type == "no_buffer"
    assert plan.problem_type == "no_buffer"
    # The fire is named in the same breath, because it is the fastest-moving
    # number in the plan.
    assert "35.0% APR" in pipeline.diagnosis.blunt
    assert "fire" in pipeline.diagnosis.blunt


def test_lagos_buffer_targets_one_month_because_debt_is_on_fire():
    _fixture, pipeline, _plan = run("lagos_freelancer")
    assert pipeline.safety.fire_debt is True
    assert pipeline.safety.buffer_target_months == Decimal("1")


def test_lagos_fire_debt_gets_an_attack_in_parallel_with_the_buffer():
    """A year-long buffer build while 35% interest compounds is the wrong trade."""
    _fixture, pipeline, plan = run("lagos_freelancer")
    assert pipeline.safety.buffer_contribution > 0
    assert pipeline.safety.debt_extra > 0
    fire = [d for d in plan.debts if d.band == "fire"]
    assert fire and fire[0].extra_monthly > 0


def test_lagos_buffer_is_never_glider():
    _fixture, _pipeline, plan = run("lagos_freelancer")
    assert plan.buffer.is_glider is False
    assert "deposit" in plan.buffer.vehicle.lower()


def test_lagos_refusal_names_the_reason():
    _fixture, _pipeline, plan = run("lagos_freelancer")
    reason = plan.glider.blocked_reason.casefold()
    assert "buffer" in reason
    assert "debt" in reason


# ---------------------------------------------------------------------------
# Invest-it-all
# ---------------------------------------------------------------------------


def test_invest_it_all_refusal_names_the_buffer():
    _fixture, _pipeline, plan = run("invest_it_all")
    assert plan.problem_type == "no_buffer"
    assert "buffer" in plan.glider.blocked_reason.casefold()


def test_invest_it_all_gets_no_growth_sleeve_despite_asking():
    _fixture, _pipeline, plan = run("invest_it_all")
    assert plan.book.growth_pct == 0
    assert plan.book.growth_sleeve == []
    assert plan.surplus_monthly == 0


# ---------------------------------------------------------------------------
# Bleed
# ---------------------------------------------------------------------------


def test_bleed_is_named_and_blocks_everything():
    _fixture, pipeline, plan = run("bleed")
    assert plan.problem_type == "cashflow_bleed"
    assert pipeline.safety.bleed is True
    assert plan.book.gated is True
    assert plan.glider.kind == "none"
    assert "exceeds income" in plan.glider.blocked_reason


# ---------------------------------------------------------------------------
# US W2: the healthy case
# ---------------------------------------------------------------------------


def test_us_w2_draft_weights_sum_to_one_hundred_and_carry_no_ids():
    """Weights are final; asset ids are resolved later or not at all."""
    _fixture, _pipeline, plan = run("us_w2_idle_cash")
    draft = plan.glider.draft
    assert draft is not None
    assert sum((w.weight for w in draft.weights), Decimal("0")) == Decimal("100")
    assert all(w.asset_id == "" for w in draft.weights), "ids must not be guessed"
    assert draft.status == "draft_local"
    assert draft.schedule == {"type": "interval", "frequency": "monthly"}
    # Not submit-ready without ids, and it says so rather than pretending.
    with pytest.raises(ValueError, match="resolved asset ids"):
        draft.as_payload()


def test_us_w2_defensive_sleeve_is_never_volatile():
    _fixture, _pipeline, plan = run("us_w2_idle_cash")
    joined = " ".join(plan.book.defensive_sleeve).casefold()
    assert "never part of this sleeve" in joined
    for token in ("bitcoin", "eth", "solana", "doge"):
        assert token not in joined


def test_us_w2_discloses_onchain_risks_before_any_draft():
    _fixture, _pipeline, plan = run("us_w2_idle_cash")
    risks = " ".join(plan.glider.risks).casefold()
    assert "smart contract" in risks
    assert "depeg" in risks
    assert "no deposit insurance" in risks


# ---------------------------------------------------------------------------
# Crypto appetite
# ---------------------------------------------------------------------------


def test_crypto_appetite_is_capped_by_capacity_not_preference():
    _fixture, pipeline, plan = run("crypto_everything")
    assert pipeline.diagnosis.problem_type == "overconfidence_risk"
    assert plan.book.growth_pct <= Decimal("60")
    assert any("caps growth" in override for override in plan.book.overrides)


def test_crypto_plan_is_never_a_single_asset():
    _fixture, _pipeline, plan = run("crypto_everything")
    assert any("never a single token" in item for item in plan.book.growth_sleeve)


# ---------------------------------------------------------------------------
# Existing Glider portfolio
# ---------------------------------------------------------------------------


def test_drifted_portfolio_reports_drift_against_target():
    fixture, _pipeline, plan = run("glider_drifted")
    expected = fixture["expected"]
    equity_id = fixture["portfolio"]["targetAllocation"]["broad_equity_index"]
    stable_id = fixture["portfolio"]["targetAllocation"]["high_quality_stablecoin"]

    assert plan.glider.drift[equity_id] > expected["equity_overweight_points_above"]
    assert plan.glider.drift[stable_id] < 0


def test_drifted_portfolio_action_is_about_the_drift_not_a_new_book():
    """Someone who already holds a portfolio does not need a second one."""
    _fixture, _pipeline, plan = run("glider_drifted")
    actions = " ".join(a.what for a in plan.actions_90d).casefold()
    assert "off target" in actions
    assert "set up the" not in actions


# ---------------------------------------------------------------------------
# Missing data
# ---------------------------------------------------------------------------


def test_missing_income_is_a_data_gap_that_asks():
    _fixture, pipeline, plan = run("missing_income")
    assert plan.problem_type == "data_gap"
    assert plan.confidence == "low"
    assert pipeline.safety.investing_allowed is False
    assert plan.glider.kind == "none"

    asked = " ".join(action.what for action in plan.actions_90d).casefold()
    assert "take-home" in asked
    assert "fixed monthly" in asked


def test_missing_income_invents_no_figures():
    """No plan on numbers we do not have: everything monetary stays zero."""
    _fixture, _pipeline, plan = run("missing_income")
    assert plan.monthly_take_home == 0
    assert plan.surplus_monthly == 0
    assert plan.buffer.target_amount == 0
    assert plan.cashflow.total() == 0
    assert plan.debts == []


# ---------------------------------------------------------------------------
# The invariant is structural, not just observed
# ---------------------------------------------------------------------------


def test_schema_refuses_a_gated_plan_that_invests():
    """A gated book carrying a real growth sleeve cannot be constructed."""
    _fixture, _pipeline, plan = run("us_w2_idle_cash")
    with pytest.raises(ValueError, match="safety-gated"):
        MoneyPlan(
            diagnosis=plan.diagnosis,
            problem_type=plan.problem_type,
            currency=plan.currency,
            monthly_take_home=plan.monthly_take_home,
            cashflow=plan.cashflow,
            buffer=plan.buffer,
            surplus_monthly=Decimal("500"),
            book=plan.book.model_copy(update={"gated": True}),
            glider=plan.glider,
            confidence=plan.confidence,
        )


def test_schema_refuses_surplus_without_a_growth_sleeve():
    _fixture, _pipeline, plan = run("us_w2_idle_cash")
    with pytest.raises(ValueError, match="must appear together"):
        MoneyPlan(
            diagnosis=plan.diagnosis,
            problem_type=plan.problem_type,
            currency=plan.currency,
            monthly_take_home=plan.monthly_take_home,
            cashflow=plan.cashflow,
            buffer=plan.buffer,
            surplus_monthly=Decimal("500"),
            book=plan.book.model_copy(update={"growth_pct": Decimal("0")}),
            glider=plan.glider,
            confidence=plan.confidence,
        )


def test_schema_refuses_a_glider_refusal_without_a_reason():
    with pytest.raises(ValueError, match="must give a reason"):
        from miriam_agent.money.schema import GliderAction

        GliderAction(kind="none")


def test_schema_refuses_a_submitted_draft():
    """Enrollment is user-signed, so a submitted draft is not representable."""
    from miriam_agent.money.schema import DraftWeight, GliderAction, GliderDraft

    draft = GliderDraft(
        name="x",
        template="t",
        book="70/30",
        weights=[DraftWeight(asset_class="a", weight=Decimal("100"))],
        submitted=True,
    )
    with pytest.raises(ValueError, match="user-signed"):
        GliderAction(kind="draft", draft=draft)


def test_from_mapping_rejects_unknown_fields():
    with pytest.raises(Exception):
        from_mapping({"country": "NG", "not_a_field": 1})

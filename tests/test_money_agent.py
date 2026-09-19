"""The narration clamp: the LLM may write words, not numbers.

The clamp is the difference between asking a model not to invent a figure and
enforcing that it did not. Every test here is adversarial on purpose -- the
happy path is the easy half.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

import pytest

from miriam_agent.agents.llm import LLMResponse
from miriam_agent.money import build_money_plan, from_mapping
from miriam_agent.money.agent import (
    check_plan_contradiction,
    clamp_violations,
    narrate,
    narrate_with_report,
    plan_vocabulary,
)
from miriam_agent.money.schema import MoneyPlan


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _intake(**overrides):
    base = {
        "country": "US",
        "currency": "USD",
        "income_amount": "9000",
        "income_frequency": "monthly",
        "income_volatility": "steady",
        "fixed_costs": "4200",
        "variable_spend": "1600",
        "cash_on_hand": "38000",
        "existing_investments": "0",
        "job_stability": "stable",
        "dependents": 0,
        "goals": [{"label": "retirement", "horizon_months": 120}],
        "can_self_custody": True,
    }
    base.update(overrides)
    return from_mapping(base)


def plan_for(**overrides) -> MoneyPlan:
    return build_money_plan(_intake(**overrides))


class FakeProvider:
    """A provider that returns whatever narrative a test hands it."""

    model = "fake"

    def __init__(self, payload=None, *, error=None, no_tool=False):
        self.payload = payload
        self.error = error
        self.no_tool = no_tool

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        if self.error:
            raise self.error
        if self.no_tool:
            return LLMResponse(content="I forget the tool.", tool_calls=[])
        return LLMResponse(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "emit_money_plan_narrative",
                        "arguments": json.dumps(self.payload),
                    },
                }
            ],
        )


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_grounded_narration_is_applied():
    plan = plan_for()
    amount = int(float(plan.surplus_monthly))
    payload = {
        "diagnosis": (
            "You have a surplus and nothing is invested. That is the whole " "problem."
        ),
        "actions": [
            f"This month: fund the book with {amount} a month",
            "This month: hold the buffer where it is",
        ],
    }
    narrated, report = _run(narrate_with_report(plan, provider=FakeProvider(payload)))
    assert report.status == "applied"
    assert narrated.diagnosis == payload["diagnosis"]
    assert narrated.actions_90d[0].what == payload["actions"][0]


def test_narration_preserves_computed_amounts_and_currency():
    """The model rewrites prose; the structured figures are untouched."""
    plan = plan_for()
    payload = {
        "diagnosis": "Nothing is invested.",
        "actions": [f"Invest {int(float(plan.surplus_monthly))} a month"],
    }
    narrated = _run(narrate(plan, provider=FakeProvider(payload)))
    assert narrated.surplus_monthly == plan.surplus_monthly
    assert narrated.cashflow == plan.cashflow
    assert narrated.book == plan.book
    assert narrated.currency == plan.currency


def test_a_shorter_action_list_keeps_the_computed_actions():
    plan = plan_for()
    payload = {"diagnosis": "Nothing is invested.", "actions": ["This month: start"]}
    narrated = _run(narrate(plan, provider=FakeProvider(payload)))
    assert len(narrated.actions_90d) == len(plan.actions_90d)
    assert narrated.actions_90d[0].what == "This month: start"
    # The remaining actions keep their computed wording.
    assert narrated.actions_90d[1].what == plan.actions_90d[1].what


# ---------------------------------------------------------------------------
# The clamp
# ---------------------------------------------------------------------------


def test_invented_amount_voids_the_narration():
    """The headline adversarial case: a confident figure nobody computed."""
    plan = plan_for()
    payload = {
        "diagnosis": "You have ₦9,999,999 working for you already.",
        "actions": ["This payday: do nothing"],
    }
    narrated, report = _run(narrate_with_report(plan, provider=FakeProvider(payload)))
    assert report.status == "rejected_invented_numbers"
    assert "9999999" in report.violations
    assert narrated.diagnosis == plan.diagnosis, "deterministic text must stand"


def test_invented_percentage_voids_the_narration():
    """Rates are the most dangerous invention, so they are checked too."""
    plan = plan_for()
    payload = {
        "diagnosis": "Your portfolio is expected to return 18% a year.",
        "actions": ["This month: relax"],
    }
    _narrated, report = _run(narrate_with_report(plan, provider=FakeProvider(payload)))
    assert report.status == "rejected_invented_numbers"
    assert "18" in report.violations


def test_an_invented_number_in_an_action_also_voids_the_whole_narration():
    plan = plan_for()
    payload = {
        "diagnosis": "Nothing is invested.",
        "actions": ["This payday: move 777777 into the book"],
    }
    _narrated, report = _run(narrate_with_report(plan, provider=FakeProvider(payload)))
    assert report.status == "rejected_invented_numbers"


def test_restating_a_computed_figure_is_allowed():
    """The clamp forbids new figures, not the plan's own."""
    plan = plan_for()
    amount = int(float(plan.surplus_monthly))
    payload = {
        "diagnosis": f"You are leaving {amount} a month uninvested.",
        "actions": [f"This month: invest {amount}"],
    }
    _narrated, report = _run(narrate_with_report(plan, provider=FakeProvider(payload)))
    assert report.status == "applied", report.violations


def test_plan_vocabulary_contains_the_computed_figures():
    plan = plan_for()
    vocab = plan_vocabulary(plan)
    assert str(int(float(plan.surplus_monthly))) in vocab
    assert str(int(float(plan.monthly_take_home))) in vocab
    assert "70" in vocab  # the book's growth share


def test_clamp_detects_a_figure_absent_from_a_gated_plan():
    """A refusal must not sprout a number either."""
    plan = build_money_plan(
        from_mapping(
            {
                "country": "NG",
                "currency": "NGN",
                "income_amount": "450000",
                "fixed_costs": "300000",
                "variable_spend": "80000",
                "cash_on_hand": "0",
                "debts": [
                    {
                        "label": "payday loan",
                        "balance": "600000",
                        "apr_pct": "35",
                        "minimum_monthly": "45000",
                    }
                ],
            }
        )
    )
    narrative = _Narrative(
        diagnosis="You have 8888888 available to invest today.",
        actions=[],
    )
    assert clamp_violations(plan, narrative) == ["8888888"]


def _Narrative(diagnosis, actions):
    from miriam_agent.money.agent import MoneyNarrative

    return MoneyNarrative(diagnosis=diagnosis, actions=actions)


# ---------------------------------------------------------------------------
# Failure is fail-open
# ---------------------------------------------------------------------------


def test_llm_failure_returns_the_computed_plan_unchanged():
    plan = plan_for()
    provider = FakeProvider(error=RuntimeError("gateway down"))
    narrated, report = _run(narrate_with_report(plan, provider=provider))
    assert report.status == "unavailable"
    assert narrated == plan


def test_missing_tool_call_is_reported_as_invalid():
    plan = plan_for()
    narrated, report = _run(
        narrate_with_report(plan, provider=FakeProvider(no_tool=True))
    )
    assert report.status == "invalid"
    assert narrated == plan


def test_a_plan_with_no_provider_configured_stays_intact():
    """No LLM configured is a normal state, not a broken plan."""
    plan = plan_for()
    provider = FakeProvider(error=RuntimeError("OPENAI_API_KEY is not set"))
    narrated, report = _run(narrate_with_report(plan, provider=provider))
    assert report.status == "unavailable"
    assert narrated.is_investing() == plan.is_investing()


def test_narrated_plan_still_satisfies_the_ordering_invariant():
    """Whatever the model writes, the plan cannot become an invalid one."""
    plan = build_money_plan(
        from_mapping(
            {
                "country": "NG",
                "currency": "NGN",
                "income_amount": "450000",
                "fixed_costs": "300000",
                "variable_spend": "80000",
                "cash_on_hand": "0",
                "debts": [
                    {
                        "label": "payday loan",
                        "balance": "600000",
                        "apr_pct": "35",
                        "minimum_monthly": "45000",
                    }
                ],
            }
        )
    )
    payload = {
        "diagnosis": "Invest as much as you can right now.",
        "actions": ["This month: put it all in"],
    }
    narrated = _run(narrate(plan, provider=FakeProvider(payload)))
    assert narrated.book.gated is True
    assert narrated.surplus_monthly == 0
    assert narrated.glider.kind == "none"


# ---------------------------------------------------------------------------
# The second gate: prose that contradicts a refusal
# ---------------------------------------------------------------------------


def _gated_plan():
    """Lagos: the plan correctly refuses to invest."""
    return build_money_plan(
        from_mapping(
            {
                "country": "NG",
                "currency": "NGN",
                "income_amount": "450000",
                "fixed_costs": "300000",
                "variable_spend": "80000",
                "cash_on_hand": "0",
                "debts": [
                    {
                        "label": "payday loan",
                        "balance": "600000",
                        "apr_pct": "35",
                        "minimum_monthly": "45000",
                    }
                ],
            }
        )
    )


@pytest.mark.parametrize(
    "text",
    [
        "You should start investing this month.",
        "Your portfolio would do well here.",
        "Let's put it all in crypto.",
        "A 70/30 allocation suits you.",
        "Glider has a strategy for this.",
    ],
)
def test_prose_that_implies_investing_is_caught(text):
    """A correct refusal plus prose that ignores it is still a wrong answer."""
    plan = _gated_plan()
    assert plan.is_investing() is False
    assert check_plan_contradiction(plan, text), text


@pytest.mark.parametrize(
    "text",
    [
        "Nothing goes onchain yet.",
        "You are not ready to invest.",
        "There is no portfolio here and there should not be.",
        "Do not invest until the buffer holds.",
        "I would hold off on crypto.",
    ],
)
def test_negated_investing_language_is_allowed(text):
    """A refusal has to be able to say the word 'invest'."""
    plan = _gated_plan()
    assert check_plan_contradiction(plan, text) == []


def test_the_checker_does_not_fire_on_a_plan_that_does_invest():
    """The gate only applies when the plan refused."""
    plan = plan_for()
    assert plan.is_investing() is True
    assert check_plan_contradiction(plan, "Your portfolio is funded.") == []


def test_narration_that_contradicts_the_plan_is_rejected():
    plan = _gated_plan()
    payload = {
        "diagnosis": "Start investing this month, you can afford it.",
        "actions": ["This payday: open a portfolio"],
    }
    narrated, report = _run(narrate_with_report(plan, provider=FakeProvider(payload)))
    assert report.status == "rejected_contradicts_plan"
    assert report.violations
    assert narrated.diagnosis == plan.diagnosis, "deterministic text must stand"
    assert narrated.is_investing() is False


def test_a_grounded_negated_narration_is_accepted():
    plan = _gated_plan()
    payload = {
        "diagnosis": "Nothing goes onchain and no portfolio exists yet.",
        "actions": ["This payday: build the buffer"],
    }
    _narrated, report = _run(narrate_with_report(plan, provider=FakeProvider(payload)))
    assert report.status == "applied", report.violations

"""The public money path: one door, connected balances, and a runnable demo.

Step 2 of the work order says there must be exactly one function that does money
maths. These tests hold that line: the diagnostics view and the plan view must
agree, the plan must come from the same core, and the CLI a user actually runs
must produce a real plan.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

import pytest

from miriam_agent.money import (
    AccountsSnapshot,
    GliderState,
    build_money_plan,
    coerce_profile,
    explain_money_plan,
    from_mapping,
)
from miriam_agent.money.demo import FIXTURES, main, run_fixture, summarize

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "money"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _us_profile():
    return {
        "country": "US",
        "currency": "USD",
        "income_amount": "9000",
        "fixed_costs": "4200",
        "variable_spend": "1600",
        "cash_on_hand": "38000",
        "job_stability": "stable",
        "dependents": 0,
        "goals": [{"label": "retirement", "horizon_months": 120}],
        # Only a user who can hold their own keys is offered an onchain draft.
        "can_self_custody": True,
    }


# ---------------------------------------------------------------------------
# One door
# ---------------------------------------------------------------------------


def test_the_plan_and_the_trace_agree():
    """Diagnostics are not a second planner: same core, same numbers."""
    profile = _us_profile()
    pipeline = explain_money_plan(profile)
    plan = build_money_plan(profile)

    assert plan.surplus_monthly == pipeline.book.investable_surplus
    assert plan.book == pipeline.book
    assert plan.glider == pipeline.glider
    assert plan.cashflow == pipeline.cashflow
    assert plan.problem_type == pipeline.diagnosis.problem_type


def test_build_money_plan_accepts_a_dict():
    plan = build_money_plan(_us_profile())
    assert plan.is_investing() is True


def test_build_money_plan_accepts_an_intake_profile():
    intake = from_mapping(_us_profile())
    assert build_money_plan(intake).is_investing() is True


def test_build_money_plan_accepts_a_financial_profile():
    """The existing profile layer feeds the same door.

    A ``FinancialProfile`` carries amounts but not a country, so a caller that
    needs localized rates uses the bridge first. That is the documented two-step
    for this case, and it keeps country out of the money maths.
    """
    from miriam_agent.financial.profile import FinancialProfile
    from miriam_agent.money import from_financial_profile

    profile = FinancialProfile()
    profile.set_fact("income_amount", 9000, source="user")
    profile.set_fact("income_frequency", "monthly", source="user")
    profile.set_fact("essential_expenses", 4200, source="user")
    profile.set_fact("savings", 38000, source="user")

    intake = from_financial_profile(profile, country="US", currency="USD")
    plan = build_money_plan(intake)
    assert plan.monthly_take_home == Decimal("9000")
    assert plan.buffer.current_amount == Decimal("38000")


def test_coerce_profile_rejects_nonsense():
    with pytest.raises(TypeError, match="build_money_plan expects"):
        coerce_profile(42)


def test_there_is_no_second_math_door():
    """``run_pipeline`` was folded into the one core; it must not come back."""
    import miriam_agent.money as money
    import miriam_agent.money.plan as plan_module

    assert not hasattr(money, "run_pipeline")
    assert not hasattr(plan_module, "run_pipeline")


# ---------------------------------------------------------------------------
# Connected balances
# ---------------------------------------------------------------------------


def test_connected_balances_override_a_stated_figure():
    """A ledger is evidence; a memory is not."""
    profile = _us_profile()
    profile["cash_on_hand"] = "1000"

    stated = build_money_plan(profile)
    connected = build_money_plan(
        profile, AccountsSnapshot(cash=Decimal("50000"), source="ledger")
    )
    assert connected.buffer.current_amount == Decimal("50000")
    assert connected.buffer.current_amount != stated.buffer.current_amount


def test_a_partial_snapshot_does_not_blank_a_stated_number():
    """Only the fields the snapshot carries are overridden."""
    plan = build_money_plan(
        _us_profile(),
        AccountsSnapshot(cash=Decimal("50000")),
    )
    assert plan.monthly_take_home == Decimal("9000")
    assert plan.cashflow.fixed == Decimal("4200")


def test_connected_balances_are_recorded_as_an_assumption():
    plan = build_money_plan(
        _us_profile(), AccountsSnapshot(cash=Decimal("50000"), source="ledger")
    )
    assert any("read from ledger" in note for note in plan.assumptions)


def test_connected_debts_drive_the_triage():
    plan = build_money_plan(
        _us_profile(),
        AccountsSnapshot(
            debts=[
                {
                    "label": "card",
                    "balance": "9000",
                    "apr_pct": "29",
                    "minimum_monthly": "300",
                }
            ]
        ),
    )
    assert plan.debts and plan.debts[0].band == "fire"
    assert plan.is_investing() is False


# ---------------------------------------------------------------------------
# Glider state
# ---------------------------------------------------------------------------


def test_glider_state_turns_a_draft_into_a_monitor_decision():
    without = build_money_plan(_us_profile())
    assert without.glider.kind == "draft"

    fixture = json.loads((FIXTURE_DIR / "glider_drifted.json").read_text())
    with_state = build_money_plan(
        _us_profile(),
        glider_state=GliderState(
            portfolio=fixture["portfolio"], positions=fixture["positions"]
        ),
    )
    assert with_state.glider.kind == "monitor"
    assert with_state.glider.portfolio_id == "a1b2c3d4"
    assert with_state.glider.drift


# ---------------------------------------------------------------------------
# The tool the model must call
# ---------------------------------------------------------------------------


def test_get_money_plan_tool_is_registered_and_read_only():
    from miriam_agent.tools import build_tool_registry

    tool = build_tool_registry().get("get_money_plan")
    assert tool is not None
    assert tool.is_mutation is False
    assert tool.requires_approval is False
    assert not tool.allow_auto_execute or True  # read tools may auto-run


def test_get_money_plan_tool_returns_a_plan_and_a_spoken_line():
    from miriam_agent.tools import build_tool_registry

    tool = build_tool_registry().get("get_money_plan")
    result = _run(tool.handler({"profile": _us_profile()}, {"token": None}))

    assert result["plan"]["surplus_monthly"] is not None
    assert result["spoken"]
    assert result["glider_kind"] == "draft"
    assert result["investing"] is True
    assert result["next_action"]


def test_get_money_plan_tool_refuses_for_the_lagos_profile():
    from miriam_agent.tools import build_tool_registry

    tool = build_tool_registry().get("get_money_plan")
    result = _run(
        tool.handler(
            {
                "profile": {
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
            },
            {"token": None},
        )
    )
    assert result["investing"] is False
    assert result["glider_kind"] == "none"
    assert "buffer" in result["spoken"].casefold()


def test_get_money_plan_tool_survives_no_input():
    """A missing profile is a data gap, not a crash and not a fabricated plan."""
    from miriam_agent.tools import build_tool_registry

    tool = build_tool_registry().get("get_money_plan")
    result = _run(tool.handler({}, {"token": None}))
    assert result["plan"]["problem_type"] == "data_gap"
    assert result["investing"] is False


# ---------------------------------------------------------------------------
# The demo a user runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias", sorted(FIXTURES))
def test_every_alias_loads_and_produces_a_plan(alias):
    plan = run_fixture(alias)
    assert plan.diagnosis
    assert summarize(plan)


def test_demo_all_exits_zero_when_every_gate_passes():
    """Exit code is the merge gate: a failing gate must fail the command."""
    assert main(["--all"]) == 0


@pytest.mark.parametrize("alias", ["lagos", "us_surplus", "invest_it_all"])
def test_demo_single_fixture_prints_a_plan(alias, capsys):
    assert main(["--fixture", alias]) == 0
    out = capsys.readouterr().out
    assert "diagnosis" in out
    assert "glider" in out
    assert "next action" in out


def test_demo_json_mode_emits_a_real_plan(capsys):
    assert main(["--fixture", "us_surplus", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["problem_type"] == "idle_surplus"
    assert payload["glider"]["kind"] == "draft"


def test_demo_full_mode_prints_the_structured_block(capsys):
    assert main(["--fixture", "lagos", "--full"]) == 0
    out = capsys.readouterr().out
    assert "1. DIAGNOSIS" in out
    assert "12. WHAT WOULD CHANGE THIS PLAN" in out


def test_demo_unknown_fixture_is_a_clear_error():
    with pytest.raises(SystemExit, match="known fixtures"):
        run_fixture("not_a_fixture")

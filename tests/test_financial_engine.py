"""Financial-snapshot intelligence engine: health, forecast, plan."""

from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.financial.intelligence import (
    compute_cash_flow_forecast,
    compute_financial_health,
    compute_financial_plan,
    period_to_window,
)


def snapshot(
    *,
    spend: str = "900.00",
    stash: str = "700.00",
    total: str = "1600.00",
    deposits: str = "5000.00",
    withdrawals: str = "600.00",
    card: str = "400.00",
    monthly_flow: list | None = None,
    budget: dict | None = None,
    profile: dict | None = None,
    obligations: list | None = None,
    from_date: str = "2026-06-01",
    to_date: str = "2026-08-31",
) -> dict:
    if budget is None:
        budget = {"set": True, "monthly_limit": "2000.00", "currency": "USD"}
    if profile is None:
        profile = {
            "has_profile": True,
            "monthly_income": "3000.00",
            "income_frequency": "monthly",
            "monthly_fixed_costs": "1200.00",
            "monthly_savings_target": "300.00",
            "emergency_fund_target": "5000.00",
            "risk_tolerance": "moderate",
            "investment_horizon": "medium",
            "financial_goal": "emergency_fund",
            "primary_currency": "USD",
        }
    return {
        "period": {"from": from_date, "to": to_date},
        "balances": {
            "spending_balance": spend,
            "stash_balance": stash,
            "total_balance": total,
        },
        "money_flow": {
            "total_deposits": deposits,
            "deposit_count": 2,
            "total_withdrawals": withdrawals,
            "withdrawal_count": 4,
            "total_card_spend": card,
            "card_spend_count": 6,
            "total_p2p": "0.00",
            "p2p_count": 0,
            "total_receipts": "0.00",
            "receipt_count": 0,
        },
        "monthly_flow": monthly_flow or [],
        "budget": budget,
        "profile": profile,
        "upcoming_obligations": obligations or [],
    }


def test_period_to_window_maps_periods():
    today = date(2026, 9, 13)
    assert period_to_window("this_month", today) == ("2026-09-01", "2026-09-13")
    assert period_to_window("last_month", today) == ("2026-08-01", "2026-08-31")
    f, t = period_to_window("last_90_days", today)
    assert t == "2026-09-13"
    assert (date.fromisoformat(t) - date.fromisoformat(f)).days == 89
    f6, t6 = period_to_window("last_6_months", today)
    assert t6 == "2026-09-13"
    f12, t12 = period_to_window("last_12_months", today)
    assert t12 == "2026-09-13"
    assert period_to_window("unknown_period", today) == (None, None)


def test_health_scores_strong_case():
    health = compute_financial_health(snapshot(), period="last_90_days")
    assert health["source"] == "python"
    assert health["engine"] == "financial-snapshot"
    assert health["score"] >= 80
    assert health["status"] == "strong"
    assert health["budget_status"] == "on_track"
    assert health["savings_rate_pct"] > 50
    by_name = {c["name"]: c["score"] for c in health["score_components"]}
    assert by_name["Savings Rate"] == 25
    assert by_name["Budget Control"] == 20
    assert by_name["Runway"] == 25
    assert by_name["Stash Discipline"] == 20


def test_health_fragile_on_empty_data():
    health = compute_financial_health(
        snapshot(
            spend="5.00",
            stash="0.00",
            total="5.00",
            deposits="0.00",
            withdrawals="0.00",
            card="0.00",
        ),
        period="this_month",
    )
    assert 0 <= health["score"] <= 100
    assert health["score"] < 60
    assert health["budget_status"] == "on_track"
    assert health["monthly_income"] == 0.0


def test_health_budget_over_budget_flag():
    health = compute_financial_health(
        snapshot(deposits="10000.00", withdrawals="6000.00", card="1500.00"),
        period="last_90_days",
    )
    assert health["budget_status"] == "over_budget"
    assert health["budget_remaining"] < 0
    assert any("over budget" in a for a in health["recommended_actions"])


def test_health_multi_month_trend():
    monthly_flow = [
        {"month": "2026-06", "total_deposits": "1000.00", "total_outflow": "900.00"},
        {"month": "2026-07", "total_deposits": "1200.00", "total_outflow": "800.00"},
        {"month": "2026-08", "total_deposits": "1800.00", "total_outflow": "700.00"},
    ]
    health = compute_financial_health(
        snapshot(
            deposits="4000.00",
            withdrawals="1500.00",
            card="900.00",
            monthly_flow=monthly_flow,
        ),
        period="last_90_days",
    )
    assert len(health["monthly_trend"]) == 3
    assert health["monthly_trend"][0]["month"] == "2026-06"
    assert health["trend_direction"] == "improving"


def test_forecast_projects_from_balances_and_budget():
    forecast = compute_cash_flow_forecast(snapshot())
    assert forecast["engine"] == "financial-snapshot"
    assert forecast["spend_balance"] == 900.0
    assert forecast["stash_balance"] == 700.0
    assert forecast["days_remaining"] >= 0
    assert forecast["safe_daily_spend"] > 0
    assert forecast["projected_end_balance"] >= 0
    assert (
        forecast["projected_end_balance"]
        <= forecast["spend_balance"] + forecast["stash_balance"]
    )
    assert forecast["confidence"] in {"low", "medium", "high"}
    assert "Stay under" in forecast["primary_action"]
    assert forecast["next_month"]["expected_net"] == forecast["projected_net_flow"]


def test_plan_includes_profile_driven_steps():
    plan = compute_financial_plan(snapshot())
    assert plan["engine"] == "financial-snapshot"
    assert plan["health"]["score"] >= 80
    assert plan["forecast"]["projected_end_balance"] >= 0
    assert plan["profile"]["has_profile"] is True
    titles = [step["title"] for step in plan["next_steps"]]
    assert "Close the emergency-fund gap" in titles
    assert "Protect this month" in titles


def test_legacy_spending_summary_fallback():
    legacy = {
        "balances": {"spending_balance": "10.00", "stash_balance": "5.00"},
        "spending_summary": {"total_spent": "300.00"},
        "upcoming_obligations": [],
    }
    health = compute_financial_health(legacy)
    assert health["source"] == "python"
    assert 0 <= health["score"] <= 100
    assert health["spend_balance"] == 10.0
    forecast = compute_cash_flow_forecast(legacy)
    assert forecast["engine"] == "financial-snapshot"


def test_tool_definitions_not_stubbed():
    from miriam_agent.tools.definitions import build_tool_registry

    registry = build_tool_registry()
    for name in (
        "get_financial_health",
        "get_cash_flow_forecast",
        "get_financial_plan",
    ):
        tool = registry.get(name)
        assert tool is not None, name
        assert "not available yet" not in tool.description
    health = registry.get("get_financial_health")
    period = health.args_schema["properties"]["period"]
    assert "last_90_days" in period["enum"]
    assert "last_12_months" in period["enum"]


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-x"])

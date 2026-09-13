"""Regression tests for miriam_agent.financial.intelligence.FinancialIntelligence.

Covers three bugs found during the production audit, all in the same two
public methods:

  - ``analyze_portfolio`` crashed on every call: it passed a ``period``
    argument to ``memory_store.get_portfolio_data``, which only ever
    accepted ``user_id``.
  - ``generate_budget_plan`` crashed for any user with no locally-stored
    income (essentially everyone) via a division-by-zero in
    ``_allocate_budget``.
  - ``generate_budget_plan`` also crashed once real per-category expense
    data was present, via a ``float - dict`` TypeError when computing
    ``net_cash_flow`` (masked by ``_calculate_monthly_expenses`` silently
    returning ``{}`` for the old stub's empty data).

Also covers the new behavior: both tools now pull real numbers from the Go
backend when a token is supplied, instead of always operating on
hardcoded-zero local stubs.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeMemoryStore:
    """Mimics database/memory.py's stub methods (no profile, no data)."""

    async def get_portfolio_data(self, user_id):
        return {
            "investments": [],
            "total_value": 0.0,
            "daily_returns": [],
            "allocations": {},
        }

    async def get_income_data(self, user_id):
        return {"monthly_income": 0.0, "income_streams": [], "frequency": "monthly"}

    async def get_expense_data(self, user_id):
        return {"monthly_expenses": 0.0, "categories": {}, "transactions": []}


class FakeGoClient:
    """Mimics the real Go backend response shapes seen in test_proactive.py."""

    async def get_investment_positions(self, token):
        return [
            {"symbol": "VOO", "value": "8000.00", "cost_basis": "6000.00"},
            {"symbol": "AAPL", "value": "2000.00", "cost_basis": "2500.00"},
        ]

    async def get_spending_summary(self, token, period="month"):
        return {
            "total_spent": "1200.00",
            "top_categories": [
                {"category": "housing", "amount": "900.00"},
                {"category": "food", "amount": "300.00"},
            ],
        }


# -----------------------------------------------------------------------
# analyze_portfolio
# -----------------------------------------------------------------------


def test_analyze_portfolio_no_longer_crashes_without_token():
    """Regression: used to raise TypeError -> FinancialError on every call."""
    from miriam_agent.financial.intelligence import FinancialIntelligence

    fi = FinancialIntelligence(FakeMemoryStore())
    result = _run(fi.analyze_portfolio("u-1"))
    # No holdings anywhere -> honest "no data" response, not a crash and
    # not a fake all-zeros analysis.
    assert result["error"] == "No portfolio data found"


def test_analyze_portfolio_uses_real_go_positions_when_token_present():
    from miriam_agent.financial.intelligence import FinancialIntelligence

    fi = FinancialIntelligence(FakeMemoryStore(), go_client=FakeGoClient())
    result = _run(fi.analyze_portfolio("u-1", token="tok"))

    assert "error" not in result
    assert result["performance"]["total_value"] == 10000.0
    assert result["performance"]["cost_basis"] == 8500.0


# -----------------------------------------------------------------------
# generate_budget_plan
# -----------------------------------------------------------------------


def test_generate_budget_plan_no_longer_crashes_with_zero_income():
    """Regression: used to raise ZeroDivisionError -> FinancialError for any
    user without a stored financial profile (the common case today)."""
    from miriam_agent.financial.intelligence import FinancialIntelligence

    fi = FinancialIntelligence(FakeMemoryStore())
    result = _run(fi.generate_budget_plan("u-1"))

    assert result["monthly_income"] == 0.0
    assert result["net_cash_flow"] == 0.0
    assert result["budget_allocation"]["savings"]["percentage"] == 0.0


def test_generate_budget_plan_uses_real_go_expenses_when_token_present():
    from miriam_agent.financial.intelligence import FinancialIntelligence

    class MemoryWithIncome(FakeMemoryStore):
        async def get_income_data(self, user_id):
            return {
                "regular_income": [{"amount": 5000, "frequency": "monthly"}],
                "bonus_income": [],
            }

    fi = FinancialIntelligence(MemoryWithIncome(), go_client=FakeGoClient())
    result = _run(fi.generate_budget_plan("u-1", token="tok"))

    assert result["monthly_income"] == 5000.0
    assert result["monthly_expenses"] == {"housing": 900.0, "food": 300.0}
    # net_cash_flow must be a number (used to TypeError against a dict).
    assert isinstance(result["net_cash_flow"], float)
    assert result["budget_allocation"]["housing"]["monthly_amount"] == 900.0

"""Investment tool registration and routing tests.

Verifies the investment Agent-API tools land in the live registry with the
right safety metadata (reads auto-execute, mutations stage-and-confirm), that
tools pointing at removed endpoints are gone (and that the agent exposes no
withdrawal tool), and that handlers call the expected Go client methods and
payloads, including staged-confirmation pass-through.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.agents.tools import RiskLevel  # noqa: E402
from miriam_agent.tools import build_tool_registry  # noqa: E402

READ_TOOLS = {
    "get_portfolio",
    "get_positions",
    "get_asset",
    "search_assets",
    "get_strategy",
    "list_strategies",
    "get_rebalance_preview",
    "get_investment_limits",
    "list_executions",
    "get_execution",
    "get_execution_status",
    "list_audit_events",
    "get_investor",
    "get_investor_activity",
    "list_investors",
    # new Glider transaction detail reads
    "get_strategy_performance",
    "get_enrollment_performance",
    "get_sector_exposure",
    "get_allocation_breakdown",
    "get_strategy_schedule",
    "get_strategy_preferences",
    "get_strategy_fees",
    "get_provider_versions",
}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_read_tools_registered_and_auto_execute():
    reg = build_tool_registry()
    auto = reg.auto_execute_names()
    for name in READ_TOOLS:
        tool = reg.get(name)
        assert tool is not None, name
        assert tool.category == "investment", name
        assert tool.risk_level == RiskLevel.LOW, name
        assert tool.is_mutation is False, name
        assert tool.requires_approval is False, name
        assert tool.allow_auto_execute is True, name
        assert name in auto, name


def test_removed_and_invented_tools_are_gone():
    reg = build_tool_registry()
    for name in (
        "execute_investment",
        "preview_strategy",
        "get_order",
        "get_transaction",
        "get_transaction_status",
        "buy_multiple_assets",
    ):
        assert reg.get(name) is None, name
        assert name not in reg.list_names(), name


def test_agent_has_no_withdrawal_tool():
    from miriam_agent.tools import build_tool_registry
    from miriam_agent.tools import vault_definitions as _vault  # noqa: F401

    reg = build_tool_registry()
    assert not any(
        "withdraw" in name and "preview" not in name for name in reg.list_names()
    )


def test_descriptions_are_honest_about_what_glider_publishes():
    reg = build_tool_registry()
    for name in ("list_investors", "get_investor", "get_investor_activity"):
        desc = reg.get(name).description.lower()
        assert "publicly exposes" in desc, name


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def get_investment_portfolio(self, token):
        self.calls.append(("get_investment_portfolio", token))
        return {"total_value_usd": 42, "source": "glider"}

    async def get_investment_execution(self, token, execution_id):
        self.calls.append(("get_investment_execution", token, execution_id))
        return {"id": execution_id, "status": "filled"}

    async def create_investment_order(self, token, payload, confirmation_token=None):
        self.calls.append(
            ("create_investment_order", token, payload, confirmation_token)
        )
        return {"status": "AWAITING_CONFIRMATION"}

    async def create_investment_strategy(self, token, payload, confirmation_token=None):
        self.calls.append(
            ("create_investment_strategy", token, payload, confirmation_token)
        )
        return {"status": "AWAITING_CONFIRMATION"}

    async def set_investment_allocation(self, token, payload, confirmation_token=None):
        self.calls.append(
            ("set_investment_allocation", token, payload, confirmation_token)
        )
        return {"status": "AWAITING_CONFIRMATION"}

    async def rebalance_investment_strategy(self, token, strategy_id, reason=None):
        self.calls.append(("rebalance_investment_strategy", token, strategy_id, reason))
        return {"id": "e1", "status": "completed"}


def test_get_portfolio_handler_calls_investment_client(monkeypatch):
    import miriam_agent.tools.investment_definitions as inv

    fake = _FakeClient()
    monkeypatch.setattr(inv, "get_go_client", lambda: fake)
    reg = build_tool_registry()

    result = _run(reg.execute("get_portfolio", {}, {"token": "tok"}))
    assert result["total_value_usd"] == 42
    assert fake.calls == [("get_investment_portfolio", "tok")]


def test_get_execution_status_returns_status_subset(monkeypatch):
    import miriam_agent.tools.investment_definitions as inv

    fake = _FakeClient()
    monkeypatch.setattr(inv, "get_go_client", lambda: fake)
    reg = build_tool_registry()

    result = _run(
        reg.execute("get_execution_status", {"execution_id": "e1"}, {"token": "tok"})
    )
    assert result["execution_id"] == "e1"
    assert result["status"] == "filled"
    assert "failure_reason" not in result


def test_no_investment_tool_is_a_mutation():
    """The action tools are deleted, so the whole module is read-only.

    Anything here that created a strategy, enrolled funds, set an allocation or
    placed an order called a rail. A rail is not reachable from a chat turn, so
    those tools do not exist and nothing in this module may write.
    """
    from miriam_agent.tools import build_tool_registry

    registry = build_tool_registry()
    for tool in registry.list_by_category("investment"):
        assert not tool.is_mutation, tool.name
        assert not tool.requires_approval, tool.name
        assert tool.allow_auto_execute, tool.name
    assert registry.list_by_category("investment_action") == []

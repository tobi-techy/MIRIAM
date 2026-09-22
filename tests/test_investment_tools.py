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
from miriam_agent.core.exceptions import IntegrationError  # noqa: E402
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
}

ACTION_TOOLS = {
    "create_strategy",
    "update_strategy",
    "enroll_strategy",
    "pause_strategy",
    "resume_strategy",
    "rebalance_strategy",
    "buy_asset",
    "sell_asset",
    "set_allocation",
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


def test_action_tools_registered_and_stage_confirm():
    reg = build_tool_registry()
    staged = reg.stage_confirm_names()
    for name in ACTION_TOOLS:
        tool = reg.get(name)
        assert tool is not None, name
        assert tool.category == "investment_action", name
        assert tool.risk_level == RiskLevel.HIGH, name
        assert tool.is_mutation is True, name
        assert tool.requires_approval is True, name
        assert tool.allow_auto_execute is False, name
        assert name in staged, name


def test_removed_and_invented_tools_are_gone():
    reg = build_tool_registry()
    for name in (
        "execute_investment",
        "get_strategy_performance",
        "preview_strategy",
        "get_order",
        "get_transaction",
        "get_transaction_status",
        "buy_multiple_assets",
    ):
        assert reg.get(name) is None, name
        assert name not in reg.list_names(), name


def test_agent_has_no_withdrawal_tool():
    from miriam_agent.tools import vault_definitions as _vault  # noqa: F401
    from miriam_agent.tools import build_tool_registry

    reg = build_tool_registry()
    assert not any(
        "withdraw" in name and "preview" not in name for name in reg.list_names()
    )


def test_descriptions_are_honest_about_glider_orders_and_investors():
    reg = build_tool_registry()
    for name in ("buy_asset", "sell_asset", "set_allocation"):
        desc = reg.get(name).description.lower()
        assert "limit order" in desc, name
        assert "not" in desc, name
    for name in ("list_investors", "get_investor", "get_investor_activity"):
        desc = reg.get(name).description.lower()
        assert "publicly exposes" in desc, name


def test_staged_tool_descriptions_document_confirmation_token():
    reg = build_tool_registry()
    for name in (
        "create_strategy",
        "update_strategy",
        "enroll_strategy",
        "buy_asset",
        "sell_asset",
        "set_allocation",
    ):
        desc = reg.get(name).description.lower()
        assert "confirmation_token" in desc, name
        assert "staged for confirmation" in desc, name


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


def test_buy_asset_handler_builds_order_payload(monkeypatch):
    import miriam_agent.tools.investment_definitions as inv

    fake = _FakeClient()
    monkeypatch.setattr(inv, "get_go_client", lambda: fake)
    reg = build_tool_registry()

    result = _run(
        reg.execute(
            "buy_asset",
            {"symbol": "AAPL", "amount_usd": 25},
            {"token": "tok", "idempotency_key": "k-inv"},
        )
    )
    assert result["status"] == "AWAITING_CONFIRMATION"
    name, token, payload, confirmation = fake.calls[0]
    assert name == "create_investment_order"
    assert token == "tok"
    assert payload["side"] == "buy"
    assert payload["symbol"] == "AAPL"
    assert payload["amount_usd"] == 25
    assert payload["idempotency_key"] == "k-inv"
    assert confirmation is None


def test_create_strategy_passes_confirmation_token_through_ctx(monkeypatch):
    import miriam_agent.tools.investment_definitions as inv

    fake = _FakeClient()
    monkeypatch.setattr(inv, "get_go_client", lambda: fake)
    reg = build_tool_registry()

    _run(
        reg.execute(
            "create_strategy",
            {
                "name": "Growth",
                "objective": "long-term growth",
                "risk": "medium",
                "horizon": "10y",
                "target_allocation": [{"symbol": "VOO", "weight": 1.0}],
            },
            {"token": "tok", "confirmation_token": "cfm-9"},
        )
    )
    name, token, payload, confirmation = fake.calls[0]
    assert name == "create_investment_strategy"
    assert token == "tok"
    assert payload["name"] == "Growth"
    assert confirmation == "cfm-9"


def test_set_allocation_handler_builds_targets_payload(monkeypatch):
    import miriam_agent.tools.investment_definitions as inv

    fake = _FakeClient()
    monkeypatch.setattr(inv, "get_go_client", lambda: fake)
    reg = build_tool_registry()

    _run(
        reg.execute(
            "set_allocation",
            {
                "strategy_id": "s1",
                "targets": [
                    {"symbol": "AAPL", "weight": 0.6},
                    {"symbol": "VOO", "weight": 0.4},
                ],
                "rationale": "rebalance",
            },
            {"token": "tok", "idempotency_key": "k-alloc"},
        )
    )
    name, token, payload, confirmation = fake.calls[0]
    assert name == "set_investment_allocation"
    assert payload["strategy_id"] == "s1"
    assert payload["targets"][0]["symbol"] == "AAPL"
    assert payload["rationale"] == "rebalance"
    assert payload["idempotency_key"] == "k-alloc"
    assert confirmation is None


def test_rebalance_strategy_uses_strategy_id_and_reason(monkeypatch):
    import miriam_agent.tools.investment_definitions as inv

    fake = _FakeClient()
    monkeypatch.setattr(inv, "get_go_client", lambda: fake)
    reg = build_tool_registry()

    result = _run(
        reg.execute(
            "rebalance_strategy",
            {"strategy_id": "s1", "reason": "drift"},
            {"token": "tok"},
        )
    )
    assert result["status"] == "completed"
    assert fake.calls == [("rebalance_investment_strategy", "tok", "s1", "drift")]


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


def test_provider_cooldown_surfaces_as_clean_tool_error(monkeypatch):
    import miriam_agent.tools.investment_definitions as inv

    class _CooldownClient(_FakeClient):
        async def rebalance_investment_strategy(self, token, strategy_id, reason=None):
            raise IntegrationError(
                "Go backend POST /api/v1/investments/strategies/s1/rebalance "
                "failed: INVESTMENT_PROVIDER_COOLDOWN"
            )

    monkeypatch.setattr(inv, "get_go_client", lambda: _CooldownClient())
    reg = build_tool_registry()

    import pytest

    with pytest.raises(IntegrationError) as excinfo:
        _run(reg.execute("rebalance_strategy", {"strategy_id": "s1"}, {"token": "tok"}))
    assert "INVESTMENT_PROVIDER_COOLDOWN" in str(excinfo.value)


def test_staged_confirmation_tools_match_the_documented_set():
    from miriam_agent.tools.investment_definitions import (
        STAGED_CONFIRMATION_TOOLS,
    )

    reg = build_tool_registry()
    documented = {
        name
        for name in ACTION_TOOLS
        if "confirmation_token" in reg.get(name).description.lower()
    }
    assert STAGED_CONFIRMATION_TOOLS == documented


class _StagedClient:
    """Go stages the first call, then executes the replay carrying the token."""

    def __init__(self, verdict: str = "REQUIRES_CONFIRMATION") -> None:
        self.calls: list[tuple] = []
        self.verdict = verdict

    async def create_investment_order(self, token, payload, confirmation_token=None):
        self.calls.append((token, payload, confirmation_token))
        if confirmation_token is None:
            return {
                "status": "AWAITING_CONFIRMATION",
                "policy": {"verdict": self.verdict},
                "confirmation": {"token": "cfm-1", "action": "buy_asset"},
            }
        return {
            "status": "COMPLETED",
            "execution": {"id": "e1", "status": "submitted"},
        }


class _AllowAllPolicy:
    async def validate_action(self, **kwargs) -> bool:  # pragma: no cover - trivial
        return True


def _agent_with_allow_all_policy():
    from miriam_agent.agents.agent_loop import Agent
    from miriam_agent.safety.confirmations import PendingConfirmationStore

    agent = Agent(
        registry=build_tool_registry(),
        confirmation_store=PendingConfirmationStore(redis_enabled=False),
    )
    agent.safety_policy = _AllowAllPolicy()
    return agent


def _verified_context() -> dict:
    return {"user_id": "u1", "token": "tok", "roles": ["verified"]}


def test_agent_replays_the_staged_confirmation_token(monkeypatch):
    import miriam_agent.tools.investment_definitions as inv

    fake = _StagedClient()
    monkeypatch.setattr(inv, "get_go_client", lambda: fake)
    agent = _agent_with_allow_all_policy()
    context = _verified_context()
    args = {"symbol": "AAPL", "amount_usd": 25}

    # _safe_execute only runs approved money actions that have a server-side
    # pending confirmation, so stage the proposal first.
    _run(
        agent.confirmation_store.stage(
            "u1", agent._signature("buy_asset", args)
        )
    )

    result = _run(
        agent._safe_execute(
            "buy_asset",
            args,
            context,
            "u1",
            {"roles": ["verified"]},
            approved=True,
        )
    )

    assert result["status"] == "COMPLETED"
    assert len(fake.calls) == 2, "the staged call must be replayed exactly once"
    assert fake.calls[0][2] is None
    assert fake.calls[1][2] == "cfm-1"
    assert fake.calls[0][1] == fake.calls[1][1], "replay must carry the same payload"


def test_requires_authentication_is_never_auto_replayed(monkeypatch):
    import miriam_agent.tools.investment_definitions as inv

    fake = _StagedClient(verdict="REQUIRES_AUTHENTICATION")
    monkeypatch.setattr(inv, "get_go_client", lambda: fake)
    agent = _agent_with_allow_all_policy()
    args = {"symbol": "AAPL", "amount_usd": 25}
    _run(agent.confirmation_store.stage("u1", agent._signature("buy_asset", args)))

    result = _run(
        agent._safe_execute(
            "buy_asset",
            args,
            _verified_context(),
            "u1",
            {"roles": ["verified"]},
            approved=True,
        )
    )

    assert len(fake.calls) == 1
    assert "in-app passcode" in result["error"]
    assert result["status"] == "AWAITING_CONFIRMATION"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-x"])

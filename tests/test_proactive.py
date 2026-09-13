"""Tests for the proactive analyst and the tool-loop safety fix.

Covered:
  - SafetyPolicy allowlist matches the LIVE registry (phantom legacy names
    are rejected, real money tools allowed)
  - The agent loop's _safe_execute passes financial_profile and honors the
    policy boolean (no more TypeError on every tool call)
  - Analyst JSON parsing / normalization
  - Analyst reaches out on a real snapshot, stays quiet on garbage
  - Proactive dedupe state (in-process fallback)

Note: tests follow the repo convention of sync wrappers around
``asyncio.get_event_loop().run_until_complete(...)`` so the shared session
event loop is never torn down (pytest-asyncio loop teardown breaks the
other suites' use of ``get_event_loop``).
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# -----------------------------------------------------------------------
# Safety policy allowlist must match the live registry
# -----------------------------------------------------------------------


def test_safety_policy_allowlist_matches_live_registry():
    from miriam_agent.safety.policy import SafetyPolicy
    from miriam_agent.tools import build_tool_registry

    registry = build_tool_registry()
    registry_names = set(registry.list_names())
    policy = SafetyPolicy()

    async def allowed(name: str) -> bool:
        return await policy._is_action_allowed(name)

    # Every read-only live tool is allowed.
    read_only = {
        "get_balance",
        "get_transactions",
        "get_spending_summary",
        "analyze_portfolio",
        "get_financial_plan",
        "budget_advice",
        "search_memory",
        "lookup_recipient",
        "list_automations",
        "list_obligations",
        "list_scheduled_investments",
        "list_bill_beneficiaries",
        "get_cash_flow_forecast",
        "get_financial_health",
        "create_obligation",
        "mark_obligation_paid",
    }
    assert read_only <= registry_names
    for name in read_only:
        assert _run(allowed(name)) is True, name

    # Live money / lasting-behavior tools are allowed (staged + idempotent + RBAC).
    for name in {
        "send_money",
        "execute_investment",
        "transfer_stash_to_spending",
        "transfer_spending_to_stash",
        "create_automation",
        "update_automation",
        "delete_automation",
        "create_scheduled_investment",
        "pause_scheduled_investment",
        "resume_scheduled_investment",
    }:
        assert _run(allowed(name)) is True, name

    # Legacy phantom names must be denied (they don't exist anymore).
    for name in (
        "transfer_funds",
        "withdraw_funds",
        "deposit_funds",
        "execute_strategy",
    ):
        assert _run(allowed(name)) is False, name

    # Unknown tools are denied.
    assert _run(allowed("nonexistent_tool")) is False


def test_safety_policy_validate_action_accepts_financial_profile():
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()

    async def run():
        # Real read-only tool: must pass the full signature including
        # financial_profile (the argument that used to crash the agent loop).
        return await policy.validate_action(
            tool_name="get_balance",
            arguments={},
            user_id="u-1",
            financial_profile={"name": "Test"},
        )

    assert _run(run()) is True


# -----------------------------------------------------------------------
# Agent loop: _safe_execute honors the policy and returns tool results
# -----------------------------------------------------------------------


class MockProvider:
    """Minimal LLM double honoring the LLMProvider interface."""

    model = "mock-v1"

    def __init__(self, content: str, tool_calls=None):
        self._content = content
        self._tool_calls = tool_calls or []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        from miriam_agent.agents.llm import LLMResponse

        return LLMResponse(
            content=self._content,
            tool_calls=self._tool_calls,
            model=self.model,
        )

    async def stream(self, messages, tools=None, temperature=None, max_tokens=None):
        yield {"type": "error", "message": "not implemented in test"}

    def cost_estimate(self, usage):
        return 0.0


def test_agent_loop_runs_readonly_tool_without_typeerror(monkeypatch):
    """Regression: _safe_execute used to crash on the missing financial_profile,
    producing a swallowed TypeError tool result. Now it executes for real."""
    import json

    from miriam_agent.agents.agent_loop import Agent
    from miriam_agent.agents.base import AgentConfig

    # Stub the Go client singleton so get_balance returns clean data.
    from miriam_agent.integrations import go_client as gc
    from miriam_agent.safety.policy import SafetyPolicy
    from miriam_agent.tools import ensure_registered

    class FakeGoClient:
        def __init__(self):
            self.balance_calls = 0

        async def get_balances(self, token):
            self.balance_calls += 1
            return {"wallets": [{"wallet_type": "spend", "balance": "420.00"}]}

    fake = FakeGoClient()
    monkeypatch.setattr(gc, "_client", fake)

    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "get_balance",
            "arguments": json.dumps({}),
        },
    }

    provider = MockProvider(
        content="Your spend wallet is at 420.",
        tool_calls=[tool_call],
    )

    async def run():
        registry = ensure_registered()
        agent = Agent(
            registry=registry,
            provider=provider,
            safety_policy=SafetyPolicy(),
            config=AgentConfig(
                name="financial_agent",
                tools=registry.list_names(),
                system_prompt="Miriam Financial Agent",
            ),
        )
        return await agent.run(
            user_id="u-1",
            token="tok",
            message="what's my balance?",
            user_context={"roles": ["user"]},
        )

    result = _run(run())
    assert not result.requires_confirmation
    # The money of the test: the tool ran (its result was fed back) rather
    # than erroring with TypeError inside _safe_execute or being blocked by
    # the safety policy. A blocked call would never reach the handler.
    assert fake.balance_calls > 0
    tool_names = [c["name"] for c in result.tool_calls]
    assert "get_balance" in tool_names


# -----------------------------------------------------------------------
# Analyst parsing
# -----------------------------------------------------------------------


def test_extract_json_plain():
    from miriam_agent.proactive.analyst import extract_json

    data = extract_json(
        '{"should_reach_out": true, "priority": "high", "message": "hi"}'
    )
    assert data is not None
    assert data["should_reach_out"] is True


def test_extract_json_fenced_and_noisy():
    from miriam_agent.proactive.analyst import extract_json

    data = extract_json(
        'Sure! Here you go:\n```json\n{"should_reach_out": false}\n```\n'
        "Hope this helps."
    )
    assert data is not None
    assert data["should_reach_out"] is False


def test_extract_json_garbage_returns_none():
    from miriam_agent.proactive.analyst import extract_json

    assert extract_json("") is None
    assert extract_json("sorry, I cannot help with that today") is None


def test_normalize_priority():
    from miriam_agent.proactive.analyst import normalize_priority

    assert normalize_priority("HIGH") == "high"
    assert normalize_priority("CRITICAL") == "low"
    assert normalize_priority(None) == "low"


# -----------------------------------------------------------------------
# Analyst behavior
# -----------------------------------------------------------------------


class FakeState:
    def __init__(self):
        self.marked = []

    async def should_stay_quiet(self, user_id, priority, message):
        return False

    async def mark(self, user_id, priority, message):
        self.marked.append((user_id, priority, message))


class FakeGoAgentClient:
    async def get_balances(self, token):
        return {"wallets": [{"wallet_type": "spend", "balance": "120.00"}]}

    async def get_transactions(self, token, limit=20):
        return [
            {"amount": "85.00", "merchant": "Gym", "category": "fitness"},
            {"amount": "142.00", "merchant": "Groceries", "category": "groceries"},
        ]

    async def get_spending_summary(self, token, period="month"):
        return {
            "total_spent": "227.00",
            "top_categories": [{"category": "groceries", "amount": "142.00"}],
        }

    async def get_upcoming_bills(self, token):
        return []

    async def get_financial_health(self, token):
        return {"score": 72}

    async def get_user_profile(self, token):
        return {"profile": {"name": "Test User"}}

    def __getattr__(self, name):
        async def _missing(*args, **kwargs):
            return {}

        return _missing


def test_analyst_reaches_out_on_real_snapshot(monkeypatch):
    from miriam_agent.integrations import go_client as gc
    from miriam_agent.proactive import analyst

    monkeypatch.setattr(gc, "_client", FakeGoAgentClient())

    response = (
        '{"should_reach_out": true, "priority": "high", "category": "cashflow", '
        '"message": "Your spend wallet is down to 120 and groceries took 142 this '
        'month. Want me to park a little into stash so you are never left short?", '
        '"reason": "Low buffer"}'
    )

    class Provider:
        model = "mock"

        async def complete(
            self, messages, tools=None, temperature=None, max_tokens=None
        ):
            from miriam_agent.agents.llm import LLMResponse

            return LLMResponse(content=response, model=self.model)

        async def stream(self, *args, **kwargs):
            yield {}

        def cost_estimate(self, usage):
            return 0.0

    monkeypatch.setattr(analyst, "get_llm_provider", lambda: Provider())

    outcome = _run(
        analyst.analyze_finances(
            user_id="u-1",
            token="tok",
            state=FakeState(),
        )
    )
    assert outcome.should_reach_out is True
    assert outcome.priority == "high"
    assert outcome.category == "cashflow"
    assert "120" in outcome.message
    assert outcome.message.strip()


def test_analyst_stays_quiet_on_garbage(monkeypatch):
    from miriam_agent.integrations import go_client as gc
    from miriam_agent.proactive import analyst

    monkeypatch.setattr(gc, "_client", FakeGoAgentClient())

    class Provider:
        model = "mock"

        async def complete(
            self, messages, tools=None, temperature=None, max_tokens=None
        ):
            from miriam_agent.agents.llm import LLMResponse

            return LLMResponse(
                content="This isn't valid JSON at all.", model=self.model
            )

        async def stream(self, *args, **kwargs):
            yield {}

        def cost_estimate(self, usage):
            return 0.0

    monkeypatch.setattr(analyst, "get_llm_provider", lambda: Provider())

    outcome = _run(
        analyst.analyze_finances(
            user_id="u-1",
            token="tok",
            state=FakeState(),
        )
    )
    assert outcome.should_reach_out is False
    assert outcome.message == ""


# -----------------------------------------------------------------------
# Dedupe / cadence state
# -----------------------------------------------------------------------


def test_state_dedupe_and_interval():
    from miriam_agent.proactive.state import ProactiveStateStore

    store = ProactiveStateStore(min_interval_hours=12.0, use_settings_url=False)

    # Nothing stamped -> reach out.
    assert _run(store.should_stay_quiet("u-1", "medium", "Hello money friend")) is False
    _run(store.mark("u-1", "medium", "Hello money friend"))

    # Same message, recent -> stay quiet.
    assert _run(store.should_stay_quiet("u-1", "medium", "Hello money friend")) is True

    # Different message, same interval -> stay quiet.
    assert _run(store.should_stay_quiet("u-1", "medium", "Different message")) is True

    # High priority always allowed through (except the exact dedupe).
    assert _run(store.should_stay_quiet("u-1", "high", "SERIOUS thing")) is False

    # Keys are per-user.
    assert _run(store.should_stay_quiet("u-2", "medium", "Hello money friend")) is False


def test_message_hash_is_stable():
    from miriam_agent.proactive.state import message_hash

    assert message_hash("same message") == message_hash("  same message  ")
    assert message_hash("a") != message_hash("b")

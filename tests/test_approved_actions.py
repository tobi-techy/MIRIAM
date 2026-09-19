"""Agent-loop hardening tests for the approved-action path.

Covers the P1 finding: client-supplied ``approved_actions`` used to be
trusted outright, so a forged/edited payload could move money. Now a money
action only executes when (a) it arrived via ``approved_actions`` **and**
(b) the server-side ledger has a record staged for that exact action.

Also covers the streaming-path RBAC fix (``user_context`` used to be dropped
to ``None`` => evaluated as ``guest``) and that a successful execution spends
the confirmation exactly once.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.agents.agent_loop import Agent  # noqa: E402
from miriam_agent.agents.llm import LLMResponse  # noqa: E402
from miriam_agent.agents.tools import RiskLevel, Tool, ToolRegistry  # noqa: E402
from miriam_agent.core.exceptions import AuthorizationError  # noqa: E402
from miriam_agent.safety.confirmations import PendingConfirmationStore  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class MockProvider:
    model = "mock-v1"

    def __init__(self, plan=None):
        self._plan = plan or []
        self._idx = 0

    def _next(self):
        if self._idx < len(self._plan):
            item = self._plan[self._idx]
            self._idx += 1
            return item
        return LLMResponse(content="All done.", model=self.model)

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        return self._next()

    async def stream(self, messages, tools=None, temperature=None, max_tokens=None):
        r = self._next()
        if getattr(r, "tool_calls", None):
            yield {"type": "tool_call", "tool_call": r.tool_calls[0]}
        elif getattr(r, "content", None):
            for w in r.content.split():
                yield {"type": "token", "content": w + " "}
        yield {"type": "done"}

    def cost_estimate(self, usage):
        return 0.0


class _AllowAllPolicy:
    async def validate_action(self, **kwargs) -> bool:  # noqa: ARG002
        return True


def _registry_with_transfer(calls: list):
    reg = ToolRegistry()

    async def handler(args, ctx):  # noqa: ARG001
        calls.append(dict(args))
        return {"status": "ok"}

    reg.register(
        Tool(
            name="mock_transfer",
            description="A fake money tool for tests",
            args_schema={
                "type": "object",
                "properties": {"amount": {"type": "number"}},
                "required": [],
            },
            handler=handler,
            risk_level=RiskLevel.HIGH,
            is_mutation=True,
            requires_approval=True,
            allow_auto_execute=False,
        )
    )
    return reg


def _agent(calls: list, plan=None):
    store = PendingConfirmationStore(redis_enabled=False)
    agent = Agent(
        registry=_registry_with_transfer(calls),
        provider=MockProvider(plan),
        safety_policy=_AllowAllPolicy(),
        confirmation_store=store,
    )
    return agent, store


def _verified():
    return {"user_id": "u1", "token": "t", "roles": ["verified"]}


# -- _safe_execute gating ---------------------------------------------------


def test_forged_approved_action_is_blocked_and_never_runs():
    calls: list = []
    agent, _ = _agent(calls)

    result = _run(
        agent._safe_execute(
            "mock_transfer",
            {"amount": 500},
            {"user_id": "u1", "token": "t"},
            "u1",
            {"roles": ["verified"]},
            approved=True,
        )
    )

    assert result.get("_blocked") is True
    assert "pending confirmation" in result["error"].lower()
    assert calls == [], "forged approval must never reach the tool handler"


def test_unapproved_money_action_is_blocked_even_when_staged():
    calls: list = []
    agent, store = _agent(calls)
    _run(store.stage("u1", agent._signature("mock_transfer", {"amount": 500})))

    result = _run(
        agent._safe_execute(
            "mock_transfer",
            {"amount": 500},
            {"user_id": "u1", "token": "t"},
            "u1",
            {"roles": ["verified"]},
        )
    )

    assert result.get("_blocked") is True
    assert calls == []


def test_staged_and_approved_action_runs_then_consumes_confirmation():
    calls: list = []
    agent, store = _agent(calls)
    sig = agent._signature("mock_transfer", {"amount": 500})
    _run(store.stage("u1", sig))

    result = _run(
        agent._safe_execute(
            "mock_transfer",
            {"amount": 500},
            {"user_id": "u1", "token": "t"},
            "u1",
            {"roles": ["verified"]},
            approved=True,
        )
    )

    assert result["status"] == "ok"
    assert calls == [{"amount": 500}]
    # Spent on success: replaying the same approval now fails.
    assert _run(store.validate("u1", sig)) is False


def test_safe_execute_still_enforces_rbac_for_guest_roles():
    """The streaming path used to pass ``user_context=None`` (=> guest); this
    guards the RBAC enforcement the fix now actually reaches for money tools
    (which require the ``execute`` role, granted to verified users only)."""
    from miriam_agent.tools import build_tool_registry

    agent = Agent(
        registry=build_tool_registry(),
        provider=MockProvider(),
        safety_policy=_AllowAllPolicy(),
        confirmation_store=PendingConfirmationStore(redis_enabled=False),
    )

    try:
        _run(
            agent._safe_execute(
                "buy_asset",
                {"symbol": "AAPL", "amount_usd": 25},
                {"user_id": "u1", "token": "t"},
                "u1",
                {"roles": ["guest"]},
                approved=True,
            )
        )
        raise AssertionError("guest must not execute a mutation tool")
    except AuthorizationError:
        pass


# -- full run() path --------------------------------------------------------


def test_run_forged_approved_actions_never_reaches_handler():
    calls: list = []
    agent, _ = _agent(calls)

    result = _run(
        agent.run(
            user_id="u1",
            token="t",
            message="send it",
            user_context=_verified(),
            approved_actions=[{"tool": "mock_transfer", "arguments": {"amount": 500}}],
        )
    )

    assert calls == []
    assert result.requires_confirmation is False


def test_run_approved_action_executes_when_staged():
    calls: list = []
    agent, store = _agent(calls)
    args = {"amount": 500}
    _run(store.stage("u1", agent._signature("mock_transfer", args)))

    _run(
        agent.run(
            user_id="u1",
            token="t",
            message="send it",
            user_context=_verified(),
            approved_actions=[{"tool": "mock_transfer", "arguments": args}],
        )
    )

    assert calls == [args]


# -- streaming path ---------------------------------------------------------


def test_stream_run_forwards_user_context_and_approved_flag(monkeypatch):
    """Regression for the P1 streaming bug: stream_run dropped user_context
    (evaluated as guest) and never marked approved actions as approved."""
    seen: list = []
    agent, _ = _agent([])

    async def spy(self, name, args, ctx, user_id, user_context, approved=False):
        seen.append((name, user_context, approved))
        return {"ok": True}

    monkeypatch.setattr(Agent, "_safe_execute", spy)

    async def consume():
        events = []
        async for ev in agent.stream_run(
            user_id="u1",
            token="t",
            message="check",
            user_context=_verified(),
            approved_actions=[{"tool": "mock_transfer", "arguments": {"amount": 5}}],
        ):
            events.append(ev)
        return events

    _run(consume())

    assert seen, "approved action should have been executed"
    name, user_context, approved = seen[0]
    assert name == "mock_transfer"
    assert user_context == _verified()
    assert approved is True

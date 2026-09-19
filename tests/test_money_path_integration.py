"""Integration tests for the money path: Agent <-> SafetyPolicy <-> tool handler.

These exist because every other money-path test injects an ``_AllowAllPolicy``
stub (``tests/test_approved_actions.py``, ``tests/test_investment_tools.py``),
leaving the seam between the agent loop and the *real* ``SafetyPolicy``
untested.

That seam had a live defect: ``Agent._safe_execute`` called
``validate_action`` without forwarding its ``approved`` decision, and
``validate_action`` denies anything that requires approval when ``approved``
is falsy. Every money action was therefore refused before the Go backend was
ever called, even with a valid, ledger-backed user confirmation.

These tests use the real policy (with only the audit-log read stubbed, so no
database is needed) and a real registry whose money handler is swapped for a
recorder, then assert on whether the handler actually ran.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.agents.agent_loop import Agent  # noqa: E402
from miriam_agent.agents.llm import LLMResponse  # noqa: E402
from miriam_agent.core.exceptions import AuthorizationError  # noqa: E402
from miriam_agent.safety.confirmations import PendingConfirmationStore  # noqa: E402
from miriam_agent.safety.policy import SafetyPolicy  # noqa: E402
from miriam_agent.tools import build_tool_registry  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class MockProvider:
    """Minimal LLM stand-in: replays a fixed plan of responses."""

    model = "mock-v1"

    def __init__(self, plan=None):
        self._plan = list(plan or [])
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
            yield {"type": "token", "content": r.content}
        yield {"type": "done"}

    def cost_estimate(self, usage):
        return 0.0


class _PolicyNoAuditHistory(SafetyPolicy):
    """Real policy, minus the audit-log DB read.

    The audit trail is unavailable in a unit test and ``_check_limits`` fails
    closed when it cannot read it. Stubbing only that read isolates the
    approval branch, so the assertions below are about approval handling
    rather than database availability.
    """

    async def _get_recent_activities(self, user_id, timeframe_hours=24, strict=False):
        return []


@pytest.fixture(autouse=True)
def _restore_registry_handlers():
    """The tool registry is a process-wide singleton; restore it afterwards."""
    registry = build_tool_registry()
    snapshot = {t.name: t.handler for t in registry}
    yield
    for name, handler in snapshot.items():
        registry.get(name).handler = handler


def _registry_with_recorder(tool_name: str, calls: list):
    """Real registry, with one tool's handler swapped for a recorder."""
    registry = build_tool_registry()
    tool = registry.get(tool_name)
    assert tool is not None, f"{tool_name} must be registered"

    async def _record(args, ctx):
        calls.append(dict(args))
        return {"status": "ok", "recorded": True}

    tool.handler = _record
    return registry


def _agent(tool_name: str, calls: list):
    store = PendingConfirmationStore(redis_enabled=False)
    agent = Agent(
        registry=_registry_with_recorder(tool_name, calls),
        provider=MockProvider(),
        safety_policy=_PolicyNoAuditHistory(),
        confirmation_store=store,
    )
    return agent, store


_VERIFIED = {"user_id": "u1", "roles": ["verified"]}


# ---------------------------------------------------------------------------
# The approved path reaches the backend
# ---------------------------------------------------------------------------


def test_approved_send_money_reaches_backend():
    """The headline regression: an approved money action must run."""
    calls: list = []
    agent, store = _agent("send_money", calls)
    args = {"to": "alice@rail.io", "amount": 10.0}
    _run(store.stage("u1", agent._signature("send_money", args)))

    result = _run(
        agent._safe_execute(
            "send_money",
            args,
            {"user_id": "u1", "token": "t"},
            "u1",
            _VERIFIED,
            approved=True,
        )
    )

    assert calls == [args], "the approved action must reach the tool handler"
    assert result.get("_blocked") is None
    assert result["status"] == "ok"


def test_approved_transfer_reaches_backend_end_to_end():
    """Same guarantee through the full ``Agent.run`` approval replay."""
    calls: list = []
    agent, store = _agent("transfer_spending_to_stash", calls)
    args = {"amount": 25.0}
    _run(store.stage("u1", agent._signature("transfer_spending_to_stash", args)))

    result = _run(
        agent.run(
            user_id="u1",
            token="t",
            message="move 25 to stash",
            user_context=_VERIFIED,
            approved_actions=[
                {"tool": "transfer_spending_to_stash", "arguments": args}
            ],
        )
    )

    assert calls == [args]
    assert result.requires_confirmation is False


def test_policy_allows_the_same_action_once_approved():
    """The policy itself was never the problem: only the flag matters."""
    policy = _PolicyNoAuditHistory()
    kwargs = dict(
        tool_name="send_money",
        arguments={"to": "alice@rail.io", "amount": 10.0},
        user_id="u1",
        financial_profile=None,
    )

    assert _run(policy.validate_action(**kwargs)) is False
    assert _run(policy.validate_action(**kwargs, approved=True)) is True


# ---------------------------------------------------------------------------
# The unapproved path still refuses
# ---------------------------------------------------------------------------


def test_unapproved_money_action_is_refused_and_never_runs():
    calls: list = []
    agent, _ = _agent("send_money", calls)

    result = _run(
        agent._safe_execute(
            "send_money",
            {"to": "alice@rail.io", "amount": 10.0},
            {"user_id": "u1", "token": "t"},
            "u1",
            _VERIFIED,
        )
    )

    assert calls == [], "an unapproved money action must never reach the handler"
    assert result.get("_blocked") is True


def test_forged_approval_without_a_staged_record_is_refused():
    """approved=True is not enough: the server-side ledger must agree.

    The amount is deliberately inside the per-transaction cap so the refusal
    can only come from the missing staging record, not from a limit.
    """
    calls: list = []
    agent, _ = _agent("send_money", calls)

    result = _run(
        agent._safe_execute(
            "send_money",
            {"to": "attacker@rail.io", "amount": 500.0},
            {"user_id": "u1", "token": "t"},
            "u1",
            _VERIFIED,
            approved=True,
        )
    )

    assert calls == []
    assert result.get("_blocked") is True
    assert "pending confirmation" in result["error"].lower()


def test_guest_cannot_execute_a_money_tool_even_when_approved():
    calls: list = []
    agent, store = _agent("send_money", calls)
    args = {"to": "alice@rail.io", "amount": 10.0}
    _run(store.stage("u1", agent._signature("send_money", args)))

    with pytest.raises(AuthorizationError):
        _run(
            agent._safe_execute(
                "send_money",
                args,
                {"user_id": "u1", "token": "t"},
                "u1",
                {"roles": ["guest"]},
                approved=True,
            )
        )
    assert calls == []


def test_confirmation_is_spent_after_one_execution():
    """A replayed approval must not move the same money twice."""
    calls: list = []
    agent, store = _agent("send_money", calls)
    args = {"to": "alice@rail.io", "amount": 10.0}
    sig = agent._signature("send_money", args)
    _run(store.stage("u1", sig))

    first = _run(
        agent._safe_execute(
            "send_money", args, {"user_id": "u1", "token": "t"}, "u1", _VERIFIED,
            approved=True,
        )
    )
    second = _run(
        agent._safe_execute(
            "send_money", args, {"user_id": "u1", "token": "t"}, "u1", _VERIFIED,
            approved=True,
        )
    )

    assert first["status"] == "ok"
    assert second.get("_blocked") is True, "the confirmation was already spent"
    assert calls == [args], "the handler ran exactly once"


# ---------------------------------------------------------------------------
# State-changing tools that used to auto-execute with no confirmation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("create_obligation", {"name": "Netflix", "amount": 15.0}),
        ("mark_obligation_paid", {"id": "obl_123"}),
        ("save_bill_beneficiary", {"category": "airtime", "recipient": "08012345678"}),
    ],
)
def test_state_changing_tools_are_staged_and_require_approval(tool_name, args):
    """These POST/PATCH to the Go backend, so they must not run unprompted."""
    calls: list = []
    agent, _ = _agent(tool_name, calls)
    tool = agent.registry.get(tool_name)

    assert tool.is_mutation, f"{tool_name} mutates backend state"
    assert tool.requires_approval, f"{tool_name} must be user-confirmed"
    assert not tool.allow_auto_execute

    result = _run(
        agent._safe_execute(
            tool_name, dict(args), {"user_id": "u1", "token": "t"}, "u1", _VERIFIED
        )
    )

    assert calls == [], f"{tool_name} ran without a confirmation"
    assert result.get("_blocked") is True


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("create_obligation", {"name": "Netflix", "amount": 15.0}),
        ("mark_obligation_paid", {"id": "obl_123"}),
        ("save_bill_beneficiary", {"category": "airtime", "recipient": "08012345678"}),
    ],
)
def test_state_changing_tools_execute_once_approved(tool_name, args):
    """Gating them must not break them."""
    calls: list = []
    agent, store = _agent(tool_name, calls)
    _run(store.stage("u1", agent._signature(tool_name, args)))

    result = _run(
        agent._safe_execute(
            tool_name, dict(args), {"user_id": "u1", "token": "t"}, "u1", _VERIFIED,
            approved=True,
        )
    )

    assert calls == [dict(args)]
    assert result.get("_blocked") is None

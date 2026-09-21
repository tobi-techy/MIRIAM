"""Tests for the read-tool boundary in ``miriam_agent.safety.policy``.

This file used to cover the money allowlist, the daily and per-transaction
limits, the fraud-pattern heuristics and the approval workflow. All of it is
deleted, and so are those tests: the rules they covered were unreachable (the
registry holds no money tool) and ``hands/limits.py`` owns the ceilings now. A
test of a rule nobody enforces is a test that keeps dead code alive.

What is left to test is what the policy actually does: it decides whether a read
the agent loop wants to run is allowed, using the registry as the source of
truth, and it refuses anything that writes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

# A mutation the live registry never holds, standing in for the money tools that
# used to be registered here. The boundary denies a *registered* mutation, so
# testing that needs one to deny. Deliberately not named like a real money tool:
# ``build_tool_registry`` strips those by name.
SYNTHETIC_MUTATION = "probe_mutation"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@contextmanager
def synthetic_mutation() -> Iterator[None]:
    """Register a mutation for the duration of a test, then remove it again.

    The registry is a process-wide singleton, so this restores it on the way out
    rather than leaving a mutation behind for a later test to trip over.
    """
    from miriam_agent.agents.tools import RiskLevel, Tool, get_registry

    registry = get_registry()

    async def _handler(args, ctx):  # noqa: ANN001, ANN202
        return {"ok": True}

    added = registry.get(SYNTHETIC_MUTATION) is None
    if added:
        registry.register(
            Tool(
                name=SYNTHETIC_MUTATION,
                description="test-only mutation",
                args_schema={"type": "object", "properties": {}},
                handler=_handler,
                category="test",
                risk_level=RiskLevel.MEDIUM,
                is_mutation=True,
                requires_approval=True,
                allow_auto_execute=False,
            )
        )
    try:
        yield
    finally:
        if added:
            registry.unregister(SYNTHETIC_MUTATION)


# ---------------------------------------------------------------------------
# Reads are allowed
# ---------------------------------------------------------------------------


def test_a_registered_read_is_allowed():
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    for name in ("get_balance", "get_transactions", "get_money_plan"):
        assert _run(policy.validate_action(name, {}, "u-1")) is True, name


def test_the_full_call_signature_is_accepted():
    """The agent loop passes user_id and financial_profile; both must be fine."""
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    allowed = _run(
        policy.validate_action(
            tool_name="get_balance",
            arguments={},
            user_id="u-1",
            financial_profile={"name": "Test"},
        )
    )
    assert allowed is True


# ---------------------------------------------------------------------------
# Anything that writes is refused
# ---------------------------------------------------------------------------


def test_an_unregistered_tool_is_refused():
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    assert _run(policy.validate_action("nonexistent_tool", {}, "u-1")) is False


def test_a_money_tool_is_refused_by_absence():
    """No money tool is registered, so the boundary has nothing to allow."""
    from miriam_agent.safety.money_tools import MONEY_TOOL_NAMES
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    for name in sorted(MONEY_TOOL_NAMES):
        assert _run(policy.validate_action(name, {}, "u-1")) is False, name


def test_a_registered_mutation_is_refused():
    """Broader than the name list: a tool that writes is refused however named."""
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    with synthetic_mutation():
        assert _run(policy.validate_action(SYNTHETIC_MUTATION, {}, "u-1")) is False


# ---------------------------------------------------------------------------
# Blocked content in a read's arguments
# ---------------------------------------------------------------------------


def test_blocked_content_in_arguments_is_refused():
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    for arguments in (
        {"description": "pay a scam invoice"},
        {"category": "fraud"},
        {"description": "something ILLEGAL"},
    ):
        allowed = _run(policy.validate_action("get_balance", arguments, "u-1"))
        assert allowed is False, arguments


def test_ordinary_arguments_are_not_blocked():
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    allowed = _run(
        policy.validate_action(
            "get_transactions", {"category": "groceries", "limit": 20}, "u-1"
        )
    )
    assert allowed is True


def test_the_policy_holds_no_money_data():
    """The lists it used to load are gone; only content policy remains."""
    from miriam_agent.safety.policy import BLOCKED_CATEGORIES, SafetyPolicy

    policy = SafetyPolicy()
    assert policy.blocked_categories == BLOCKED_CATEGORIES
    for gone in (
        "money_movement_limits",
        "suspicious_patterns",
        "approval_workflow",
        "risk_scores",
    ):
        assert not hasattr(policy, gone), gone

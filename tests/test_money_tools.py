"""The canonical money-tool set must stay in sync with the tool registry.

``safety/money_tools.py`` is the single source of truth that the safety policy
allowlist, the audit-detail filter and the daily-limit filter all read. It is
curated rather than derived (so a newly registered mutation cannot silently
inherit money-movement privileges), which means the two can drift -- and they
did: the automation and scheduled-investment mutations were gated for approval
but missing from the audit and limit lists, so their amounts were never
recorded.

These tests make that drift impossible in either direction.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.safety.money_tools import MONEY_TOOLS, TRANSFER_TOOLS  # noqa: E402
from miriam_agent.tools import build_tool_registry  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _registered_mutations() -> set[str]:
    registry = build_tool_registry()
    return {
        tool.name for tool in registry if tool.is_mutation or tool.requires_approval
    }


def test_canonical_set_matches_the_registry_exactly():
    """A new mutation tool with no entry here (or a stale entry) fails the suite."""
    registered = _registered_mutations()
    assert registered == set(MONEY_TOOLS), (
        f"missing from MONEY_TOOLS: {sorted(registered - set(MONEY_TOOLS))}; "
        f"not registered: {sorted(set(MONEY_TOOLS) - registered)}"
    )


def test_every_canonical_tool_is_fully_gated():
    """Being in MONEY_TOOLS means: staged, approval-required, never auto-run."""
    registry = build_tool_registry()
    for name in sorted(MONEY_TOOLS):
        tool = registry.get(name)
        assert tool is not None, f"{name} is not registered"
        assert tool.is_mutation, f"{name} must be marked a mutation"
        assert tool.requires_approval, f"{name} must require approval"
        assert not tool.allow_auto_execute, f"{name} must never auto-execute"
        assert tool.risk_level.value in {"medium", "high", "critical"}, name


def test_transfer_tools_are_a_subset_of_money_tools():
    """Anything counted toward transfer limits must also be gated."""
    assert TRANSFER_TOOLS <= MONEY_TOOLS


def test_every_canonical_tool_passes_the_policy_allowlist():
    """The policy allowlist reads MONEY_TOOLS, so none may be denied outright."""
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    for name in sorted(MONEY_TOOLS):
        assert _run(policy._is_action_allowed(name)) is True, name


def test_read_only_tools_are_not_in_the_canonical_set():
    """The gate must not spread to reads (that would need approval to check a
    balance)."""
    registry = build_tool_registry()
    read_only = {
        tool.name
        for tool in registry
        if not tool.is_mutation and not tool.requires_approval
    }
    assert not (read_only & set(MONEY_TOOLS))

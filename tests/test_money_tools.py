"""The live registry must hold no writer of a balance, and no money rule may
live anywhere but ``hands/``.

``safety/money_tools.py`` is the single source of truth for one narrow question:
"is this tool name one of the money family?" The answer is used to strip those
names out of the live registry and to refuse one a model asks for.

Money is not a tool. ``hands/`` is the only writer of a balance and
``orchestrator.py`` is the only way in, so the tests here enforce absence, which
is the strongest form available.
"""

from __future__ import annotations

import pathlib

import pytest

from miriam_agent.safety.money_tools import MONEY_TOOL_NAMES, is_money_tool
from miriam_agent.tools import build_tool_registry

ROOT = pathlib.Path(__file__).resolve().parents[1] / "miriam_agent"


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_the_live_registry_holds_no_money_tool():
    """The strongest statement of the invariant: they are not there to call."""
    registry = build_tool_registry()
    assert sorted(set(registry.list_names()) & MONEY_TOOL_NAMES) == []


def test_the_live_registry_holds_no_mutation_at_all():
    """Broader than the name list: nothing that writes backend state is offered."""
    registry = build_tool_registry()
    mutations = [
        tool.name for tool in registry if tool.is_mutation or tool.requires_approval
    ]
    assert mutations == []


def test_no_mutation_is_ever_offered_to_a_model():
    """``llm_schemas`` is the model's whole surface, so this is the real check."""
    registry = build_tool_registry()
    offered = {schema["function"]["name"] for schema in registry.llm_schemas()}
    assert offered & MONEY_TOOL_NAMES == set()
    for name in offered:
        tool = registry.get(name)
        assert tool is not None
        assert not tool.is_mutation and not tool.requires_approval, name


def test_read_only_tools_are_still_offered():
    """The filter must not have removed the reads the agent answers from."""
    registry = build_tool_registry()
    offered = {schema["function"]["name"] for schema in registry.llm_schemas()}
    assert {"get_balance", "get_transactions"} <= offered
    # The money *plan* is a read: it computes advice and calls no rail.
    assert "get_money_plan" in offered


def test_the_forbidden_set_covers_the_names_the_design_names():
    """A regression guard on the list itself, so a rename cannot slip past it."""
    for name in (
        "send_money",
        "transfer",
        "split",
        "invest",
        "unlock",
        "lock",
        "change_track",
    ):
        assert is_money_tool(name), name


# ---------------------------------------------------------------------------
# The rules live in one place
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    ["safety/policy.py", "agents/agent_loop.py", "api/chat.py"],
)
def test_no_money_rule_survives_outside_hands(module):
    """No limits, no allowlist, no approval step, outside ``hands/``.

    The second implementation is what drifts. ``hands/limits.py`` owns the
    ceilings, ``hands/ledger.py`` owns the balances, and every other module
    either routes to them or reads the result.
    """
    source = (ROOT / module).read_text()
    for rule in (
        "MAX_DAILY_TRANSFER",
        "MAX_TRANSACTION_AMOUNT",
        "money_movement_limits",
        "approval_workflow",
        "requires_approval(",
        "suspicious_patterns",
    ):
        assert rule not in source, f"{module} still holds {rule}"


def test_the_policy_module_holds_no_money_vocabulary():
    """The one file this PR gutted, checked for the words it used to hold."""
    source = (ROOT / "safety" / "policy.py").read_text()
    for gone in (
        "MONEY_TOOLS",
        "TRANSFER_TOOLS",
        "high_risk_daily_limit",
        "daily_limit",
        "transaction_limit",
        "amount_ngn",
        "suspicious",
    ):
        assert gone not in source, gone


def test_no_module_outside_hands_decides_a_limit():
    """``hands/limits.py`` is the only place a cap is computed."""
    offenders = []
    for path in ROOT.rglob("*.py"):
        if "hands" in path.parts or "money" in path.parts:
            continue
        source = path.read_text()
        if "affordable_cap" in source or "evaluate_limits" in source:
            offenders.append(str(path.relative_to(ROOT)))
    # The orchestrator and the API may route to Hands, but must not compute one.
    for name in offenders:
        source = (ROOT / name).read_text()
        assert "def affordable_cap" not in source, name
        assert "daily_limit" not in source, name

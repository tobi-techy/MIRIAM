"""Tests for the TypeSafe tool and egress gates.

Never hits the live API: branch logic runs against recorded fixtures and the
gate wrappers use a fake client.
"""

from __future__ import annotations

import json
from pathlib import Path

from typesafe_sdk import NoulAnswer, ScoreAnswer, TypeSafeError, Usage

from miriam_agent.judgment.gates import (
    EgressBranch,
    ToolBranch,
    build_state,
    decide_egress,
    decide_tool,
    egress_gate,
    tool_gate,
)
from miriam_agent.judgment.schemas import (
    EgressJudgment,
    JudgmentState,
    ProposedTool,
    ToolDescriptor,
    ToolJudgment,
    TurnInput,
)

TOOL_GOLDEN = Path(__file__).parent / "fixtures" / "tool_golden.json"
EGRESS_GOLDEN = Path(__file__).parent / "fixtures" / "egress_golden.json"


def _tool_judgment(recorded: dict) -> ToolJudgment:
    return ToolJudgment.model_construct(
        model="jev-latest",
        usage=Usage(input_tokens=10, output_tokens=5),
        tool_is_relevant=NoulAnswer.model_construct(noul=recorded["tool_is_relevant"]),
        args_match_request=NoulAnswer.model_construct(
            noul=recorded["args_match_request"]
        ),
        args_look_complete=NoulAnswer.model_construct(
            noul=recorded["args_look_complete"]
        ),
        costly=NoulAnswer.model_construct(noul=recorded["costly"]),
        irreversible=NoulAnswer.model_construct(noul=recorded["irreversible"]),
        exceeds_user_authority=NoulAnswer.model_construct(
            noul=recorded["exceeds_user_authority"]
        ),
        user_confirmed_this_action=NoulAnswer.model_construct(
            noul=recorded.get("user_confirmed_this_action", 0.0)
        ),
    )


def _egress_judgment(recorded: dict) -> EgressJudgment:
    return EgressJudgment.model_construct(
        model="jev-latest",
        usage=Usage(input_tokens=10, output_tokens=5),
        answers_the_ask=NoulAnswer.model_construct(noul=recorded["answers_the_ask"]),
        invents_facts=NoulAnswer.model_construct(noul=recorded["invents_facts"]),
        leaks_system=NoulAnswer.model_construct(noul=recorded["leaks_system"]),
        repeats_pii=NoulAnswer.model_construct(noul=recorded["repeats_pii"]),
        echoes_user_secret=NoulAnswer.model_construct(
            noul=recorded.get("echoes_user_secret", 0.0)
        ),
        tone_fit=ScoreAnswer.model_construct(score=recorded["tone_fit"]),
        policy_violation=NoulAnswer.model_construct(noul=recorded["policy_violation"]),
    )


def _load(path: Path) -> list[dict]:
    return json.loads(path.read_text())["cases"]


# ---------------------------------------------------------------------------
# Golden sets
# ---------------------------------------------------------------------------


def test_tool_golden_set_matches_expected_branches():
    for case in _load(TOOL_GOLDEN):
        decision = decide_tool(_tool_judgment(case["recorded"]))
        assert decision.branch.value == case["expected_branch"], (
            f"{case['id']}: expected {case['expected_branch']}, "
            f"got {decision.branch.value}"
        )


def test_egress_golden_set_matches_expected_branches():
    for case in _load(EGRESS_GOLDEN):
        decision = decide_egress(_egress_judgment(case["recorded"]))
        assert decision.branch.value == case["expected_branch"], (
            f"{case['id']}: expected {case['expected_branch']}, "
            f"got {decision.branch.value}"
        )


# ---------------------------------------------------------------------------
# Tool gate (pure + wrapper)
# ---------------------------------------------------------------------------


def test_irrelevant_tool_is_rejected():
    decision = decide_tool(
        _tool_judgment(
            {
                "tool_is_relevant": 0.1,
                "args_match_request": 0.9,
                "args_look_complete": 0.9,
                "costly": 0.01,
                "irreversible": 0.01,
                "exceeds_user_authority": 0.01,
            }
        )
    )
    assert decision.branch is ToolBranch.REJECT


def test_irreversible_tool_is_blocked_above_threshold():
    decision = decide_tool(
        _tool_judgment(
            {
                "tool_is_relevant": 0.9,
                "args_match_request": 0.9,
                "args_look_complete": 0.9,
                "costly": 0.1,
                "irreversible": 0.9,
                "exceeds_user_authority": 0.1,
            }
        )
    )
    assert decision.branch is ToolBranch.BLOCK


def test_costly_tool_needs_confirmation_between_thresholds():
    decision = decide_tool(
        _tool_judgment(
            {
                "tool_is_relevant": 0.9,
                "args_match_request": 0.9,
                "args_look_complete": 0.9,
                "costly": 0.62,
                "irreversible": 0.1,
                "exceeds_user_authority": 0.1,
            }
        )
    )
    assert decision.branch is ToolBranch.CONFIRM


def test_read_tool_is_allowed():
    decision = decide_tool(
        _tool_judgment(
            {
                "tool_is_relevant": 0.98,
                "args_match_request": 0.97,
                "args_look_complete": 0.99,
                "costly": 0.01,
                "irreversible": 0.01,
                "exceeds_user_authority": 0.01,
            }
        )
    )
    assert decision.branch is ToolBranch.ALLOW


def test_confirmed_confirm_band_action_executes():
    """An action the user explicitly confirmed skips the second prompt."""
    decision = decide_tool(
        _tool_judgment(
            {
                "tool_is_relevant": 0.97,
                "args_match_request": 0.96,
                "args_look_complete": 0.97,
                "costly": 0.92,
                "irreversible": 0.2,
                "exceeds_user_authority": 0.1,
                "user_confirmed_this_action": 0.93,
            }
        )
    )
    assert decision.branch is ToolBranch.ALLOW
    assert decision.reason == "user_confirmed_this_action"


def test_confirmation_never_rescues_a_hard_block():
    """Destructive actions block even when the user said yes."""
    decision = decide_tool(
        _tool_judgment(
            {
                "tool_is_relevant": 0.95,
                "args_match_request": 0.94,
                "args_look_complete": 0.95,
                "costly": 0.05,
                "irreversible": 0.92,
                "exceeds_user_authority": 0.1,
                "user_confirmed_this_action": 0.97,
            }
        )
    )
    assert decision.branch is ToolBranch.BLOCK


class _FakeClient:
    def __init__(self, error=None):
        self._error = error

    async def system_one(self, state, questions, *, response_model=None, **kwargs):
        raise self._error


async def test_tool_gate_fails_closed_for_write_tools(monkeypatch):
    monkeypatch.setattr("miriam_agent.judgment.gates.enabled", lambda: True)
    state = JudgmentState(
        turn=TurnInput(user_text="send money"),
        proposed_tool=ProposedTool(name="send_money", args={}),
        tools=[ToolDescriptor(name="send_money", purpose="send", side_effects="write")],
    )
    decision = await tool_gate(state, client=_FakeClient(error=TypeSafeError("down")))
    assert decision.branch is ToolBranch.BLOCK
    assert decision.degraded is True


async def test_tool_gate_fails_closed_but_reads_still_run(monkeypatch):
    monkeypatch.setattr("miriam_agent.judgment.gates.enabled", lambda: True)
    state = JudgmentState(
        turn=TurnInput(user_text="balance"),
        proposed_tool=ProposedTool(name="get_balance", args={}),
        tools=[ToolDescriptor(name="get_balance", purpose="read", side_effects="read")],
    )
    decision = await tool_gate(state, client=_FakeClient(error=TypeSafeError("down")))
    assert decision.branch is ToolBranch.ALLOW
    assert decision.degraded is True


async def test_tool_gate_allows_when_disabled(monkeypatch):
    monkeypatch.setattr("miriam_agent.judgment.gates.enabled", lambda: False)
    state = JudgmentState(
        turn=TurnInput(user_text="send money"),
        proposed_tool=ProposedTool(name="send_money", args={}),
    )
    decision = await tool_gate(state)
    assert decision.branch is ToolBranch.ALLOW
    assert decision.degraded is True


# ---------------------------------------------------------------------------
# Egress gate (pure + wrapper)
# ---------------------------------------------------------------------------


def test_policy_violation_discards():
    decision = decide_egress(
        _egress_judgment(
            {
                "answers_the_ask": 0.4,
                "invents_facts": 0.1,
                "leaks_system": 0.05,
                "repeats_pii": 0.01,
                "tone_fit": 1.0,
                "policy_violation": 0.94,
            }
        )
    )
    assert decision.branch is EgressBranch.DISCARD
    assert decision.reply is not None


def test_leaks_system_discards():
    decision = decide_egress(
        _egress_judgment(
            {
                "answers_the_ask": 0.6,
                "invents_facts": 0.2,
                "leaks_system": 0.68,
                "repeats_pii": 0.01,
                "tone_fit": 1.0,
                "policy_violation": 0.5,
            }
        )
    )
    assert decision.branch is EgressBranch.DISCARD


def test_invented_facts_regenerate():
    decision = decide_egress(
        _egress_judgment(
            {
                "answers_the_ask": 0.85,
                "invents_facts": 0.88,
                "leaks_system": 0.01,
                "repeats_pii": 0.01,
                "tone_fit": 1.0,
                "policy_violation": 0.05,
            }
        )
    )
    assert decision.branch is EgressBranch.REGENERATE


def test_good_reply_sends():
    decision = decide_egress(
        _egress_judgment(
            {
                "answers_the_ask": 0.95,
                "invents_facts": 0.02,
                "leaks_system": 0.01,
                "repeats_pii": 0.01,
                "tone_fit": 1.0,
                "policy_violation": 0.01,
            }
        )
    )
    assert decision.branch is EgressBranch.SEND


def test_echoed_user_secret_discards_even_without_policy_violation():
    """The literal-echo question owns the discard, so it does not depend on
    repeats_pii or policy_violation scoring high."""
    decision = decide_egress(
        _egress_judgment(
            {
                "answers_the_ask": 0.8,
                "invents_facts": 0.1,
                "leaks_system": 0.05,
                "repeats_pii": 0.2,
                "echoes_user_secret": 0.94,
                "tone_fit": 1.0,
                "policy_violation": 0.1,
            }
        )
    )
    assert decision.branch is EgressBranch.DISCARD
    assert decision.reply is not None


async def test_egress_gate_fails_open(monkeypatch):
    monkeypatch.setattr("miriam_agent.judgment.gates.enabled", lambda: True)
    state = JudgmentState(turn=TurnInput(user_text="hi"), draft_reply="hi there")
    decision = await egress_gate(state, client=_FakeClient(error=TypeSafeError("down")))
    assert decision.branch is EgressBranch.SEND
    assert decision.degraded is True


async def test_egress_gate_sends_when_disabled(monkeypatch):
    monkeypatch.setattr("miriam_agent.judgment.gates.enabled", lambda: False)
    state = JudgmentState(turn=TurnInput(user_text="hi"), draft_reply="hi there")
    decision = await egress_gate(state)
    assert decision.branch is EgressBranch.SEND
    assert decision.degraded is True


# ---------------------------------------------------------------------------
# State builder (tool/egress extras)
# ---------------------------------------------------------------------------


def test_build_state_includes_proposed_tool_and_tool_results():
    from types import SimpleNamespace

    registry = [
        SimpleNamespace(
            name="get_balance",
            description="read",
            is_mutation=False,
            requires_approval=False,
        ),
    ]
    state = build_state(
        user_id="u-1",
        message="What's my balance?",
        user_context={"roles": ["member"]},
        registry=registry,
        proposed_tool=ProposedTool(name="get_balance", args={}),
        draft_reply="You have 500.",
        tool_results=[{"name": "get_balance", "result": {"balance": 500}}],
    )

    assert state.proposed_tool is not None
    assert state.proposed_tool.name == "get_balance"
    assert state.draft_reply == "You have 500."
    # The tool result became a tool-role history entry for grounding checks.
    assert any(h.role == "tool" and "get_balance" in h.text for h in state.history)

"""Regression tests for the JEV improvement pass.

Covers: local PII refuse when TypeSafe is disabled, tool args-completeness
reject, egress grounding parity, deterministic empty-draft regenerate, and
the tone soft-signal (which must never flip a send by itself).
"""

from __future__ import annotations

from typesafe_sdk import NoulAnswer, ScoreAnswer, Usage

from miriam_agent.judgment.gates import (
    EgressBranch,
    ToolBranch,
    build_state,
    decide_egress,
    decide_tool,
    egress_gate,
    ingress_gate,
)
from miriam_agent.judgment.schemas import EgressJudgment
from tests.test_judgment_gates import _egress_judgment, _tool_judgment


async def test_pii_refuses_when_typesafe_disabled(monkeypatch):
    monkeypatch.setattr("miriam_agent.judgment.gates.enabled", lambda: False)
    state = build_state(user_id="u", message="my password is hunter2, store it")
    assert state.pii_detected is True
    decision = await ingress_gate(state)
    assert decision.branch.value == "refuse"
    assert decision.reply


def test_incomplete_args_rejected_with_reason():
    decision = decide_tool(
        _tool_judgment(
            {
                "tool_is_relevant": 0.95,
                "args_match_request": 0.93,
                "args_look_complete": 0.1,
                "costly": 0.05,
                "irreversible": 0.05,
                "exceeds_user_authority": 0.02,
            }
        )
    )
    assert decision.branch is ToolBranch.REJECT
    assert "args_incomplete" in (decision.reason or "")


def test_egress_grounding_carries_generator_evidence():
    state = build_state(
        user_id="u",
        message="What's my balance?",
        user_context={"currency": "NGN", "monthly_income": 5000},
        memory_facts=[{"type": "goal", "content": "save 1M"}],
        financial_plan={"target": 100},
        draft_reply="You have 500.",
        tool_results=[{"name": "get_balance", "result": {"spending": 1250}}],
    )
    assert "monthly_income" in state.user_profile
    assert "save 1M" in state.memory_context
    assert "target" in state.plan_context
    assert any(h.role == "tool" for h in state.history)


async def test_empty_draft_regenerates_without_api_call(monkeypatch):
    monkeypatch.setattr("miriam_agent.judgment.gates.enabled", lambda: True)

    async def _fail(*args, **kwargs):
        raise AssertionError("JEV must not be called for an empty draft")

    monkeypatch.setattr("miriam_agent.judgment.gates.evaluate", _fail)
    state = build_state(user_id="u", message="hi", draft_reply="   ")
    decision = await egress_gate(state)
    assert decision.branch is EgressBranch.REGENERATE


def _good_egress_with_tone(tone: float):
    return _egress_judgment(
        {
            "answers_the_ask": 0.95,
            "invents_facts": 0.02,
            "leaks_system": 0.01,
            "repeats_pii": 0.01,
            "tone_fit": tone,
            "policy_violation": 0.01,
        }
    )


def test_tone_never_flips_send_but_rides_as_note():
    cold = decide_egress(_good_egress_with_tone(0.2))
    assert cold.branch is EgressBranch.SEND
    assert cold.tone_note == "cold"

    sloppy = decide_egress(_good_egress_with_tone(1.9))
    assert sloppy.branch is EgressBranch.SEND
    assert sloppy.tone_note == "sloppy"

    fine = decide_egress(_good_egress_with_tone(1.0))
    assert fine.branch is EgressBranch.SEND
    assert fine.tone_note is None

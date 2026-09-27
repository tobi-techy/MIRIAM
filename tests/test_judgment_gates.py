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
from miriam_agent.judgment.state import build_state

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


def test_incomplete_tool_args_are_rejected():
    """Completeness is an execution precondition, not a logging side note."""
    decision = decide_tool(
        _tool_judgment(
            {
                "tool_is_relevant": 0.95,
                "args_match_request": 0.95,
                "args_look_complete": 0.1,
                "costly": 0.9,
                "irreversible": 0.1,
                "exceeds_user_authority": 0.1,
            }
        )
    )

    assert decision.branch is ToolBranch.REJECT
    assert decision.reason == "args_incomplete"


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


def test_extreme_tone_rides_as_a_note_without_blocking_send():
    decision = decide_egress(
        _egress_judgment(
            {
                "answers_the_ask": 0.95,
                "invents_facts": 0.02,
                "leaks_system": 0.01,
                "repeats_pii": 0.01,
                "echoes_user_secret": 0.01,
                "tone_fit": 0.2,
                "policy_violation": 0.01,
            }
        )
    )

    assert decision.branch is EgressBranch.SEND
    assert decision.tone_note == "cold"


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


def test_build_state_includes_egress_grounding_sources():
    state = build_state(
        user_id="u-1",
        message="How am I doing?",
        user_context={"balances": {"spendable": 500}, "roles": ["member"]},
        draft_reply="You have 500 spendable and a 1,000 savings goal.",
        memory_facts=[{"type": "goal", "content": "Save 1,000 this year"}],
        financial_plan={"savings_target": 1000},
    )

    sources = {item.source for item in state.supporting_context}
    assert "profile" in sources
    assert "memory:goal" in sources
    assert "plan" in sources


def test_ingress_state_does_not_carry_egress_grounding():
    state = build_state(
        user_id="u-1",
        message="Hi",
        user_context={"balances": {"spendable": 500}, "roles": ["member"]},
    )

    assert state.supporting_context == []


async def test_streaming_emits_egress_correction_when_reply_is_rewritten(monkeypatch):
    from miriam_agent.agents.agent_loop import Agent
    from miriam_agent.tools import build_tool_registry

    class _Provider:
        async def stream(self, *, messages, tools, temperature, max_tokens):
            yield {"type": "token", "content": "unsafe "}
            yield {"type": "token", "content": "draft"}
            yield {"type": "done"}

        async def complete(self, **kwargs):  # pragma: no cover - not reached
            raise AssertionError("regeneration should not run")

    async def fake_apply_egress(self, **kwargs):
        assert kwargs["draft"] == "unsafe draft"
        assert kwargs["memory_facts"] == [
            {"type": "goal", "content": "Save 1,000 this year"}
        ]
        assert kwargs["financial_plan"] == {"savings_target": 1000}
        return "safe reply"

    monkeypatch.setattr("miriam_agent.agents.agent_loop.typesafe_enabled", lambda: True)
    monkeypatch.setattr(Agent, "_apply_egress", fake_apply_egress)

    agent = Agent(registry=build_tool_registry(), provider=_Provider())
    events = [
        event
        async for event in agent.stream_run(
            user_id="u-1",
            token="tok",
            message="How am I doing?",
            memory_facts=[{"type": "goal", "content": "Save 1,000 this year"}],
            financial_plan={"savings_target": 1000},
        )
    ]

    assert [e for e in events if e["type"] == "token"] == [
        {"type": "token", "content": "unsafe "},
        {"type": "token", "content": "draft"},
    ]
    assert {e["content"] for e in events if e["type"] == "egress_correction"} == {
        "safe reply"
    }
    assert events[-1] == {"type": "done", "content": "safe reply"}


def test_build_state_populates_plan_and_entitlements_for_tool_authority():
    state = build_state(
        user_id="u-1",
        message="Can I rebalance?",
        user_context={
            "roles": ["member"],
            "plan": "pro",
            "entitlements": ["rebalance"],
        },
    )

    assert state.user.plan == "pro"
    assert state.user.known_flags == ["member", "rebalance"]


def test_claim_extraction_keeps_material_financial_claims():
    from miriam_agent.judgment.egress_claims import extract_claims

    claims = extract_claims(
        "You have 1,250 in spendable. Nice work. "
        "You paid Ada 500 and your savings rate grew by 3%."
    )

    assert len(claims) == 2
    assert "1,250" in claims[0]
    assert "3%" in claims[1]


def test_claim_catalog_builds_one_question_per_material_claim():
    from miriam_agent.judgment.egress_claims import build_claim_catalog

    catalog, checks = build_claim_catalog("You have 1,250. You paid Ada 500.")

    assert len(checks) == 2
    assert set(catalog.questions) == {check.question_id for check in checks}
    assert all(
        getattr(catalog.response_model, "model_fields")[check.question_id]
        for check in checks
    )


async def test_egress_gate_regenerates_when_a_claim_is_unsupported(monkeypatch):
    from miriam_agent.judgment.egress_claims import (
        build_claim_catalog,
        unsupported_claims,
    )

    monkeypatch.setattr("miriam_agent.judgment.gates.enabled", lambda: True)
    state = build_state(
        user_id="u-1",
        message="What's my balance?",
        draft_reply="You have 1,250 in spendable. You paid Ada 500.",
        tool_results=[{"name": "get_balance", "result": {"spendable": 1250}}],
    )
    holistic = _egress_judgment(
        {
            "answers_the_ask": 0.95,
            "invents_facts": 0.05,
            "leaks_system": 0.01,
            "repeats_pii": 0.01,
            "echoes_user_secret": 0.01,
            "tone_fit": 1.0,
            "policy_violation": 0.01,
        }
    )
    catalog, checks = build_claim_catalog(state.draft_reply or "")
    claim_response = catalog.response_model.model_construct(
        model="jev-latest",
        usage=Usage(input_tokens=5, output_tokens=2),
        **{
            checks[0].question_id: NoulAnswer.model_construct(noul=0.95),
            checks[1].question_id: NoulAnswer.model_construct(noul=0.05),
        },
    )

    class _ClaimClient:
        async def system_one(self, state, questions, *, response_model=None, **kwargs):
            if response_model is EgressJudgment:
                return holistic
            return claim_response

    decision = await egress_gate(state, client=_ClaimClient())

    assert decision.branch is EgressBranch.REGENERATE
    assert unsupported_claims(claim_response, checks, threshold=0.6) == [
        checks[0].claim
    ]

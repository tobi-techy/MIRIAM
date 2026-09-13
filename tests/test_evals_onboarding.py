"""Behavioral evals and the explicit state machine.

The onboarding behavior is not just unit-tested: recorded/conversation turns
are linted against the personality spec's rules (offline, deterministic), the
trace fixture must pass a strict threshold, and the stage/intent state machine
is exercised exhaustively for every legal intent in every stage, plus a table
completeness check so a missing edge can never silently become a default.

Note: tests follow the repo convention of sync wrappers around
``asyncio.get_event_loop().run_until_complete(...)``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _user(uid: str = "u-1"):
    return type("U", (), {"id": uid})()


# -----------------------------------------------------------------------
# Event linter / spec quality
# -----------------------------------------------------------------------


def test_scenarios_pass_spec_linter():
    from miriam_agent.onboarding.evals import SCENARIOS, run_replay

    for name, turns in SCENARIOS:
        for result in run_replay(turns):
            assert result["ok"], (name, result["violations"], result["reply"])


def test_trace_replay_passes_threshold():
    from miriam_agent.onboarding import evals

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fixture = os.path.join(root, "tests", "evals", "fixtures", "traces.jsonl")
    result = evals.evaluate_trace_file(fixture, threshold=1.0)
    assert result["total"] > 0
    assert result["ok"], result


def test_trace_replay_has_no_failed_turns():
    from miriam_agent.onboarding import evals

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fixture = os.path.join(root, "tests", "evals", "fixtures", "traces.jsonl")
    result = evals.evaluate_trace_file(fixture, threshold=1.0)
    assert [r for r in result["results"] if not r["ok"]] == []


# -----------------------------------------------------------------------
# State machine helpers (service-flow drivers)
# -----------------------------------------------------------------------


@dataclass
class FakeMemory:
    entries: list = field(default_factory=list)

    async def store_memory(self, user_id, memory_type, content, metadata=None):
        self.entries.append(
            {
                "user_id": user_id,
                "type": memory_type,
                "content": content,
                "metadata": metadata or {},
            }
        )

    async def get_conversation_history(self, conversation_id):
        return []


@dataclass
class FakeStates:
    data: dict = field(default_factory=dict)

    def _load(self, user_id):
        raw = self.data.get(user_id)
        if raw is None:
            return None
        from miriam_agent.onboarding.state import OnboardingState

        return OnboardingState(raw)

    async def save_state(self, user_id, state):
        self.data[user_id] = state.to_dict()

    async def get_state(self, user_id):
        return self._load(user_id)

    async def clear(self, user_id):
        self.data.pop(user_id, None)


class FakeProvider:
    """Returns canned JSON responses in order (content-only; the driver's
    free-text fallback path, so these tests also cover the fallback)."""

    def __init__(self, responses: list[str] | None = None):
        self._responses = list(responses or [])
        self.calls: list = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append(messages)
        from miriam_agent.agents.llm import LLMResponse

        return LLMResponse(
            content=(self._responses.pop(0) if self._responses else _r("interview"))
        )


def _service(monkeypatch, provider=None):
    from miriam_agent.onboarding.service import OnboardingService
    from miriam_agent.onboarding.trace import OnboardingTraceStore

    states = FakeStates()
    memory = FakeMemory()
    monkeypatch.setattr(
        "miriam_agent.onboarding.service.get_onboarding_state_store",
        lambda: states,
    )
    monkeypatch.setattr(
        "miriam_agent.onboarding.service.get_onboarding_trace_store",
        lambda: OnboardingTraceStore(use_redis=False),
    )
    svc = OnboardingService(memory, provider=provider or FakeProvider())
    return svc, states


def _r(intent, *, reply="ok", facts=None, adjustment=""):
    return json.dumps(
        {
            "reply": reply,
            "intent": intent,
            "facts": facts or {},
            "suggested_replies": [],
            "adjustment": adjustment,
        }
    )


def _plan_present(reply="Here is your plan."):
    return json.dumps({"reply": reply})


def _drive(monkeypatch, responses, messages):
    service, states = _service(monkeypatch, FakeProvider(responses))
    user = _user()
    turn = None
    for message in messages:
        turn = _run(service.handle_turn(user, message=message))
    return states.data["u-1"], turn


# -----------------------------------------------------------------------
# Explicit state machine: every legal (stage, intent) edge, exercised
# -----------------------------------------------------------------------


def test_stage_intent_transition_matrix_interview(monkeypatch):
    from miriam_agent.onboarding.state import (
        STAGE_AWAITING_STATEMENT,
        STAGE_COMPLETE,
        STAGE_INTERVIEW,
        STAGE_PLAN_CONSENT,
    )

    enter = [_r("interview", reply="the opener")]

    s, t = _drive(monkeypatch, enter + [_r("interview")], ["hey", "Tola", "still here"])
    assert s["stage"] == STAGE_INTERVIEW and t.completed is False

    s, _ = _drive(
        monkeypatch, enter + [_r("request_statement")], ["hey", "Tola", "enough?"]
    )
    assert s["stage"] == STAGE_AWAITING_STATEMENT

    s, _ = _drive(
        monkeypatch,
        enter + [_r("present_plan"), _plan_present()],
        ["hey", "Tola", "let's wrap"],
    )
    assert s["stage"] == STAGE_PLAN_CONSENT and s["plan_presented"] is True

    s, t = _drive(monkeypatch, enter + [_r("abandon")], ["hey", "Tola", "stop"])
    assert s["stage"] == STAGE_COMPLETE and t.completed is True


def test_stage_intent_transition_matrix_awaiting_statement(monkeypatch):
    from miriam_agent.onboarding.state import (
        STAGE_AWAITING_STATEMENT,
        STAGE_COMPLETE,
        STAGE_PLAN_CONSENT,
    )

    enter = [_r("interview", reply="opener"), _r("request_statement")]
    tail = ["hey", "Tola", "what's next"]

    s, _ = _drive(monkeypatch, enter + [_r("request_statement")], tail + ["send it"])
    assert s["stage"] == STAGE_AWAITING_STATEMENT

    s, _ = _drive(
        monkeypatch, enter + [_r("present_plan"), _plan_present()], tail + ["go"]
    )
    assert s["stage"] == STAGE_PLAN_CONSENT

    s, t = _drive(monkeypatch, enter + [_r("abandon")], tail + ["never mind"])
    assert s["stage"] == STAGE_COMPLETE and t.completed is True


def test_stage_intent_transition_matrix_plan_consent(monkeypatch):
    from miriam_agent.onboarding.state import (
        STAGE_AWAITING_ADJUSTMENT,
        STAGE_COMPLETE,
        STAGE_PLAN_CONSENT,
    )

    enter = [_r("interview", reply="opener"), _r("present_plan"), _plan_present()]
    tail = ["hey", "Tola", "wrap it"]

    s, t = _drive(monkeypatch, enter + [_r("consent_yes")], tail + ["set it up"])
    assert s["stage"] == STAGE_COMPLETE and t.completed is True and t.automated is True

    s, t = _drive(monkeypatch, enter + [_r("consent_no")], tail + ["not now"])
    assert s["stage"] == STAGE_COMPLETE and t.completed is True and t.automated is False

    s, _ = _drive(monkeypatch, enter + [_r("adjust")], tail + ["change something"])
    assert s["stage"] == STAGE_AWAITING_ADJUSTMENT

    s, t = _drive(monkeypatch, enter + [_r("abandon")], tail + ["forget it"])
    assert s["stage"] == STAGE_COMPLETE and t.completed is True

    s, _ = _drive(monkeypatch, enter + [_r("interview")], tail + ["tell me more"])
    assert s["stage"] == STAGE_PLAN_CONSENT


def test_stage_intent_transition_matrix_awaiting_adjustment(monkeypatch):
    from miriam_agent.onboarding.state import (
        STAGE_AWAITING_ADJUSTMENT,
        STAGE_COMPLETE,
        STAGE_PLAN_CONSENT,
    )

    enter = [
        _r("interview", reply="opener"),
        _r("present_plan"),
        _plan_present(),
        _r("adjust"),
    ]
    tail = ["hey", "Tola", "wrap it", "change something"]

    s, _ = _drive(
        monkeypatch,
        enter + [_r("adjust", adjustment="bigger buffer"), _plan_present("reworked")],
        tail + ["make the buffer bigger"],
    )
    assert s["stage"] == STAGE_PLAN_CONSENT and s["adjustments"] == ["bigger buffer"]

    s, _ = _drive(monkeypatch, enter + [_r("adjust")], tail + ["another change"])
    assert s["stage"] == STAGE_AWAITING_ADJUSTMENT

    s, _ = _drive(
        monkeypatch,
        enter + [_r("done_adjusting"), _plan_present("reworked")],
        tail + ["looks good"],
    )
    assert s["stage"] == STAGE_PLAN_CONSENT

    s, t = _drive(monkeypatch, enter + [_r("abandon")], tail + ["stop this"])
    assert s["stage"] == STAGE_COMPLETE and t.completed is True

    s, _ = _drive(monkeypatch, enter + [_r("interview")], tail + ["one more thing"])
    assert s["stage"] == STAGE_AWAITING_ADJUSTMENT


def test_transition_table_is_complete(monkeypatch):
    from miriam_agent.onboarding import driver
    from miriam_agent.onboarding.service import (
        _DEFAULT_TRANSITION,
        _TRANSITIONS,
    )
    from miriam_agent.onboarding.state import ALL_STAGES

    del monkeypatch
    for stage, intents in driver.STAGE_INTENTS.items():
        for intent in intents:
            assert (stage, intent) in _TRANSITIONS, (stage, intent)
    # The default exists and never moves the conversation.
    assert _DEFAULT_TRANSITION.action == "stay"
    # Every explicit edge lands on a real stage and a known action.
    actions = {
        "stay",
        "present_plan",
        "request_statement",
        "complete_automated",
        "complete_draft",
        "abandon",
        "adjust",
    }
    for (stage, intent), transition in _TRANSITIONS.items():
        assert transition.action in actions
        if transition.action != "stay":
            assert transition.to in ALL_STAGES, (stage, intent, transition)


def test_driver_accepts_structured_tool_call(monkeypatch):
    from miriam_agent.agents.llm import LLMResponse
    from miriam_agent.onboarding import driver

    del monkeypatch
    arguments = json.dumps(
        {
            "reply": "When it lands, does it come steady or in lumps?",
            "intent": "interview",
            "facts": {"cashflow": "lumpy"},
            "suggested_replies": [],
            "adjustment": "",
        }
    )
    response = LLMResponse(
        content="",
        tool_calls=[
            {
                "id": "1",
                "type": "function",
                "function": {
                    "name": "emit_conductor_outcome",
                    "arguments": arguments,
                },
            }
        ],
    )
    out = driver._outcome_from_response(response, "interview")
    assert out is not None
    assert out.intent == "interview"
    assert out.facts == {"cashflow": "lumpy"}
    assert out.reply.endswith("?")


def test_driver_tool_call_with_garbage_arguments_falls_back(monkeypatch):
    from miriam_agent.agents.llm import LLMResponse
    from miriam_agent.onboarding import driver

    del monkeypatch
    response = LLMResponse(
        content='{"reply":"fallback works?","intent":"interview"}',
        tool_calls=[
            {
                "id": "1",
                "type": "function",
                "function": {"name": "emit_conductor_outcome", "arguments": "not json"},
            }
        ],
    )
    out = driver._outcome_from_response(response, "interview")
    assert out is not None
    assert out.reply == "fallback works?"


# -----------------------------------------------------------------------
# Adversarial hardening: injected/fabricated numbers never reach the user
# -----------------------------------------------------------------------


def test_present_plan_clamps_invented_number(monkeypatch):
    from miriam_agent.onboarding.state import STAGE_PLAN_CONSENT

    poisoned = json.dumps(
        {
            "reply": (
                "Here's the plan - lock in a guaranteed 8% APY, risk-free. "
                "Shall I set it up?"
            )
        }
    )
    responses = [
        _r("interview", reply="the opener"),
        _r("interview", facts={"cashflow": "roughly 4000/month"}),
        _r("present_plan"),
        poisoned,
    ]
    state, turn = _drive(
        monkeypatch, responses, ["hey", "Tola", "income is lumpy", "wrap it"]
    )
    assert state["stage"] == STAGE_PLAN_CONSENT
    assert "8%" not in turn.response
    assert "APY" not in turn.response
    assert "Here's your picture" in turn.response


def test_present_plan_keeps_grounded_reply(monkeypatch):
    grounded_reply = (
        "We'll plan around your 4000 a month, buffer first. Shall I set this "
        "up so it runs quietly in the background?"
    )
    responses = [
        _r("interview", reply="the opener"),
        _r("interview", facts={"cashflow": "roughly 4000/month"}),
        _r("present_plan"),
        json.dumps({"reply": grounded_reply}),
    ]
    _, turn = _drive(
        monkeypatch, responses, ["hey", "Tola", "income is lumpy", "wrap it"]
    )
    assert turn.response == grounded_reply


def test_run_replay_flags_ungrounded_numbers():
    from miriam_agent.onboarding import evals

    results = evals.run_replay(
        [
            {
                "reply": "Earn 8% APY, risk-free.",
                "intent": "present_plan",
                "present": True,
                "grounded": "buffer first",
            }
        ]
    )
    assert results[0]["violations"] == ["R10"]

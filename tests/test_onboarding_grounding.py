"""The reported hallucination: a figure nobody stated reached the user.

Miriam answered "I would love you to talk me through funding my account" with
"N30000.00 left this month. That's about N10000.00/day if we keep it tidy."
Nobody said 30,000 and nobody divided it into a daily rate.

The linter was not the problem: R10 flagged both numbers. The conductor path
computed the violation, wrote it to the trace, and dispatched the reply anyway
-- only the plan presentation ever clamped on R10. These tests pin the fixed
contract, in the order the failure happened:

  * R10 on a money reply is a hard violation, not a note;
  * a hard violation buys exactly one re-ask, in Miriam's own voice;
  * a reply that drifts twice is replaced by deterministic copy;
  * a number the user actually gave still ships on the first try.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

_NAIRA = "\u20a6"

# The exact reply from the incident report.
HALLUCINATED_REPLY = (
    f"{_NAIRA}30000.00 left this month.\n"
    f"That's about {_NAIRA}10000.00/day if we keep it tidy."
)
USER_ASK = "Yeah I would love you to talk me through funding my account"
OPENER = "Nice to meet you, Tola! What's been on your mind about money lately?"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _user(uid: str = "u-grounding"):
    return type("U", (), {"id": uid})()


def _reply(reply, intent="interview", facts=None, taps=None):
    return json.dumps(
        {
            "reply": reply,
            "intent": intent,
            "facts": facts or {},
            "suggested_replies": taps or [],
            "adjustment": "",
        }
    )


def _full_text(turn) -> str:
    """Every bubble the user would actually see, joined."""
    parts = [turn.response or ""]
    parts.extend(turn.messages or [])
    return "\n".join(part for part in parts if part)


@dataclass
class FakeMemory:
    entries: list[dict] = field(default_factory=list)
    _history: dict[str, list[dict]] = field(default_factory=dict)

    async def store_memory(self, user_id, memory_type, content, metadata=None):
        self.entries.append(
            {"user_id": user_id, "type": memory_type, "content": content}
        )

    async def get_conversation_history(self, conversation_id):
        return list(self._history.get(conversation_id, []))


@dataclass
class FakeStates:
    data: dict[str, dict] = field(default_factory=dict)

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


class ScriptedProvider:
    """Canned conductor replies in order, recording every call.

    ``fail_from`` makes the Nth and later calls raise, so a retry can be taken
    off the table the way a real outage would.
    """

    def __init__(self, responses: list[str], fail_from: int | None = None):
        self._responses = list(responses)
        self._fail_from = fail_from
        self.calls: list[list] = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        from miriam_agent.agents.llm import LLMResponse

        self.calls.append(messages)
        if self._fail_from is not None and len(self.calls) >= self._fail_from:
            raise RuntimeError("llm unavailable")
        if not self._responses:
            return LLMResponse(content=_reply("ok"))
        return LLMResponse(content=self._responses.pop(0))


def _service(monkeypatch, provider):
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
    svc = OnboardingService(memory, provider=provider)  # type: ignore[arg-type]
    return svc, states


def _into_interview(service, user):
    """Walk greeting -> name capture -> interview, so the next turn is a
    conductor turn (the path the incident came through)."""
    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="my name is Tola"))


# -----------------------------------------------------------------------
# Linter: what counts as a hard violation
# -----------------------------------------------------------------------


def test_incident_reply_is_a_hard_violation():
    from miriam_agent.onboarding.quality import (
        EvalMeta,
        evaluate_reply,
        hard_violations,
    )

    violations = evaluate_reply(HALLUCINATED_REPLY, EvalMeta(grounded=USER_ASK))
    assert "R10" in violations
    assert hard_violations(violations) == ["R10"]


def test_invented_daily_rate_is_caught_even_when_the_total_is_grounded():
    """The incident shape: the 30,000 was knowable, the 10,000/day was not."""
    from miriam_agent.onboarding.quality import (
        EvalMeta,
        evaluate_reply,
        hard_violations,
    )

    grounded = USER_ASK + ", my take home is 30000 a month"
    violations = evaluate_reply(HALLUCINATED_REPLY, EvalMeta(grounded=grounded))
    assert hard_violations(violations) == ["R10"]


def test_voice_drift_alone_is_not_hard():
    """Posture is worth tracing, never worth dropping a sentence over."""
    from miriam_agent.onboarding.quality import hard_violations

    assert hard_violations(["R1", "R3", "R12"]) == []
    assert hard_violations(["R1", "R10"]) == ["R10"]


# -----------------------------------------------------------------------
# The conductor path: the hole the incident came through
# -----------------------------------------------------------------------


def test_hallucinated_reply_is_reasked_and_the_clean_retry_ships(monkeypatch):
    clean = (
        "I can only speak to numbers you have given me, and I have none yet. "
        "What lands in your account each month?"
    )
    provider = ScriptedProvider(
        [_reply(OPENER), _reply(HALLUCINATED_REPLY), _reply(clean)]
    )
    service, states = _service(monkeypatch, provider)
    user = _user()
    _into_interview(service, user)

    turn = _run(service.handle_turn(user, message=USER_ASK))

    assert clean in _full_text(turn)
    assert "30000" not in _full_text(turn)

    # The retry carried the correction, and it rode in the system prompt --
    # never as words put into the user's mouth.
    from miriam_agent.onboarding import driver

    retry_messages = provider.calls[-1]
    assert driver.GROUNDING_CORRECTION in retry_messages[0].content
    assert USER_ASK in retry_messages[-1].content
    assert states.data["u-grounding"]["stage"] == "interview"


def test_double_drift_never_reaches_the_user(monkeypatch):
    provider = ScriptedProvider(
        [_reply(OPENER), _reply(HALLUCINATED_REPLY), _reply(HALLUCINATED_REPLY)]
    )
    service, _ = _service(monkeypatch, provider)
    user = _user()
    _into_interview(service, user)

    turn = _run(service.handle_turn(user, message=USER_ASK))
    text = _full_text(turn)

    assert "30000" not in text
    assert "10000" not in text
    assert "10,000" not in text
    # The conversation still moves: deterministic copy keeps the stage open.
    assert text.strip()


def test_a_dead_retry_falls_back_rather_than_sending_the_draft(monkeypatch):
    provider = ScriptedProvider(
        [_reply(OPENER), _reply(HALLUCINATED_REPLY)], fail_from=3
    )
    service, _ = _service(monkeypatch, provider)
    user = _user()
    _into_interview(service, user)

    turn = _run(service.handle_turn(user, message=USER_ASK))
    text = _full_text(turn)

    assert "30000" not in text
    assert "10000" not in text
    assert text.strip()


def test_garbage_retry_falls_back_rather_than_sending_the_draft(monkeypatch):
    provider = ScriptedProvider(
        [_reply(OPENER), _reply(HALLUCINATED_REPLY), "not json at all"]
    )
    service, _ = _service(monkeypatch, provider)
    user = _user()
    _into_interview(service, user)

    turn = _run(service.handle_turn(user, message=USER_ASK))
    text = _full_text(turn)

    assert "30000" not in text
    assert text.strip()


def test_a_number_the_user_gave_still_ships_first_try(monkeypatch):
    """No false positives: the guard must not fire on the user's own figures,
    and must not spend a retry when there is nothing to fix."""
    reply = "So 30,000 comes in each month. What goes out first?"
    provider = ScriptedProvider([_reply(OPENER), _reply(reply)])
    service, _ = _service(monkeypatch, provider)
    user = _user()
    _into_interview(service, user)
    calls_before = len(provider.calls)

    turn = _run(service.handle_turn(user, message="my take home is 30000 a month"))

    assert reply in _full_text(turn)
    assert len(provider.calls) == calls_before + 1


def test_a_fact_with_an_invented_figure_is_refused(monkeypatch):
    """A figure parked in ``facts`` beside a clean reply becomes memory, so it
    is refused by the same hard R10 rule as a figure in the reply itself."""
    clean = "Got it. What's coming in each month, roughly?"
    provider = ScriptedProvider(
        [
            _reply(OPENER),
            _reply(clean, facts={"cashflow": "30000 a month"}),
            _reply(clean, facts={"cashflow": "steady"}),
        ]
    )
    service, states = _service(monkeypatch, provider)
    user = _user()
    _into_interview(service, user)

    turn = _run(service.handle_turn(user, message="I earn a decent amount"))

    assert "30000" not in turn.response
    # The invented figure never reaches persisted state or memory.
    assert "30000" not in json.dumps(states.data["u-grounding"])
    assert "steady" in json.dumps(states.data["u-grounding"])

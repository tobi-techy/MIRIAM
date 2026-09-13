"""Tests for the conversational financial onboarding flow.

Covers:
  - Question bank: core ordering, adaptive triggered follow-ups (capped),
    deterministic branching
  - Plan builder: diagnostics, overlays, steps, standing-rules gating
  - State store: local-fallback round-trip and isolation per user
  - Driver: LLM-led parse/validation, canonical classification, stage-safe
    intents, taps clamping, deterministic fallback on garbage/errors
  - Service flow (LLM-led): interview -> statement -> plan -> consent ->
    complete, action-intent bypass, abandon, adjustments, document shortcut

Note: tests follow the repo convention of sync wrappers around
``asyncio.get_event_loop().run_until_complete(...)`` so the shared session
event loop is never torn down.
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
# Question bank
# -----------------------------------------------------------------------


def test_core_questions_asked_in_order():
    from miriam_agent.onboarding import questions as q

    asked: list[str] = []
    for core in q.CORE:
        nxt = q.next_question({}, asked)
        assert nxt.id == core.id, (nxt.id, core.id)
        asked.append(nxt.id)


def test_core_done_then_followups_by_priority():
    from miriam_agent.onboarding import questions as q

    answers = {
        # Force every trigger on so priority order (not trigger order) decides.
        "money_feelings": "Stressful",
        "income_predictability": "All over the place",
        "shortage_cause": "I spend more than I plan",
        "liquidity_runway": "A few days",
        "dependents": "Extended family",
        "goal_direction": "Build wealth",
        "involvement": "Keep it light",
    }
    asked = [c.id for c in q.CORE]
    out = []
    while True:
        nxt = q.next_question(answers, asked, max_followups=5)
        if nxt is None:
            break
        out.append(nxt.id)
        asked.append(nxt.id)
    assert out == [qid for qid in q.FOLLOWUP_PRIORITY]


def test_untriggered_followups_never_asked():
    from miriam_agent.onboarding import questions as q

    # Calm money, steady income, no dependents, not wealth-building.
    answers = {
        "money_feelings": "Calm",
        "income_predictability": "Steady every month",
        "shortage_cause": "I don't run short",
        "liquidity_runway": "Three to six months",
        "dependents": "Just me",
        "goal_direction": "Not sure yet",
        "involvement": "Keep it light",
    }
    asked = [c.id for c in q.CORE]
    assert q.next_question(answers, asked, max_followups=5) is None


def test_max_followups_caps_triggers():
    from miriam_agent.onboarding import questions as q

    answers = {
        "money_feelings": "Stressful",
        "income_predictability": "All over the place",
        "shortage_cause": "I spend more than I plan",
        "liquidity_runway": "A few days",
        "dependents": "Extended family",
        "goal_direction": "Build wealth",
        "involvement": "Keep it light",
    }
    asked = [c.id for c in q.CORE]
    out = []
    while True:
        nxt = q.next_question(answers, asked, max_followups=2)
        if nxt is None:
            break
        out.append(nxt.id)
        asked.append(nxt.id)
    assert len(out) == 2
    assert out == [qid for qid in q.FOLLOWUP_PRIORITY][:2]


def test_answered_filters_blank():
    from miriam_agent.onboarding import questions as q

    blank = {c.id: "" for c in q.CORE}
    assert q.answered(blank) == {}
    assert q.is_followup("debt_probe") is True
    assert q.is_followup("money_feelings") is False


# -----------------------------------------------------------------------
# Plan builder
# -----------------------------------------------------------------------


def test_plan_stability_seeker_short_runway_no_automation():
    from miriam_agent.onboarding.plan import build_plan

    answers = {
        "money_feelings": "Stressful",
        "income_predictability": "Steady every month",
        "shortage_cause": "The timing, it comes in late",
        "liquidity_runway": "Maybe a month",
        "dependents": "Just me",
        "goal_direction": "Stop the stress",
        "involvement": "Keep it light",
    }
    plan = build_plan(answers)
    assert plan["diagnostic_state"] == "Stability Seeker"
    assert "goal_urgency" in plan["overlays"]
    assert plan["steps"][0]["title"] == "Protect the next month"
    # Hands-off involvement: no automation bullets.
    assert plan["automate"] is False
    assert plan["standing_rules"] == []
    # Tracking level still earns the weekly check-in.
    assert any(s["id"] == "checkin" for s in plan["steps"])


def test_plan_volatile_earner_automates_rules():
    from miriam_agent.onboarding.plan import build_plan

    answers = {
        "money_feelings": "Honestly, a mess",
        "income_predictability": "All over the place",
        "shortage_cause": "The timing, it comes in late",
        "liquidity_runway": "Three to six months",
        "dependents": "Just me",
        "goal_direction": "Not sure yet",
        "involvement": "Set it up for me",
    }
    plan = build_plan(answers)
    assert plan["diagnostic_state"] == "Volatile Earner"
    assert plan["automate"] is True
    ids = {r["kind"] for r in plan["standing_rules"]}
    assert {"buffer", "income_rhythm"} <= ids
    assert plan["source"] == "onboarding_interview"


def test_plan_wealth_builder_and_overlays():
    from miriam_agent.onboarding.plan import build_plan

    answers = {
        "money_feelings": "Calm",
        "income_predictability": "Steady every month",
        "shortage_cause": "I don't run short",
        "liquidity_runway": "Six months or more",
        "dependents": "Extended family",
        "goal_direction": "Build wealth",
        "involvement": "Suggest things, I'll do it",
        "debt_probe": "A noticeable amount",
        "behavior_probe": "I don't track it closely",
    }
    plan = build_plan(answers)
    assert plan["diagnostic_state"] == "Wealth Builder"
    assert {"debt_burden", "family_obligations", "spending_leakage"} <= set(
        plan["overlays"]
    )
    # Follow-up probes produce their own steps too.
    assert any(s["id"] == "debt" for s in plan["steps"])
    assert any(s["id"] == "spending_guard" for s in plan["steps"])
    assert any(s["id"] == "goal" for s in plan["steps"])
    # Document grounding flows into the summary.
    grounded = build_plan(answers, document_summary="shared stmt.pdf")
    assert grounded["statement_summary"] == "shared stmt.pdf"


# -----------------------------------------------------------------------
# State store (local fallback)
# -----------------------------------------------------------------------


def test_state_store_round_trip_and_isolation():
    from miriam_agent.onboarding.plan import build_plan
    from miriam_agent.onboarding.state import OnboardingState, OnboardingStateStore

    store = OnboardingStateStore(redis_url="", ttl_days=None)
    s1 = OnboardingState({"document_summary": "st.pdf"})
    s1.answers["money_feelings"] = "Calm"
    s1.plan_presented = True
    s1.plan = build_plan({"involvement": "Keep it light"})
    _run(store.save_state("u-1", s1))

    s2 = _run(store.get_state("u-1"))
    assert s2 is not None
    assert s2.answers["money_feelings"] == "Calm"
    assert s2.plan_presented is True
    assert s2.to_dict()["document_summary"] == "st.pdf"
    assert s2.plan["diagnostic_state"] == "Financial Beginner"

    # Per-user isolation + clear.
    assert _run(store.get_state("u-2")) is None
    _run(store.clear("u-1"))
    assert _run(store.get_state("u-1")) is None


def test_state_store_stamps_updated_at_and_expires_local_fallback():
    from miriam_agent.onboarding.state import OnboardingState, OnboardingStateStore

    store = OnboardingStateStore(redis_url="", ttl_days=1)
    s = OnboardingState()
    _run(store.save_state("u-1", s))
    assert s.updated_at > 0
    assert _run(store.get_state("u-1")) is not None

    # Stale record (older than the sliding TTL) is not resurrected.
    _run(store.save_state("u-1", s))
    store._local["miriam:onboarding:state:u-1"]["updated_at"] = (
        s.updated_at - 2 * 24 * 60 * 60
    )
    assert _run(store.get_state("u-1")) is None
    assert "miriam:onboarding:state:u-1" not in store._local


# -----------------------------------------------------------------------
# Service: full flow, bypass, abandon, adjustments, document shortcut
# -----------------------------------------------------------------------


@dataclass
class FakeMemory:
    entries: list[dict] = field(default_factory=list)
    _history: dict[str, list[dict]] = field(default_factory=dict)

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


class FakeProvider:
    """Returns canned JSON responses in order; used by all service-flow tests."""

    def __init__(self, responses: list[str] | None = None):
        self._responses = list(responses or [])
        self.calls: list[list] = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append(messages)
        from miriam_agent.agents.llm import LLMResponse

        if not self._responses:
            default = '{"reply":"ok","intent":"interview","suggested_replies":[]}'
            return LLMResponse(content=default)
        return LLMResponse(content=self._responses.pop(0))


def _service(monkeypatch, provider=None):
    from miriam_agent.onboarding.service import OnboardingService

    states = FakeStates()
    memory = FakeMemory()
    monkeypatch.setattr(
        "miriam_agent.onboarding.service.get_onboarding_state_store",
        lambda: states,
    )
    prov = provider or FakeProvider()
    svc = OnboardingService(memory, provider=prov)  # type: ignore[arg-type]
    return svc, states, memory, prov


# ------------------------------------------------------------------
# Shorthand: each entry is a JSON string returned by the fake provider
# for one call to ``provider.complete()``.  The service makes at most
# one call per ``handle_turn`` (the conductor), plus one extra call
# when presenting the plan.  Tests list out the expected sequence
# explicitly so the driver behaviour stays deterministic.
# ------------------------------------------------------------------


def _greet(reply, intent="interview", answers=None, taps=None, adjustment=""):
    return json.dumps(
        {
            "reply": reply,
            "intent": intent,
            "answers": answers or {},
            "suggested_replies": taps or [],
            "adjustment": adjustment,
        }
    )


def _d(dim, value):
    return json.dumps(
        {
            "reply": f"{dim} done",
            "intent": "interview",
            "answers": {dim: value},
            "suggested_replies": [],
        }
    )


def _plan_present(reply="Here is your picture: Financial Beginner."):
    return json.dumps({"reply": reply})


# ------------------------------------------------------------------
# Service tests (LLM-led)
# ------------------------------------------------------------------


def test_action_intent_bypasses_interview(monkeypatch):
    user = _user()
    service, states, _, _ = _service(monkeypatch)
    for msg in ("send 5k to Tola", "what's my balance?"):
        turn = _run(service.handle_turn(user, message=msg))
        assert turn.took_over is False, msg
    assert states.data == {}


def test_full_interview_to_standing_rules(monkeypatch):
    """Happy-path: answers all dims via conductor, skips statement, gets plan,
    then consent_yes -> automated standing rules."""
    # greet -> core dims -> request_statement -> skip -> present_plan -> consent
    responses = [
        _greet("Hey! How is money feeling for you these days?"),
        _d("money_feelings", "Calm"),
        _d("income_predictability", "Steady every month"),
        _d("shortage_cause", "I don't run short"),
        _d("liquidity_runway", "Three to six months"),
        _d("dependents", "Just me"),
        _d("goal_direction", "Not sure yet"),
        # last core: involvement -> request the statement
        _greet(
            "Got it. Send me a statement, or say skip.",
            intent="request_statement",
            answers={"involvement": "Keep it light"},
        ),
        # skip statement
        _greet("No worries, let's go with what you told me.", intent="present_plan"),
        _plan_present(),
        # consent
        _greet("Done.", intent="consent_yes"),
    ]
    service, states, memory, _ = _service(monkeypatch, FakeProvider(responses))
    user = _user()

    turn = _run(service.handle_turn(user, message="hey"))
    assert turn.took_over is True

    answer_values = (
        "Calm",
        "Steady every month",
        "I don't run short",
        "Three to six months",
        "Just me",
        "Not sure yet",
    )
    for value in answer_values:
        turn = _run(service.handle_turn(user, message=value))
        assert turn.took_over is True
    assert states.data["u-1"].get("stage") == "interview"

    turn = _run(service.handle_turn(user, message="Keep it light"))
    assert states.data["u-1"].get("stage") == "awaiting_statement"

    turn = _run(service.handle_turn(user, message="Skip for now"))
    assert turn.poll is None
    assert states.data["u-1"].get("stage") == "plan_consent"

    turn = _run(service.handle_turn(user, message="ok, set it up"))
    assert turn.completed is True
    kinds = {e["type"] for e in memory.entries}
    assert "financial" in kinds and "onboarding" in kinds


def test_statement_as_first_message_starts_interview(monkeypatch):
    user = _user()
    provider = FakeProvider(
        [
            _greet(
                "Thanks for the statement - I'll build your plan on your real "
                "numbers. How is money feeling?"
            ),
        ]
    )
    service, states, _, _ = _service(monkeypatch, provider)
    turn = _run(
        service.handle_turn(
            user,
            message="[bank statement]",
            document={"name": "stmt.pdf", "summary": "average balance 4200"},
        )
    )
    assert turn.took_over is True
    assert states.data["u-1"].get("document_summary") == "average balance 4200"
    assert states.data["u-1"].get("stage") == "interview"
    assert turn.took_over is True
    assert "Thanks for the statement" in turn.response


def test_document_mid_interview_builds_plan(monkeypatch):
    user = _user()
    provider = FakeProvider(
        [
            _greet("How is money feeling?"),
            _greet(
                "Got it.",
                intent="request_statement",
                answers={"money_feelings": "Calm"},
            ),
            # document arrives while in awaiting_statement -> present_plan
            _plan_present(),
        ]
    )
    service, states, memory, _ = _service(monkeypatch, provider)
    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="Calm"))
    assert states.data["u-1"].get("stage") == "awaiting_statement"

    turn = _run(
        service.handle_turn(
            user,
            message="[bank statement]",
            document={"summary": "avg bal 3000"},
        )
    )
    assert turn.poll is None
    assert states.data["u-1"].get("stage") == "plan_consent"
    assert "3000" in (states.data["u-1"].get("document_summary") or "")


def test_abandon_during_interview(monkeypatch):
    provider = FakeProvider(
        [
            _greet("Hey!"),
            _greet("No stress.", intent="abandon"),
        ]
    )
    service, states, _, _ = _service(monkeypatch, provider)
    user = _user()
    _run(service.handle_turn(user, message="hey"))
    turn = _run(service.handle_turn(user, message="skip"))
    assert turn.completed is True
    assert states.data["u-1"]["stage"] == "complete"


def test_redo_after_complete_restarts_interview(monkeypatch):
    provider = FakeProvider(
        [
            _greet("Hey!"),
            _greet("No stress.", intent="abandon"),
            # redo triggers a fresh conductor call
            _greet("Welcome back! How is money feeling?"),
        ]
    )
    service, states, _, _ = _service(monkeypatch, provider)
    user = _user()
    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="skip"))
    assert states.data["u-1"]["stage"] == "complete"

    turn = _run(service.handle_turn(user, message="what's my balance"))
    assert turn.took_over is False

    turn = _run(service.handle_turn(user, message="let's redo it"))
    assert turn.took_over is True
    assert turn.completed is False
    assert states.data["u-1"].get("stage") == "interview"
    assert states.data["u-1"].get("answers") == {}


def test_adjustment_flow(monkeypatch):
    """plan_consent -> adjust (clarify) -> adjustment note -> re-present -> consent."""
    all_dims = {
        "money_feelings": "Calm",
        "income_predictability": "Steady",
        "shortage_cause": "I don't run short",
        "liquidity_runway": "Six months",
        "dependents": "Just me",
        "goal_direction": "Not sure",
        "involvement": "Keep it light",
    }
    provider = FakeProvider(
        [
            _greet("Hey!"),
            _greet("Got it.", intent="request_statement", answers=all_dims),
            _greet("No worries.", intent="present_plan"),
            _plan_present(),
            # user asks to adjust -> vague, so we clarify
            _greet("Sure.", intent="adjust"),
            # user states the change -> fold in and re-present
            _greet(
                "Done.",
                intent="adjust",
                answers={},
                adjustment="bigger emergency buffer",
            ),
            _plan_present("Reworked plan: Financial Beginner."),
            # consent
            _greet("Sounds good.", intent="consent_yes"),
        ]
    )
    service, states, memory, provider = _service(monkeypatch, provider)
    user = _user()
    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="I'm easy on the details"))
    _run(service.handle_turn(user, message="Skip for now"))
    assert states.data["u-1"].get("stage") == "plan_consent"

    turn = _run(service.handle_turn(user, message="Let's adjust it"))
    assert turn.took_over is True
    assert "change" in turn.response.lower() or "rework" in turn.response.lower()

    turn = _run(service.handle_turn(user, message="More buffer"))
    assert turn.took_over is True
    assert states.data["u-1"].get("stage") == "plan_consent"
    assert "bigger emergency buffer" in states.data["u-1"].get("adjustments")

    turn = _run(service.handle_turn(user, message="looks good"))
    assert turn.completed is True
    assert any(e["type"] == "onboarding" for e in memory.entries)


def test_llm_failure_uses_deterministic_fallback(monkeypatch):
    """When provider raises, the deterministic fallback kicks in."""

    class _BoomProvider:
        async def complete(self, *a, **kw):
            raise RuntimeError("model offline")

    service, states, _, _ = _service(monkeypatch, _BoomProvider())
    user = _user()
    turn = _run(service.handle_turn(user, message="hey"))
    assert turn.took_over is True
    assert turn.poll is not None  # deterministic first question poll
    assert states.data["u-1"].get("stage") == "interview"


def test_garbage_json_uses_fallback(monkeypatch):
    service, states, _, _ = _service(monkeypatch, FakeProvider(["not json at all"]))
    user = _user()
    turn = _run(service.handle_turn(user, message="hey"))
    assert turn.took_over is True
    assert turn.poll is not None


def test_first_message_statement_starts_interview_no_plan(monkeypatch):
    """A statement as the first message should save the scan AND start the
    interview (not immediately build a thin plan)."""
    provider = FakeProvider(
        [
            _greet(
                "Thanks for the statement - I'll use those real numbers. "
                "How is money feeling?"
            ),
        ]
    )
    service, states, _, _ = _service(monkeypatch, provider)
    user = _user()
    turn = _run(
        service.handle_turn(
            user,
            message="stmt",
            document={"summary": "balance 5000"},
        )
    )
    assert states.data["u-1"].get("document_summary") == "balance 5000"
    assert states.data["u-1"].get("stage") == "interview"
    assert turn.poll is None  # LLM replies, no deterministic poll
    assert "statement" in turn.response.lower()


# -----------------------------------------------------------------------
# Driver: parsing, classification, and validation
# -----------------------------------------------------------------------


def test_driver_parse_fenced_json_and_canonicalize():
    from miriam_agent.onboarding import driver

    text = (
        '```json\n{"reply": "How is money feeling?", "intent": "interview",'
        ' "answers": {"money_feelings": "stressful"},'
        ' "suggested_replies": ["Calm"]}\n```'
    )
    out = driver._parse_driver_output(text, "interview")
    assert out is not None
    assert out.reply == "How is money feeling?"
    assert out.answers == {"money_feelings": "Stressful"}
    assert out.suggested == ["Calm"]


def test_driver_unknown_intent_falls_back_to_stage_default():
    from miriam_agent.onboarding import driver

    out = driver._parse_driver_output(
        '{"reply":"x","intent":"consent_yes"}', "interview"
    )
    assert out.intent == "interview"
    out = driver._parse_driver_output(
        '{"reply":"x","intent":"consent_yes"}', "plan_consent"
    )
    assert out.intent == "consent_yes"
    out = driver._parse_driver_output(
        '{"reply":"x","intent":"bogus"}', "awaiting_statement"
    )
    assert out.intent == "request_statement"


def test_driver_drops_unknown_dims_and_clamps_taps():
    from miriam_agent.onboarding import driver

    payload = {
        "reply": "q",
        "intent": "interview",
        "answers": {"nope_dim": "x", "goal_direction": "Buy a house"},
        "suggested_replies": ["a" * 100, "b", "c", "d", "e"],
    }
    out = driver._parse_driver_output(json.dumps(payload), "interview")
    assert set(out.answers) == {"goal_direction"}
    assert out.answers["goal_direction"] == "Buy a house"
    assert len(out.suggested) == 4
    assert len(out.suggested[0]) <= driver.MAX_TAP_LENGTH
    assert out.suggested[0].endswith("…")


def test_driver_garbage_and_empty_reply_none():
    from miriam_agent.onboarding import driver

    assert driver._parse_driver_output("not json", "interview") is None
    assert driver._parse_driver_output("", "interview") is None
    assert (
        driver._parse_driver_output('{"reply":"  ","intent":"interview"}', "interview")
        is None
    )


def test_driver_reply_clamped_with_taps():
    from miriam_agent.onboarding import driver

    long = "x" * 200
    out = driver._parse_driver_output(
        json.dumps({"reply": long, "suggested_replies": ["y"]}),
        "interview",
    )
    assert len(out.reply) <= driver.MAX_REPLY_WITH_TAPS
    assert out.reply.endswith("…")
    out2 = driver._parse_driver_output(json.dumps({"reply": long}), "interview")
    assert out2.reply == long


def test_canonicalize():
    from miriam_agent.onboarding import questions as q

    assert q.canonicalize("money_feelings", "stressful") == "Stressful"
    assert q.canonicalize("money_feelings", "stress") == "Stressful"
    assert q.canonicalize("money_feelings", "unknown vibes") == "unknown vibes"
    assert q.canonicalize("goal_direction", "Buy a house") == "Buy a house"
    assert q.canonicalize("not_a_dim", "x") == "x"


def test_conductor_turn_sends_stage_and_facts():
    from miriam_agent.agents.llm import LLMResponse
    from miriam_agent.onboarding import driver
    from miriam_agent.onboarding.state import OnboardingState

    class Capture:
        async def complete(
            self, messages, tools=None, temperature=None, max_tokens=None
        ):
            self.messages = messages
            return LLMResponse(content='{"reply":"hi","intent":"request_statement"}')

    state = OnboardingState()
    state.stage = "awaiting_statement"
    provider = Capture()
    out = _run(
        driver.conductor_turn(
            provider=provider, state=state, history=[], user_text="send it"
        )
    )
    assert out is not None and out.intent == "request_statement"
    user_block = provider.messages[-1].content
    assert "awaiting_statement" in user_block
    assert "DIMENSIONS STILL TO COVER" in user_block


def test_present_plan_turn_returns_reply():
    from miriam_agent.agents.llm import LLMResponse
    from miriam_agent.onboarding import driver
    from miriam_agent.onboarding.state import OnboardingState

    class P:
        async def complete(
            self, messages, tools=None, temperature=None, max_tokens=None
        ):
            present = "Here is your picture: Financial Beginner."
            return LLMResponse(content=json.dumps({"reply": present}))

    state = OnboardingState()
    state.stage = "plan_consent"
    plan = {"diagnostic_state": "Financial Beginner", "steps": []}
    out = _run(
        driver.present_plan_turn(
            provider=P(),
            state=state,
            history=[],
            plan=plan,
            adjustments=["more buffer"],
        )
    )
    assert out is not None and "Financial Beginner" in out.reply

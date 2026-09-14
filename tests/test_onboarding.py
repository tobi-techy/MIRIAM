"""Tests for the conversational financial onboarding flow.

Miriam leads the onboarding as a real conversation: there is no question bank
and no fixed vocabulary. She records free-form ``facts`` each turn (plus a
first-class ``money_moment`` and ``goal``), a deterministic plan builder turns
those facts into the diagnosis/steps/standing rules, and consent turns the plan
into living rules.

Covers:
  - Plan builder: fuzzy detection over free-form facts, diagnostics, overlays,
    steps, standing-rules gating
  - State store: local-fallback round-trip and isolation per user
  - Driver: LLM-led parse/validation of free-form facts, stage-safe intents,
    taps clamping, bounds, deterministic fallback on garbage/errors
  - Service flow (LLM-led): agent-led interview -> statement -> plan -> consent
    -> complete, action-intent bypass, abandon, adjustments, document shortcut
  - Fallback: a broken LLM degrades to short, warm conversational lines that
    still complete the flow
  - Guard rails: the interview cap closes the conversation deterministically

Note: tests follow the repo convention of sync wrappers around
``asyncio.get_event_loop().run_until_complete(...)`` so the shared session
event loop is never torn down.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _user(uid: str = "u-1"):
    return type("U", (), {"id": uid})()


# -----------------------------------------------------------------------
# Plan builder (free-form facts → deterministic plan)
# -----------------------------------------------------------------------


def test_plan_stability_seeker_short_runway_no_automation():
    from miriam_agent.onboarding.plan import build_plan

    facts = {
        "cashflow": "steady paycheck but the money tends to run out a few days "
        "before the end",
        "runway": "maybe a month at best",
        "income": "steady every month",
    }
    plan = build_plan(facts, money_moment="paying rent is stressful")
    assert plan["diagnostic_state"] == "Stability Seeker"
    assert "goal_urgency" in plan["overlays"]
    assert plan["steps"][0]["title"] == "Protect the next month"
    # No hands-on involvement: no automation bullets.
    assert plan["automate"] is False
    assert plan["standing_rules"] == []
    # The weekly check-in is always part of the plan.
    assert any(s["id"] == "checkin" for s in plan["steps"])


def test_plan_volatile_earner_automates_rules():
    from miriam_agent.onboarding.plan import build_plan

    facts = {
        "income": "all over the place, commissions come late",
        "involvement": "set it up for me",
    }
    plan = build_plan(facts)
    assert plan["diagnostic_state"] == "Volatile Earner"
    assert plan["automate"] is True
    ids = {r["kind"] for r in plan["standing_rules"]}
    assert {"buffer", "income_rhythm"} <= ids
    assert plan["source"] == "onboarding_interview"


def test_plan_wealth_builder_and_overlays():
    from miriam_agent.onboarding.plan import build_plan

    facts = {
        "income": "steady, six months of runway saved",
        "debt": "credit card debt that's become noticeable",
        "obligations": "send money home to family every month",
        "spending": "don't track it closely, small things add up",
    }
    plan = build_plan(facts, goal="invest and build wealth")
    assert plan["diagnostic_state"] == "Wealth Builder"
    assert {"debt_burden", "family_obligations", "spending_leakage"} <= set(
        plan["overlays"]
    )
    # Probes produce their own steps too.
    assert any(s["id"] == "debt" for s in plan["steps"])
    assert any(s["id"] == "spending_guard" for s in plan["steps"])
    assert any(s["id"] == "goal" for s in plan["steps"])
    # Document grounding flows into the summary.
    grounded = build_plan(facts, "shared stmt.pdf", goal="invest and build wealth")
    assert grounded["statement_summary"] == "shared stmt.pdf"


def test_plan_uses_first_class_money_moment_and_goal():
    from miriam_agent.onboarding.plan import build_plan

    plan = build_plan(
        {"cashflow": "roughly steady, maybe three months saved"},
        money_moment="running out before payday makes me anxious",
        goal="Japan trip in 2027",
    )
    assert plan["evidence"][0] == "running out before payday makes me anxious"
    goal_step = next(s for s in plan["steps"] if s["id"] == "goal")
    assert "Japan trip in 2027" in goal_step["detail"]
    assert plan["automate"] is False


def test_plan_minimal_default_is_financial_beginner():
    from miriam_agent.onboarding.plan import build_plan

    plan = build_plan({"involvement": "keep it light"})
    assert plan["diagnostic_state"] == "Financial Beginner"
    assert plan["automate"] is False
    assert any(s["id"] == "checkin" for s in plan["steps"])


def test_plan_carries_a_structured_insight_per_diagnostic():
    """spec §21: every plan carries one deterministic financial_insight object.
    The personality layer speaks it; the backend keeps the structured read."""
    from miriam_agent.onboarding.plan import build_plan

    plans = [
        build_plan({"cashflow": "runs out a few days before payday"}),
        build_plan({"income": "commission comes late, all over the place"}),
        build_plan({"income": "steady, six months saved"}, goal="invest for wealth"),
        build_plan({"involvement": "keep it light"}),
    ]
    by_state = {p["diagnostic_state"]: p for p in plans}
    assert by_state["Stability Seeker"]["insight"] == {
        "type": "financial_insight",
        "category": "cash_flow",
        "title": "saving happens last",
        "summary": "the buffer runs out before the money does, and saving comes last",
        "severity": "high",
        "confidence": 0.65,
        "financial_impact": None,
        "evidence": ["runs out a few days before payday"],
        "recommended_action": {
            "type": "safety_net",
            "amount": None,
            "timing": "income_received",
        },
    }
    assert by_state["Volatile Earner"]["insight"]["category"] == "income_volatility"
    assert by_state["Volatile Earner"]["insight"]["severity"] == "medium"
    assert by_state["Wealth Builder"]["insight"]["category"] == "growth"
    assert by_state["Financial Beginner"]["insight"]["severity"] == "low"
    for p in plans:
        ins = p["insight"]
        assert ins["type"] == "financial_insight"
        assert ins["title"] and ins["evidence"]
        assert 0 <= ins["confidence"] <= 1
        assert "timing" in ins["recommended_action"]


def test_plan_insight_confidence_grounded_by_statement():
    from miriam_agent.onboarding.plan import build_plan

    plan = build_plan(
        {"cashflow": "runs out a few days before payday"},
        "balance 5000",
    )
    # Statement grounds the confidence up from the base 0.6 + 0.05 evidence.
    assert plan["insight"]["confidence"] == 0.8


# -----------------------------------------------------------------------
# State store (local fallback)
# -----------------------------------------------------------------------


def test_state_store_round_trip_and_isolation():
    from miriam_agent.onboarding.plan import build_plan
    from miriam_agent.onboarding.state import OnboardingState, OnboardingStateStore

    store = OnboardingStateStore(redis_url="", ttl_days=None)
    s1 = OnboardingState({"document_summary": "st.pdf"})
    s1.learned["cashflow"] = "about 4000 a month, spikes with commission"
    s1.money_moment = "the month runs out before the money does"
    s1.money_moment_meta = {
        "emotion": "frustrated",
        "suspected_problem": "cash_flow",
        "confidence": 0.72,
    }
    s1.goal = "new car"
    s1.goal_meta = {"target_date": "2027", "estimated_cost": "2.5m"}
    s1.conversation_state["user_sentiment"] = "tense"
    s1.plan_presented = True
    s1.plan = build_plan({"involvement": "keep it light"})
    _run(store.save_state("u-1", s1))

    s2 = _run(store.get_state("u-1"))
    assert s2 is not None
    assert s2.learned["cashflow"] == "about 4000 a month, spikes with commission"
    assert s2.money_moment == "the month runs out before the money does"
    assert s2.money_moment_meta == {
        "emotion": "frustrated",
        "suspected_problem": "cash_flow",
        "confidence": 0.72,
    }
    assert s2.goal == "new car"
    assert s2.goal_meta == {"target_date": "2027", "estimated_cost": "2.5m"}
    assert s2.conversation_state["user_sentiment"] == "tense"
    assert s2.conversation_state["directness_level"] == 1
    assert s2.plan_presented is True
    assert s2.to_dict()["document_summary"] == "st.pdf"
    assert s2.plan["diagnostic_state"] == "Financial Beginner"

    # Per-user isolation + clear.
    assert _run(store.get_state("u-2")) is None
    _run(store.clear("u-1"))
    assert _run(store.get_state("u-1")) is None


def test_state_coerces_corrupt_non_string_learned_values():
    """A corrupt persisted fact must load as text, never crash the turn
    (fail-open at the read boundary)."""
    from miriam_agent.onboarding.state import OnboardingState

    s = OnboardingState({"learned": {"user_sentiment": 7, "cashflow": None}})
    assert s.learned["user_sentiment"] == "7"
    assert "cashflow" not in s.learned
    assert isinstance(s.learned["user_sentiment"], str)


def test_corrupt_learned_value_never_crashes_a_turn(monkeypatch):
    from miriam_agent.onboarding.state import OnboardingState

    user = _user()
    service, states, _, _ = _service(monkeypatch)
    raw = OnboardingState().to_dict()
    raw["stage"] = "interview"
    raw["learned"] = {"user_sentiment": 7, "goal": "Japan trip"}
    states.data["u-1"] = raw
    turn = _run(service.handle_turn(user, message="hello"))
    assert turn.took_over is True


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


def test_state_round_trip_stamps_schema_version():
    from miriam_agent.onboarding.state import (
        SCHEMA_VERSION,
        OnboardingState,
        OnboardingStateStore,
    )

    store = OnboardingStateStore(redis_url="", ttl_days=None)
    s = OnboardingState()
    _run(store.save_state("u-1", s))
    assert s.schema_version == SCHEMA_VERSION
    assert s.to_dict()["schema_version"] == SCHEMA_VERSION
    restored = _run(store.get_state("u-1"))
    assert restored is not None
    assert restored.schema_version == SCHEMA_VERSION
    assert restored.to_dict()["schema_version"] == SCHEMA_VERSION


def test_v1_record_is_migrated_forward_on_load():

    from miriam_agent.onboarding.state import SCHEMA_VERSION, OnboardingState

    v1 = {
        "stage": "interview",
        "name": "Tola",
        "learned": {"cashflow": "4000 a month"},
        "document_summary": "",
        "completed_at": "",
        "conversation_state": {"user_sentiment": "tense"},
    }
    state = OnboardingState(v1)
    assert state.schema_version == SCHEMA_VERSION
    assert state.document_summary is None
    assert state.completed_at is None
    assert state.started_at > 0
    assert state.learned["cashflow"] == "4000 a month"
    assert state.conversation_state["user_sentiment"] == "tense"


def test_store_rewrites_migrated_record():
    from miriam_agent.onboarding.state import SCHEMA_VERSION, OnboardingStateStore

    store = OnboardingStateStore(redis_url="", ttl_days=None)
    v1 = {"stage": "greeting", "document_summary": "", "updated_at": time.time()}
    store._local["miriam:onboarding:state:u-1"] = v1
    restored = _run(store.get_state("u-1"))
    assert restored is not None
    assert restored.schema_version == SCHEMA_VERSION
    migrated = store._local["miriam:onboarding:state:u-1"]
    assert migrated["schema_version"] == SCHEMA_VERSION
    assert migrated["document_summary"] is None


def test_future_schema_version_loads_fail_open():
    from miriam_agent.onboarding.state import OnboardingState

    state = OnboardingState(
        {
            "schema_version": 99,
            "stage": "complete",
            "name": "Dana",
            "some_future_field": "preserved",
        }
    )
    assert state.schema_version == 99
    assert state.name == "Dana"
    assert state.stage == "complete"


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


class BrokenProvider:
    """An LLM that is down; exercises the deterministic fallback."""

    def __init__(self):
        self.calls: list = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append(messages)
        raise RuntimeError("llm unavailable")


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


def _greet(reply, intent="interview", facts=None, taps=None, adjustment=""):
    return json.dumps(
        {
            "reply": reply,
            "intent": intent,
            "facts": facts or {},
            "suggested_replies": taps or [],
            "adjustment": adjustment,
        }
    )


def _d(key, value, intent="interview"):
    return json.dumps(
        {
            "reply": f"{key} done",
            "intent": intent,
            "facts": {key: value},
            "suggested_replies": [],
        }
    )


def _plan_present(reply="Here is your picture: Financial Beginner."):
    return json.dumps({"reply": reply})


# ------------------------------------------------------------------
# Service tests (LLM-led, agent-led conversation)
# ------------------------------------------------------------------


def test_action_intent_bypasses_interview(monkeypatch):
    user = _user()
    service, states, _, _ = _service(monkeypatch)
    for msg in ("send 5k to Tola", "what's my balance?"):
        turn = _run(service.handle_turn(user, message=msg))
        assert turn.took_over is False, msg
    assert states.data == {}


def test_greeting_captures_name_then_agent_opens_money_moment(monkeypatch):
    opener = "Nice to meet you, Tola! What's been on your mind about money lately?"
    provider = FakeProvider([_greet(opener)])
    service, states, memory, _ = _service(monkeypatch, provider)
    user = _user()

    turn = _run(service.handle_turn(user, message="hey"))
    assert turn.took_over is True
    assert "first name" in turn.response.lower()
    assert turn.to_payload("")["name"] == ""
    assert states.data["u-1"]["stage"] == "greeting"

    turn = _run(service.handle_turn(user, message="my name is Tola"))
    assert turn.took_over is True
    assert turn.to_payload("")["name"] == "Tola"
    assert states.data["u-1"].get("name") == "Tola"
    assert states.data["u-1"].get("stage") == "interview"
    # The first question is Miriam's own conversational opener (one question),
    # returned verbatim -- never a canned question from a script.
    assert turn.response == opener
    assert turn.poll is None
    # The conductor led it and saw what it knows (nothing yet).
    assert "WHAT YOU KNOW SO FAR" in provider.calls[-1][-1].content


def test_conversation_is_agent_led_not_scripted(monkeypatch):
    provider = FakeProvider(
        [
            _greet("What's eating you about money lately? Be as raw as you like."),
            _greet("Got it.", facts={"money_moment": "the month runs out first"}),
        ]
    )
    service, states, memory, _ = _service(monkeypatch, provider)
    user = _user()
    _run(service.handle_turn(user, message="hey"))
    turn = _run(service.handle_turn(user, message="my name is Tola"))
    assert "What's eating you" in turn.response  # her words, not a template

    turn = _run(service.handle_turn(user, message="the month runs out first"))
    assert turn.response == "Got it."
    assert states.data["u-1"]["money_moment"] == "the month runs out first"
    # The money moment was lifted out of the free-form facts.
    assert "money_moment" not in states.data["u-1"]["learned"]


def test_greeting_skip_goes_straight_to_conversation(monkeypatch):
    provider = FakeProvider(
        [
            _greet("No problem. What's been on your mind about money lately?"),
        ]
    )
    service, states, _, _ = _service(monkeypatch, provider)
    user = _user()
    _run(service.handle_turn(user, message="hey"))
    turn = _run(service.handle_turn(user, message="skip"))
    assert states.data["u-1"].get("name") == ""
    assert states.data["u-1"].get("stage") == "interview"  # skipped the name step
    assert turn.response.startswith("No problem.")


def test_volunteered_name_mid_interview_captured(monkeypatch):
    """A statement-first user never sees the greeting, so a later explicit
    \"call me ...\" is honored mid-interview and rides on the reply payload."""
    provider = FakeProvider(
        [
            _greet("Thanks for the statement! What's been going on with money?"),
            _greet("Got it.", facts={"money_moment": "it just disappears"}),
        ]
    )
    service, states, memory, _ = _service(monkeypatch, provider)
    user = _user()
    _run(
        service.handle_turn(
            user,
            message="stmt",
            document={"summary": "balance 5000"},
        )
    )
    turn = _run(service.handle_turn(user, message="also, call me Tobi"))
    assert turn.took_over is True
    assert turn.to_payload("")["name"] == "Tobi"
    assert states.data["u-1"].get("name") == "Tobi"


def test_bare_word_is_not_a_name(monkeypatch):
    """A plain answer like \"Calm\" must never be captured as the user's name."""
    provider = FakeProvider(
        [
            _greet("Thanks for the statement! What's been going on with money?"),
            _greet("Got it.", facts={"money_moment": "it just disappears"}),
        ]
    )
    service, states, _, _ = _service(monkeypatch, provider)
    user = _user()
    _run(
        service.handle_turn(
            user,
            message="stmt",
            document={"summary": "balance 5000"},
        )
    )
    turn = _run(service.handle_turn(user, message="Calm"))
    assert turn.to_payload("")["name"] == ""
    assert states.data["u-1"].get("name") == ""


def test_full_conversation_to_standing_rules(monkeypatch):
    """Happy path: Miriam leads the whole interview conversationally, the facts
    she learns are lifted, the statement is skipped, the plan is presented, then
    consent_yes -> automated standing rules."""
    responses = [
        # name captured -> her conversational money-moment opener
        _greet("Great to meet you, Tola! What's been on your mind about money?"),
        # money moment lands
        _greet(
            "So the month ends before the money does. When income lands, does it "
            "come steady or in lumps?",
            facts={
                "money_moment": "the month is over before the money is",
                "cashflow": "pretty irregular",
            },
        ),
        # follow the thread
        _greet(
            "And if nothing came in next month - how long could you float?",
            facts={
                "income": "comes in late, all over the place",
                "involvement": "set it up for me",
            },
        ),
        # enough context -> ask for the statement
        _greet(
            "A statement would make this real. Send one over, or just say skip.",
            intent="request_statement",
            facts={"runway": "maybe a month at best"},
        ),
        # skip the statement
        _greet("No worries, we'll go with what you told me.", intent="present_plan"),
        # plan presented by the engine-presenter
        _plan_present(
            "Here's your picture: Stability Seeker. First we build the floor."
        ),
        # consent
        _greet("Done.", intent="consent_yes"),
    ]
    service, states, memory, _ = _service(monkeypatch, FakeProvider(responses))
    user = _user()

    turn = _run(service.handle_turn(user, message="hey"))
    assert turn.took_over is True
    turn = _run(service.handle_turn(user, message="Tola"))
    assert turn.took_over is True
    assert turn.response == (
        "Great to meet you, Tola! What's been on your mind about money?"
    )

    turn = _run(service.handle_turn(user, message="the month ends before"))
    assert turn.poll is None
    turn = _run(service.handle_turn(user, message="chunks and late"))
    turn = _run(service.handle_turn(user, message="maybe a month"))
    assert states.data["u-1"].get("stage") == "awaiting_statement"
    assert states.data["u-1"].get("money_moment") == (
        "the month is over before the money is"
    )

    turn = _run(service.handle_turn(user, message="Skip for now"))
    assert turn.poll is None
    assert states.data["u-1"].get("stage") == "plan_consent"

    plan = states.data["u-1"]["plan"]
    assert plan["diagnostic_state"] == "Stability Seeker"
    assert plan["automate"] is True
    kinds = {r["kind"] for r in plan["standing_rules"]}
    assert {"buffer", "income_rhythm"} <= kinds

    turn = _run(service.handle_turn(user, message="ok, set it up"))
    assert turn.completed is True
    assert turn.automated is True
    entry_kinds = {e["type"] for e in memory.entries}
    assert {"financial", "goal", "pattern", "onboarding"} <= entry_kinds


def test_statement_as_first_message_starts_conversation(monkeypatch):
    user = _user()
    provider = FakeProvider(
        [
            _greet(
                "Thanks for the statement - I'll build your plan on your real "
                "numbers. What's been on your mind about money lately?"
            ),
        ]
    )
    service, states, _, _ = _service(monkeypatch, provider)
    turn = _run(
        service.handle_turn(
            user,
            message="stmt",
            document={"summary": "balance 5000"},
        )
    )
    assert turn.took_over is True
    assert states.data["u-1"].get("document_summary") == "balance 5000"
    assert states.data["u-1"].get("stage") == "interview"
    assert turn.poll is None  # LLM replies, no deterministic poll
    assert "statement" in turn.response.lower()


def test_statement_mid_interview_builds_plan(monkeypatch):
    user = _user()
    provider = FakeProvider(
        [
            _greet("Ready when you are."),
            _plan_present("Here's your picture: Financial Beginner."),
        ]
    )
    service, states, memory, _ = _service(monkeypatch, provider)
    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="Tola"))  # name -> conductor opener
    assert states.data["u-1"].get("stage") == "interview"
    turn = _run(
        service.handle_turn(
            user,
            message="stmt",
            document={"summary": "balance 5000"},
        )
    )
    assert states.data["u-1"].get("document_summary") == "balance 5000"
    assert states.data["u-1"].get("stage") == "plan_consent"
    assert states.data["u-1"]["plan"]["statement_summary"] == "balance 5000"
    assert turn.poll is None


def test_plan_consent_document_updates_plan(monkeypatch):
    user = _user()
    provider = FakeProvider(
        [
            _greet("What's on your mind about money?"),
            _greet("Got it", intent="request_statement"),
            _greet("ok", intent="present_plan"),
            _plan_present("Here's your picture."),
        ]
    )
    service, states, memory, _ = _service(monkeypatch, provider)
    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="Tola"))
    _run(service.handle_turn(user, message="i run out by the 20th"))
    _run(service.handle_turn(user, message="Skip for now"))
    assert states.data["u-1"].get("stage") == "plan_consent"

    turn = _run(
        service.handle_turn(
            user,
            message="stmt",
            document={"summary": "balance 5000"},
        )
    )
    assert states.data["u-1"]["plan"]["statement_summary"] == "balance 5000"
    assert "real numbers" in turn.response.lower()


def test_adjustment_loop_reworks_and_consents(monkeypatch):
    user = _user()
    provider = FakeProvider(
        [
            _greet("here's your picture", intent="present_plan"),
            _plan_present("Here is your plan."),
            _greet(
                "Sure, I'll fold that in.", intent="adjust", adjustment="bigger buffer"
            ),
            _plan_present("Here is the reworked plan."),
            _greet("Done.", intent="consent_yes"),
        ]
    )
    service, states, memory, _ = _service(monkeypatch, provider)
    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="Tola"))
    assert states.data["u-1"].get("stage") == "plan_consent"

    turn = _run(service.handle_turn(user, message="make the buffer bigger"))
    assert states.data["u-1"].get("adjustments") == ["bigger buffer"]
    assert states.data["u-1"].get("stage") == "plan_consent"

    turn = _run(service.handle_turn(user, message="looks good"))
    assert turn.completed is True
    assert turn.automated is True


def test_vague_adjust_asks_one_clarifying_question(monkeypatch):
    user = _user()
    provider = FakeProvider(
        [
            _greet("ok", intent="present_plan"),
            _plan_present("Here is your plan."),
            _greet("And what should we change?", intent="adjust"),
            _greet("I'll fold that in.", intent="adjust", adjustment="bigger buffer"),
            _plan_present("Here is the reworked plan."),
        ]
    )
    service, states, memory, _ = _service(monkeypatch, provider)
    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="Tola"))

    turn = _run(service.handle_turn(user, message="change something"))
    assert states.data["u-1"].get("stage") == "awaiting_adjustment"
    assert "change" in turn.response.lower()

    turn = _run(service.handle_turn(user, message="bigger buffer"))
    assert states.data["u-1"].get("adjustments") == ["bigger buffer"]
    assert states.data["u-1"].get("stage") == "plan_consent"


def test_abandon(monkeypatch):
    user = _user()
    provider = FakeProvider(
        [
            _greet("my money feels fine"),
            _greet("No stress.", intent="abandon"),
        ]
    )
    service, states, memory, _ = _service(monkeypatch, provider)
    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="Tola"))
    turn = _run(service.handle_turn(user, message="stop this"))
    assert turn.took_over is True
    assert turn.completed is True
    assert states.data["u-1"].get("stage") == "complete"


def test_redo_restarts_finished_interview(monkeypatch):
    user = _user()
    provider = FakeProvider(
        [
            _greet("my money feels fine"),
            _greet("No stress.", intent="abandon"),
            _greet("Welcome back."),
        ]
    )
    service, states, memory, _ = _service(monkeypatch, provider)
    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="Tola"))
    _run(service.handle_turn(user, message="stop this"))
    assert states.data["u-1"].get("stage") == "complete"
    turn = _run(service.handle_turn(user, message="let's start over"))
    assert turn.took_over is True
    assert states.data["u-1"].get("stage") == "interview"


def test_finished_interview_survives_casual_chat(monkeypatch):
    from miriam_agent.onboarding.state import STAGE_COMPLETE, OnboardingState

    user = _user()
    service, states, _, _ = _service(monkeypatch)
    raw = OnboardingState().to_dict()
    raw["stage"] = STAGE_COMPLETE
    raw["name"] = "Tobi"
    raw["goal"] = "Japan trip in 2027"
    states.data["u-1"] = raw

    for text in ("let's do it", "let's go", "try again", "ok go", "sounds fun"):
        turn = _run(service.handle_turn(user, message=text))
        assert turn.took_over is False, text
        assert states.data["u-1"]["stage"] == STAGE_COMPLETE, text
        assert states.data["u-1"]["goal"] == "Japan trip in 2027", text


def test_structured_meta_lifted_from_facts(monkeypatch):
    """spec §6/§7/§22/§29: the agent's reserved meta keys are lifted off the
    free-form facts onto structured state (money-moment read, goal read,
    sentiment, internal money script)."""
    provider = FakeProvider(
        [
            _greet(
                "Got it.",
                facts={
                    "money_moment": "running out before payday",
                    "money_moment_emotion": "frustrated",
                    "money_moment_suspected_problem": "cash_flow",
                    "money_moment_confidence": "0.72",
                    "goal": "buy a car",
                    "goal_target_date": "2027",
                    "goal_estimated_cost": "2.5m",
                    "sentiment": "tense",
                    "money_script": "avoidance",
                },
            ),
        ]
    )
    service, states, memory, _ = _service(monkeypatch, provider)
    user = _user()
    _run(service.handle_turn(user, message="hey"))
    turn = _run(service.handle_turn(user, message="my name is Tola"))
    assert turn.took_over is True

    state = states.data["u-1"]
    assert state["money_moment"] == "running out before payday"
    assert state["money_moment_meta"] == {
        "emotion": "frustrated",
        "suspected_problem": "cash_flow",
        "confidence": 0.72,
    }
    assert state["goal"] == "buy a car"
    assert state["goal_meta"] == {"target_date": "2027", "estimated_cost": "2.5m"}
    assert state["conversation_state"]["user_sentiment"] == "tense"
    assert state["conversation_state"]["money_script"] == "avoidance"
    # Structured keys are consumed off the free-form fact set.
    learned = state["learned"]
    assert learned == {}  # every fact this turn was lifted onto structured fields
    for k in (
        "money_moment_emotion",
        "money_moment_suspected_problem",
        "money_moment_confidence",
        "goal_target_date",
        "goal_estimated_cost",
        "sentiment",
        "money_script",
    ):
        assert k not in learned
    # The script is remembered under a stable, internal label.
    assert any(
        e["metadata"].get("question_id") == "money_script" for e in memory.entries
    )


def test_conversation_state_escalates_and_directness_levels(monkeypatch):
    """spec §29/§12: the conversation read recomputes each turn. Early turns are
    tentative (directness 1-2); once the plan lands, the problem/insight are
    named, the relationship is established, and severity pushes directness up."""
    from miriam_agent.config.settings import get_settings

    settings = get_settings()
    original = settings.ONBOARDING_MAX_QUESTIONS
    monkeypatch.setattr(settings, "ONBOARDING_MAX_QUESTIONS", 30)
    try:
        responses = [
            _greet(
                "What's up?",
                facts={
                    "money_moment": "paycheck to paycheck",
                    "cashflow": "runs out fast",
                },
            ),
            _greet("Hmm", facts={"debt": "cards are maxed"}),
            _greet("Let's get real.", intent="request_statement"),
            _greet("ok", intent="present_plan"),
            _plan_present("Here's your plan."),
        ]
        service, states, _, _ = _service(monkeypatch, FakeProvider(responses))
        user = _user()
        _run(service.handle_turn(user, message="hey"))
        _run(service.handle_turn(user, message="Tola"))
        cs = states.data["u-1"]["conversation_state"]
        # Money moment lifted; interview stage read exists and starts measured.
        assert cs["current_topic"] == "cash_flow"
        assert cs["current_problem"]
        assert cs["relationship_stage"] in ("new", "getting_to_know")
        assert 1 <= cs["directness_level"] <= 2
        assert cs["confidence"] is not None

        _run(service.handle_turn(user, message="it's tight"))
        _run(service.handle_turn(user, message="really tight"))
        _run(service.handle_turn(user, message="Skip for now"))

        state = states.data["u-1"]
        assert state["stage"] == "plan_consent"
        cs = state["conversation_state"]
        assert cs["relationship_stage"] == "established"
        assert cs["last_insight"] == "saving happens last"
        assert cs["current_topic"] == "cash_flow"
        assert cs["pending_action"]
        # Short runway + debt => severity high, directness peaks for the blunt
        # naming of the problem.
        assert cs["directness_level"] >= 3
        assert state["plan"]["insight"]["severity"] == "high"
    finally:
        monkeypatch.setattr(settings, "ONBOARDING_MAX_QUESTIONS", original)


def _aha_flow_provider():
    return FakeProvider(
        [
            _greet("ready?"),
            _greet("ok", intent="request_statement"),
            _greet("ok", intent="present_plan"),
            _plan_present("Here you go."),
        ]
    )


def test_aha_generated_event_on_plan_present(monkeypatch):
    """spec §27: success is measured as a real insight, not a completed form;
    the deterministic plan reveal emits the aha event."""
    from miriam_agent.observability import metrics as metrics_mod

    events: list[dict] = []

    class FakeCounter:
        def labels(self, **kw):
            self.last = kw
            return self

        def inc(self):
            events.append(self.last)

    monkeypatch.setattr(metrics_mod, "ONBOARDING_EVENTS", FakeCounter())
    service, states, _, _ = _service(monkeypatch, _aha_flow_provider())
    user = _user()
    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="Tola"))
    _run(service.handle_turn(user, message="money is tight"))
    _run(service.handle_turn(user, message="Skip for now"))
    assert states.data["u-1"]["stage"] == "plan_consent"
    recorded = [e["event"] for e in events]
    assert "aha_generated" in recorded
    assert "plan_presented" in recorded


# -----------------------------------------------------------------------
# Fallback (LLM down or garbage): short, warm, still completes the flow
# -----------------------------------------------------------------------


def test_fallback_llm_down_completes_flow(monkeypatch):
    user = _user()
    service, states, memory, prov = _service(monkeypatch, BrokenProvider())

    turn = _run(service.handle_turn(user, message="hey"))
    assert turn.took_over is True and "first name" in turn.response.lower()

    # Name captured deterministically, greeting handoff is still warm.
    turn = _run(service.handle_turn(user, message="Tola"))
    assert turn.took_over is True
    assert turn.response.startswith("Nice to meet you, Tola")
    assert "money lately" in turn.response.lower()
    assert states.data["u-1"].get("stage") == "interview"

    # Reply consumed into the money moment, next question asked once.
    turn = _run(service.handle_turn(user, message="I'm drowning in rent"))
    assert states.data["u-1"].get("money_moment") == "I'm drowning in rent"
    assert "working toward" in turn.response.lower()

    turn = _run(service.handle_turn(user, message="I just want to breathe"))
    assert states.data["u-1"].get("goal") == "I just want to breathe"
    assert states.data["u-1"].get("stage") == "awaiting_statement"

    # Never loops on the same question: a second "no" goes straight to plan.
    turn = _run(service.handle_turn(user, message="Skip for now"))
    assert states.data["u-1"].get("stage") == "plan_consent"
    assert states.data["u-1"]["plan"]["diagnostic_state"] == "Financial Beginner"

    turn = _run(service.handle_turn(user, message="Yes, set it up"))
    assert turn.completed is True
    assert turn.automated is True
    assert memory.entries


def test_interview_cap_closes_conversation(monkeypatch):
    from miriam_agent.config.settings import get_settings

    settings = get_settings()
    original = settings.ONBOARDING_MAX_QUESTIONS
    monkeypatch.setattr(settings, "ONBOARDING_MAX_QUESTIONS", 3)
    try:
        user = _user()
        provider = FakeProvider()  # default replies stay on "interview"
        service, states, _, _ = _service(monkeypatch, provider)
        _run(service.handle_turn(user, message="hey"))
        _run(service.handle_turn(user, message="Tola"))
        _run(service.handle_turn(user, message="m1"))
        _run(service.handle_turn(user, message="m2"))
        turn = _run(service.handle_turn(user, message="m3"))
        assert states.data["u-1"].get("stage") == "plan_consent"
        assert states.data["u-1"].get("plan_presented") is True
        assert turn.poll is None
        # The moving-on hint was visible to the agent during the last turn it got.
        last_conductor = next(
            m[-1].content for m in provider.calls[-2:] if m[0].role == "system"
        )
        assert "MOVING ON:" in last_conductor or "interview_turns" in last_conductor
    finally:
        monkeypatch.setattr(settings, "ONBOARDING_MAX_QUESTIONS", original)


# -----------------------------------------------------------------------
# Driver: parsing, classification, and validation
# -----------------------------------------------------------------------


def test_driver_parse_fenced_json_and_facts():
    from miriam_agent.onboarding import driver

    text = (
        '```json\n{"reply": "What has been bothering you about money lately?", '
        '"intent": "interview", "facts": {"money_moment": "it is tight"},'
        ' "suggested_replies": ["Calm"]}\n```'
    )
    out = driver._parse_driver_output(text, "interview")
    assert out is not None
    assert out.reply == "What has been bothering you about money lately?"
    assert out.facts == {"money_moment": "it is tight"}
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


def test_driver_cleans_facts_and_clamps_taps():
    from miriam_agent.onboarding import driver

    payload = {
        "reply": "q",
        "intent": "interview",
        "facts": {
            "cashflow": "irregular",  # free-form keys are fine
            "goal": "Buy a house",
            "": "no key",  # blank key dropped
            "   ": "no key either",
            "x": "",  # blank value dropped
        },
        "suggested_replies": ["a" * 100, "b", "c", "d", "e"],
    }
    out = driver._parse_driver_output(json.dumps(payload), "interview")
    assert set(out.facts) == {"cashflow", "goal"}
    assert out.facts["goal"] == "Buy a house"
    assert len(out.suggested) == 4
    assert len(out.suggested[0]) <= driver.MAX_TAP_LENGTH
    assert out.suggested[0].endswith("\u2026")


def test_driver_fact_bounds():
    from miriam_agent.onboarding import driver

    long_value = "y" * (driver.MAX_FACT_VALUE_LENGTH + 50)
    many = {"long": long_value}
    for i in range(30):
        many[f"k{i}"] = f"v{i}"
    out = driver._parse_driver_output(
        json.dumps({"reply": "q", "facts": many}), "interview"
    )
    assert out is not None
    assert len(out.facts) <= driver.MAX_FACTS_PER_TURN
    assert len(out.facts["long"]) <= driver.MAX_FACT_VALUE_LENGTH
    assert out.facts["long"].endswith("\u2026")


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
    assert out.reply.endswith("\u2026")
    out2 = driver._parse_driver_output(json.dumps({"reply": long}), "interview")
    assert out2.reply == long


def test_driver_reaction_validated_against_whitelist():
    from miriam_agent.onboarding import driver

    out = driver._parse_driver_output(
        json.dumps({"reply": "nice", "reaction": "👍"}), "interview"
    )
    assert out.reaction == "👍"
    out = driver._parse_driver_output(
        json.dumps({"reply": "nice", "reaction": " 👍 "}), "interview"
    )
    assert out.reaction == "👍"
    # A non-tapback emoji never rides through as a reaction.
    out = driver._parse_driver_output(
        json.dumps({"reply": "nice", "reaction": "🎉"}), "interview"
    )
    assert out.reaction == ""
    out = driver._parse_driver_output(json.dumps({"reply": "nice"}), "interview")
    assert out.reaction == ""


def test_present_plan_reaction_validated():
    from miriam_agent.onboarding import driver

    out = driver._parse_present_text(
        json.dumps({"reply": "here is your plan", "reaction": "❤️"})
    )
    assert out.reaction == "❤️"
    out = driver._parse_present_text(
        json.dumps({"reply": "here is your plan", "reaction": "✨"})
    )
    assert out.reaction == ""


def test_conductor_turn_sends_stage_and_knows():
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
    state.learned["cashflow"] = "about 4000 a month"
    provider = Capture()
    out = _run(
        driver.conductor_turn(
            provider=provider, state=state, history=[], user_text="send it"
        )
    )
    assert out is not None and out.intent == "request_statement"
    user_block = provider.messages[-1].content
    assert "awaiting_statement" in user_block
    assert "WHAT YOU KNOW SO FAR" in user_block
    assert "about 4000 a month" in user_block
    # No dimension checklist is fed to the model anymore.
    assert "DIMENSIONS STILL TO COVER" not in user_block


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


def test_turn_carries_reaction_messages_and_share(monkeypatch):

    service, states, _, _ = _service(monkeypatch)
    share = {"kind": "plan", "title": "Your plan", "url": "https://plans.example/u-1"}
    turn = service._turn(
        "Here is your picture: Financial Beginner.",
        stage="plan_consent",
        reaction="❤️",
        share=share,
    )
    payload = turn.to_payload("c-1")
    assert payload["reaction"] == "❤️"
    assert payload["share"] == share
    assert payload["messages"] == []


def test_turn_enriches_long_reply_into_bubbles(monkeypatch):
    service, states, _, _ = _service(monkeypatch)
    s1 = (
        "First we protect the next month, because that is the hardest stretch "
        "and the floor keeps everything else above water."
    )
    s2 = "Then we smooth your income so a late check stops wrecking the week."
    s3 = "And if nothing lands, the buffer covers you while we fix the rhythm."
    reply = " ".join([s1, s2, s3])
    assert len(reply) >= 140
    turn = service._turn(reply, stage="plan_consent")
    assert turn.response == reply.split(". ")[0] + "."
    assert turn.messages, "a long multi-sentence reply must split into bubbles"
    assert len(turn.messages) <= 2
    assert all(b.strip() == b for b in turn.messages)


def test_plan_share_requires_configuration(monkeypatch):
    from miriam_agent.config.settings import Settings

    service, states, _, _ = _service(monkeypatch)
    # Default deployment: base URL empty -> no share is emitted at all.
    monkeypatch.setattr(
        "miriam_agent.onboarding.service.get_settings",
        lambda: Settings(ONBOARDING_SHARE_BASE_URL=""),
    )
    assert service._plan_share("u-1") is None

    monkeypatch.setattr(
        "miriam_agent.onboarding.service.get_settings",
        lambda: Settings(ONBOARDING_SHARE_BASE_URL="https://plans.example.com/"),
    )
    share = service._plan_share("u-1")
    assert share == {
        "kind": "plan",
        "title": "Your plan",
        "url": "https://plans.example.com/u-1",
    }


def test_reaction_and_share_flow_into_present_plan_payload(monkeypatch):
    from miriam_agent.config.settings import Settings

    monkeypatch.setattr(
        "miriam_agent.onboarding.service.get_settings",
        lambda: Settings(ONBOARDING_SHARE_BASE_URL="https://plans.example.com"),
    )
    responses = [
        _greet("Great to meet you, Tola! What's on your mind about money?"),
        _greet(
            "So the month ends before the money does. Steady or in lumps?",
            facts={
                "money_moment": "month ends before the money",
                "cashflow": "lumpy",
            },
        ),
        _greet(
            "And if nothing came in next month, how long could you float?",
            facts={"income": "commissions, late"},
        ),
        _greet(
            "A statement would make this real. Send one over, or just say skip.",
            intent="request_statement",
            facts={"runway": "maybe a month at best"},
        ),
        _greet("No worries, we go with what you told me.", intent="present_plan"),
        json.dumps(
            {
                "reply": "Here is your picture: Stability Seeker.",
                "reaction": "❤️",
            }
        ),
    ]
    service, states, memory, _ = _service(monkeypatch, FakeProvider(responses))
    user = _user()

    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="Tola"))
    _run(service.handle_turn(user, message="the month ends before"))
    _run(service.handle_turn(user, message="chunks and late"))
    _run(service.handle_turn(user, message="maybe a month"))
    turn = _run(service.handle_turn(user, message="Skip for now"))
    assert states.data["u-1"].get("stage") == "plan_consent"
    assert turn.response == "Here is your picture: Stability Seeker."
    payload = turn.to_payload("c-1")
    assert payload["reaction"] == "❤️"
    assert payload["share"] == {
        "kind": "plan",
        "title": "Your plan",
        "url": "https://plans.example.com/u-1",
    }


# ------------------------------------------------------------------
# Review fixes: HIGH-1 adjustments rework the plan, HIGH-2 mid-flow
# money actions bypass, MED-1 name prose, MED-2 meta sanitizing
# ------------------------------------------------------------------


def test_plan_adjustment_removal_reworks_steps():
    from miriam_agent.onboarding.plan import build_plan

    facts = {
        "income": "all over the place, commissions come late",
        "involvement": "set it up for me",
    }
    plan = build_plan(facts, adjustments=["drop the weekly check-in"])
    ids = {s["id"] for s in plan["steps"]}
    assert "checkin" not in ids
    assert "buffer" in ids  # the base capability survives
    assert {"buffer", "income_rhythm"} <= {r["kind"] for r in plan["standing_rules"]}
    assert plan["adjustments"] == ["drop the weekly check-in"]

    # A rejected move prunes both its step and its standing rule.
    plan = build_plan(
        {
            "income": "steady, six months saved",
            "debt": "credit card balances are high",
            "involvement": "set it up for me",
        },
        adjustments=["cut the debt step"],
    )
    assert "debt" not in {s["id"] for s in plan["steps"]}
    assert "debt" not in {r["kind"] for r in plan["standing_rules"]}


def test_plan_adjustment_cannot_remove_foundation_or_goal():
    from miriam_agent.onboarding.plan import build_plan

    facts = {
        "income": "steady, six months of runway saved",
        "debt": "card debt piling up",
        "involvement": "set it up for me",
    }
    plan = build_plan(
        facts,
        goal="invest and build wealth",
        adjustments=["no buffer", "drop the goal", "cut the debt step"],
    )
    ids = {s["id"] for s in plan["steps"]}
    assert "buffer" in ids and "goal" in ids  # foundations can't be removed
    assert "debt" not in ids  # a removable move still honors the note


def test_plan_adjustment_matching_nothing_records_note_but_keeps_plan():
    from miriam_agent.onboarding.plan import build_plan

    plan = build_plan({"involvement": "keep it light"}, adjustments=["bigger buffer"])
    assert any(s["id"] == "checkin" for s in plan["steps"])
    assert plan["adjustments"] == ["bigger buffer"]


def test_plan_adjustment_keep_note_never_removes_a_move():
    from miriam_agent.onboarding.plan import build_plan

    # "Keep the check-in" names the move but is an affirmation, not a removal --
    # and a later reversal must not be able to prune it either.
    plan = build_plan(
        {"involvement": "keep it light"}, adjustments=["keep the weekly check-in"]
    )
    assert any(s["id"] == "checkin" for s in plan["steps"])


def test_plan_adjustment_hedge_never_removes_a_move():
    from miriam_agent.onboarding.plan import build_plan

    # "keep the check-in, just not every week" refines the cadence; it must not
    # prune the whole move (a wrong removal is costlier than a kept one).
    plan = build_plan(
        {"involvement": "keep it light"},
        adjustments=["keep the check-in, just not every week"],
    )
    assert any(s["id"] == "checkin" for s in plan["steps"])
    # Retention hedges protect equally explicit removals in the same note.
    plan2 = build_plan(
        {"involvement": "keep it light"},
        adjustments=["no rush on the check-in, keep it"],
    )
    assert any(s["id"] == "checkin" for s in plan2["steps"])


def test_plan_adjustment_matches_word_variants():
    from miriam_agent.onboarding.plan import build_plan

    # "checkin" without a hyphen, "check in" with a space, and "debt" all name
    # the moves they reject.
    for note in (
        "drop the monthly checkin",
        "drop the check in",
        "remove the check-in",
    ):
        plan = build_plan({"involvement": "keep it light"}, adjustments=[note])
        assert not any(s["id"] == "checkin" for s in plan["steps"]), note
    plan = build_plan(
        {"debt": "some credit card debt", "involvement": "set it up for me"},
        adjustments=["cut the debt step"],
    )
    assert not any(s["id"] == "debt" for s in plan["steps"])


def test_plan_debt_rule_matches_set_up_by_credit_reliance():
    from miriam_agent.onboarding.plan import build_plan

    # A hands-on user who leans on credit gets the debt step AND the matching
    # standing rule (both keyed off the same signal).
    plan = build_plan(
        {
            "income": "all over the place",
            "cashflow": "cover gaps with an advance",
            "involvement": "set it up for me",
        }
    )
    assert any(s["id"] == "debt" for s in plan["steps"])
    assert any(r["kind"] == "debt" for r in plan["standing_rules"])


def test_adjustment_removal_actually_changes_presented_plan(monkeypatch):
    user = _user()
    provider = FakeProvider(
        [
            _greet("here's your picture", intent="present_plan"),
            _plan_present("Here is your plan."),
            _greet("Sure.", intent="adjust", adjustment="drop the weekly check-in"),
            _plan_present("Here is the reworked plan."),
        ]
    )
    service, states, _, _ = _service(monkeypatch, provider)
    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="Tola"))
    turn = _run(service.handle_turn(user, message="make the plan lighter"))
    reworked = states.data["u-1"]["plan"]
    assert {s["id"] for s in reworked["steps"]}.isdisjoint({"checkin"})
    assert states.data["u-1"]["adjustments"] == ["drop the weekly check-in"]
    assert turn.response == "Here is the reworked plan."


def test_wants_money_action_is_polar():
    from miriam_agent.onboarding.service import _wants_money_action

    actions = {
        "send 500 to mom",
        "transfer 200 to my account",
        "withdraw 100 now",
        "buy 0.1 btc",
        "invest 500 in an ETF",
        "deposit 50 into savings",
        "put 300 in the emergency fund",
        "move 200 to rent",
        "what's my balance?",
    }
    replies = {
        "send it",
        "send it now",
        "yes",
        "skip for now",
        "later",
        "I'll pay off my debt next month",
        "pay attention to my spending",
        "looks good",
        "set it up",
        # Habitual / scheduled / future phrasing is the user describing their
        # life, not instructing a now-transfer -- it must stay in the chat.
        "I pay the rent every month",
        "I pay rent on the first",
        "I will buy etf tomorrow",
        "I invest in my etf each month",
    }
    for msg in actions:
        assert _wants_money_action(msg), msg
    for msg in replies:
        assert not _wants_money_action(msg), msg
    # A no-amount beneficiary request still reads as an ask, not a description.
    assert _wants_money_action("can you pay him back"), "pay him back"


def test_mid_flow_money_action_bypasses_to_agent(monkeypatch):
    from miriam_agent.onboarding.state import OnboardingState

    user = _user()
    service, states, _, _ = _service(monkeypatch)
    for stage, msg in (
        ("interview", "send 500 to mom please"),
        ("plan_consent", "transfer 200 to my account"),
        ("awaiting_statement", "buy 0.1 btc"),
        ("awaiting_adjustment", "what's my balance?"),
    ):
        raw = OnboardingState().to_dict()
        raw["stage"] = stage
        states.data["u-1"] = raw
        turn = _run(service.handle_turn(user, message=msg))
        assert turn.took_over is False, (stage, msg)


def test_stage_answers_are_never_hijacked_as_money_actions(monkeypatch):
    from miriam_agent.onboarding.state import OnboardingState

    user = _user()
    provider = FakeProvider()
    service, states, _, _ = _service(monkeypatch, provider)
    raw = OnboardingState().to_dict()
    raw["stage"] = "awaiting_statement"
    states.data["u-1"] = raw
    # A poll vote on the statement ask is an answer, never a transfer.
    turn = _run(service.handle_turn(user, message="send it", is_poll_vote=True))
    assert turn.took_over is True and len(provider.calls) == 1


def test_extract_name_handles_prose_greetings():
    from miriam_agent.onboarding.service import OnboardingService

    cases = {
        "I'm Tobi, nice to meet you": "Tobi",
        "It's Tobi!": "Tobi",
        "It's Tobi, great to meet you": "Tobi",
        "my name is Tobi Ademi": "Tobi Ademi",
        "Call me Dana.": "Dana",
        "I am Tobiloba": "Tobiloba",
    }
    for text, expected in cases.items():
        assert OnboardingService._extract_name(text) == expected, text


def test_meta_float_rejects_non_numeric():
    from miriam_agent.onboarding.service import OnboardingService

    assert OnboardingService._meta_float("0.72") == 0.72
    assert OnboardingService._meta_float("high") is None
    assert OnboardingService._meta_float("") is None


def test_invalid_meta_sanitized_instead_of_kept_raw(monkeypatch):
    from miriam_agent.onboarding.contracts import MoneyMomentMeta

    provider = FakeProvider(
        [
            _greet(
                "Got it.",
                facts={
                    "money_moment": "running out before payday",
                    "money_moment_confidence": "high",
                    "money_moment_emotion": "frustrated",
                },
            )
        ]
    )
    service, states, _, _ = _service(monkeypatch, provider)
    user = _user()
    _run(service.handle_turn(user, message="hey"))
    _run(service.handle_turn(user, message="my name is Tola"))
    meta = states.data["u-1"]["money_moment_meta"]
    # The junk string confidence is dropped; the valid emotion survives; what
    # persists passes the contract (never the raw bad dict).
    assert meta == {"emotion": "frustrated"}
    assert "confidence" not in meta
    assert "money_moment_confidence" not in states.data["u-1"]["learned"]
    MoneyMomentMeta.model_validate(meta)

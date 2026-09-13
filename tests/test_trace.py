"""Tests for the append-only onboarding trace and the prompt-drift tooling.

Every LLM-led onboarding turn is traced with the prompt version that produced
it, so behavior drift can be caught and pinned to a prompt edit before it ships
(``python -m miriam_agent.onboarding.trace report|compare``). These tests cover
the store, the version hashes, the service writing correct trace records, and
the summarize/drift math.
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
# Trace store
# -----------------------------------------------------------------------


def test_trace_store_round_trip_newest_first():
    from miriam_agent.onboarding.trace import OnboardingTraceStore, TraceRecord

    store = OnboardingTraceStore(use_redis=False)
    for i in range(3):
        _run(
            store.append(
                TraceRecord(
                    user_id="u",
                    mode="conductor",
                    stage="interview",
                    intent="interview",
                    reply=f"turn {i}",
                    prompt_version="abc",
                )
            )
        )
    entries = _run(store.list_entries(limit=10))
    assert [e.reply for e in entries] == ["turn 2", "turn 1", "turn 0"]
    _run(store.clear())
    assert _run(store.list_entries(limit=10)) == []


def test_trace_store_is_capped():
    from miriam_agent.onboarding.trace import OnboardingTraceStore, TraceRecord

    store = OnboardingTraceStore(max_entries=3, use_redis=False)
    for i in range(6):
        _run(
            store.append(
                TraceRecord(
                    user_id="u",
                    mode="conductor",
                    stage="interview",
                    intent="interview",
                    reply=f"turn {i}",
                    prompt_version="abc",
                )
            )
        )
    entries = _run(store.list_entries(limit=10))
    assert [e.reply for e in entries] == ["turn 5", "turn 4", "turn 3"]


def test_trace_record_dict_round_trip():
    from miriam_agent.onboarding.trace import TraceRecord

    record = TraceRecord(
        user_id="u",
        mode="present",
        stage="plan_consent",
        intent="present_plan",
        reply="hi",
        prompt_version="abc",
        violations=["R10"],
        clamped=True,
        facts={"cashflow": "lumpy"},
        taps=["Yes"],
    )
    restored = TraceRecord.from_dict(json.loads(json.dumps(record.to_dict())))
    assert restored == record


# -----------------------------------------------------------------------
# Prompt version hashes
# -----------------------------------------------------------------------


def test_prompt_version_distinguishes_modes_and_is_stable():
    from miriam_agent.onboarding import driver

    conductor = driver.prompt_version("conductor")
    present = driver.prompt_version("present")
    combined = driver.prompt_version("combined")
    assert conductor == driver.prompt_version("conductor")  # stable across calls
    assert len(conductor) == 12 and present.isalnum()
    assert conductor != present
    assert combined != conductor and combined != present


# -----------------------------------------------------------------------
# Service writes correct trace records
# -----------------------------------------------------------------------


@dataclass
class FakeTrace:
    records: list = field(default_factory=list)

    async def append(self, record):
        from miriam_agent.onboarding.trace import TraceRecord

        self.records.append(
            record if isinstance(record, TraceRecord) else TraceRecord.from_dict(record)
        )

    async def list_entries(self, limit=200):
        return list(reversed(self.records[-limit:]))

    async def clear(self):
        self.records = []


# -----------------------------------------------------------------------
# Service-flow helpers (the same canned-provider pattern as the other tests)
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
    def __init__(self, responses: list[str] | None = None):
        self._responses = list(responses or [])
        self.calls: list = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append(messages)
        from miriam_agent.agents.llm import LLMResponse

        content = self._responses.pop(0) if self._responses else _r("interview")
        return LLMResponse(content=content)


def _service(monkeypatch, provider=None, trace=None):
    from miriam_agent.onboarding.service import OnboardingService

    states = FakeStates()
    memory = FakeMemory()
    monkeypatch.setattr(
        "miriam_agent.onboarding.service.get_onboarding_state_store",
        lambda: states,
    )
    svc = OnboardingService(
        memory,
        provider=provider or FakeProvider(),
        trace_store=trace or FakeTrace(),
    )
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


def _drive(monkeypatch, responses, messages, trace=None):
    service, states = _service(monkeypatch, FakeProvider(responses), trace=trace)
    user = _user()
    turn = None
    for message in messages:
        turn = _run(service.handle_turn(user, message=message))
    return service, states.data["u-1"], turn


def _conductor_to_present():
    return [
        _r("interview", reply="the opener"),
        _r("interview", facts={"cashflow": "roughly 4000/month"}),
        _r("present_plan"),
        _plan_present(),
    ]


def test_service_records_conductor_and_present_traces(monkeypatch):
    from miriam_agent.onboarding import driver

    trace = FakeTrace()
    _, _, _ = _drive(
        monkeypatch,
        _conductor_to_present(),
        ["hey", "Tola", "income is lumpy", "wrap it"],
        trace=trace,
    )
    modes = [r.mode for r in trace.records]
    assert modes.count("conductor") == 3  # opener, facts, present-intent
    assert modes.count("present") == 1
    conductor = [r for r in trace.records if r.mode == "conductor"]
    expected = driver.prompt_version("conductor")
    assert all(r.prompt_version == expected for r in conductor)
    assert all(r.violations == [] for r in trace.records)
    latest = trace.records[-1]
    assert latest.mode == "present"
    assert latest.stage == "plan_consent"
    assert latest.intent == "present_plan"
    assert latest.clamped is False
    assert "4000" in latest.grounded


def test_service_trace_marks_clamped_drift(monkeypatch):
    responses = [
        _r("interview", reply="the opener"),
        _r("interview", facts={"cashflow": "roughly 4000/month"}),
        _r("present_plan"),
        json.dumps({"reply": "Lock in guaranteed 8% APY. Shall I set it up?"}),
    ]
    trace = FakeTrace()
    service, _, turn = _drive(
        monkeypatch,
        responses,
        ["hey", "Tola", "income is lumpy", "wrap it"],
        trace=trace,
    )
    assert "APY" not in turn.response
    present = [r for r in trace.records if r.mode == "present"][0]
    assert present.clamped is True
    assert "R10" in present.violations


def test_service_trace_records_drifting_conductor_reply(monkeypatch):
    drifting = _r("interview", reply="You should just budget, ok?")
    responses = [_r("interview", reply="the opener"), drifting]
    trace = FakeTrace()
    _, _, _ = _drive(monkeypatch, responses, ["hey", "Tola", "more"], trace=trace)
    conductor = [r for r in trace.records if r.mode == "conductor"]
    assert any("R4" in r.violations for r in conductor)


# -----------------------------------------------------------------------
# Drift tooling
# -----------------------------------------------------------------------


def _record(version, *, reply="hi", violations=None, mode="conductor"):
    from miriam_agent.onboarding.trace import TraceRecord

    return TraceRecord(
        user_id="u",
        mode=mode,
        stage="interview",
        intent="interview",
        reply=reply,
        prompt_version=version,
        violations=list(violations or []),
    )


def test_summarize_groups_counts_and_rules():
    from miriam_agent.onboarding.trace import summarize

    entries = [
        _record("aaa", violations=["R4"]),
        _record("aaa"),
        _record("bbb", violations=["R4", "R8"]),
    ]
    summary = summarize(entries)
    assert summary["totals"] == {"turns": 3, "clean": 1, "violating": 2}
    aaa = summary["by_version"]["conductor/aaa"]
    assert aaa["turns"] == 2 and aaa["by_rule"] == {"R4": 1}


def test_drift_between_versions_reports_deltas():
    from miriam_agent.onboarding.trace import drift_between

    entries = [
        _record("old1", violations=["R4"]),
        _record("old1"),
        _record("new1", violations=["R4"]),
        _record("new1", violations=["R8"]),
        _record("new1"),
    ]
    diff = drift_between(entries, "conductor/old1", "conductor/new1")
    assert diff["old"]["entries"] == 2
    assert diff["new"]["entries"] == 3
    assert diff["by_rule"]["R4"]["delta"] == round(1 / 3 - 1 / 2, 4)
    assert diff["by_rule"]["R8"]["delta"] == round(1 / 3, 4)


def test_drift_between_missing_version_is_empty():
    from miriam_agent.onboarding.trace import drift_between

    entries = [_record("old1")]
    diff = drift_between(entries, "conductor/missing", "conductor/old1")
    assert diff["new"]["entries"] == 1 and diff["old"]["entries"] == 0


def test_cli_commands_run(monkeypatch):
    import miriam_agent.onboarding.trace as trace_module
    from miriam_agent.onboarding.trace import TraceRecord, main

    store = FakeTrace()
    for i in range(4):
        _run(
            store.append(
                TraceRecord(
                    user_id="u",
                    mode="conductor",
                    stage="interview",
                    intent="interview",
                    reply=f"t{i}",
                    prompt_version="aaa" if i < 2 else "bbb",
                    violations=["R4"] if i == 1 else [],
                )
            )
        )
    monkeypatch.setattr(trace_module, "get_onboarding_trace_store", lambda: store)

    assert main([]) == 0
    assert main(["list", "--limit", "2"]) == 0
    assert main(["report"]) == 0
    assert main(["compare", "conductor/aaa", "conductor/bbb"]) == 0
    assert main(["compare", "conductor/aaa", "conductor/nope"]) == 1

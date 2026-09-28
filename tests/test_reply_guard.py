"""The reply guard's rule set, and the two paths that apply it.

Covers what the guard added on top of "figure with no source": a figure attached
to the wrong label, a claim that an action happened, the figures written in
compressed or spelled form, and the streaming path, which now holds each
sentence back until it has been checked.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.agents.agent_loop import Agent
from miriam_agent.agents.llm import LLMResponse
from miriam_agent.judgment.gates import EGRESS_DONT_KNOW
from miriam_agent.safety import grounding
from miriam_agent.tools import build_tool_registry


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _ScriptedProvider:
    model = "mock-v1"

    def __init__(self, replies=None, chunks=None):
        self._replies = list(replies or [])
        self._chunks = list(chunks or [])
        self.calls: list[list] = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append(messages)
        if not self._replies:
            return LLMResponse(content="All done.", model=self.model)
        return LLMResponse(content=self._replies.pop(0), model=self.model)

    async def stream(self, messages, tools=None, temperature=None, max_tokens=None):
        for chunk in self._chunks:
            yield {"type": "token", "content": chunk}
        yield {"type": "done"}

    def cost_estimate(self, usage):
        return 0.0


async def _collect_stream(agent, **kwargs) -> tuple[str, str, list[dict]]:
    """Return (streamed text, done payload, events)."""
    events: list[dict] = []
    async for event in agent.stream_run(**kwargs):
        events.append(event)
    streamed = "".join(event["content"] for event in events if event["type"] == "token")
    done = next((event["content"] for event in events if event["type"] == "done"), "")
    return streamed, done, events


def _full(result) -> str:
    parts = [result.response or ""]
    parts.extend(result.messages or [])
    return "\n".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# Figures in compressed and spelled form
# ---------------------------------------------------------------------------


def test_compressed_magnitudes_are_figures():
    claims = {c.value for c in grounding.figure_claims("You have 30k available")}
    assert claims == {"30000"}
    assert {c.value for c in grounding.figure_claims("$1.5m")} == {"1500000"}


def test_minutes_are_not_millions():
    """ "5m" needs a currency mark; "30k" does not, because in a money chat it
    is always thirty thousand naira and never a race."""
    assert grounding.figure_claims("see you in 5m") == [
        c for c in grounding.figure_claims("see you in 5m") if c.value == "5"
    ]
    assert {c.value for c in grounding.figure_claims("see you in 5m")} == {"5"}


def test_spelled_figure_past_a_filler():
    claims = {c.value for c in grounding.figure_claims("ten thousand in your stash")}
    assert claims == {"10000"}


def test_spelled_magnitudes_multiply():
    assert {c.value for c in grounding.figure_claims("twenty grand")} == {"20000"}
    assert {c.value for c in grounding.figure_claims("twenty k naira")} == {"20000"}


def test_miriams_own_phrasing_is_never_a_figure():
    for prose in (
        "Two moves first - lock the month away.",
        "One idea, then one question.",
        "I'll take another look in three days.",
        "1. lock the month away\n2. split the lumps",
    ):
        assert grounding.figure_claims(prose) == [], prose


# ---------------------------------------------------------------------------
# Mislabelled figures
# ---------------------------------------------------------------------------


def test_a_figure_on_the_wrong_wallet_is_a_conflict():
    conflicts = grounding.label_conflicts(
        "You have 12,500 in your stash.",
        '{"spend": {"balance": 12500}, "stash": {"balance": 400}}',
    )
    assert [c.text for c in conflicts] == ["12,500"]


def test_a_figure_on_the_wrong_period_is_a_conflict():
    conflicts = grounding.label_conflicts(
        "That works out to 30,000 a month.", '{"income_weekly": 30000}'
    )
    assert [c.text for c in conflicts] == ["30,000"]


def test_the_same_period_in_another_mood_is_not_a_conflict():
    """ "a month" and "income_monthly" are one claim, not two."""
    assert (
        grounding.label_conflicts(
            "That works out to 30,000 a month.", '{"income_monthly": 30000}'
        )
        == []
    )


def test_generic_wrappers_never_contradict():
    assert (
        grounding.label_conflicts("Your balance is 12,500.", '{"spend": 12500}') == []
    )


def test_a_source_that_says_nothing_about_a_label_cannot_contradict_it():
    assert grounding.label_conflicts("12,500 in your stash", '{"x": 12500}') == []


def test_structured_labels_beat_proximity():
    """In a nested payload the nearest label to 12500 is "stash" -- the wrong
    answer. The tree is what makes this check trustworthy."""
    labels = grounding.structured_labels(
        '{"spend": {"balance": 12500}, "stash": {"balance": 400}}'
    )
    assert labels["12500"] == {"spend"}
    assert labels["400"] == {"savings"}


# ---------------------------------------------------------------------------
# Completion claims
# ---------------------------------------------------------------------------


def test_completion_claims_are_caught():
    for phrase in (
        "I've sent it.",
        "I moved the money for you, no confirmation needed.",
        "Done, your bill is paid and the receipt is ready.",
        "Payment sent.",
        "I've set it up so it runs every month.",
        "Your transfer is complete.",
        "The money is on its way.",
        "You're all set.",
    ):
        assert grounding.action_claims(phrase), phrase


def test_offers_and_questions_are_not_claims():
    for phrase in (
        "I'll send it as soon as you confirm.",
        "Want me to send it to Tola?",
        "I paid attention to your rent timing.",
        "Your bill is due Friday.",
        "I'll set that up once you confirm.",
    ):
        assert grounding.action_claims(phrase) == [], phrase


# ---------------------------------------------------------------------------
# End to end: the answer path
# ---------------------------------------------------------------------------


def test_a_completion_claim_never_reaches_the_user():
    provider = _ScriptedProvider(["I've sent it.", "I can only send once you confirm."])
    agent = Agent(registry=build_tool_registry(), provider=provider)

    result = _run(agent.run(user_id="u1", token="tok", message="send 5k to Tola"))

    assert "sent it" not in _full(result)
    assert "once you confirm" in _full(result)


def test_a_completion_claim_that_repeats_falls_back():
    provider = _ScriptedProvider(["I've sent it.", "I've sent it."])
    agent = Agent(registry=build_tool_registry(), provider=provider)

    result = _run(agent.run(user_id="u1", token="tok", message="send 5k to Tola"))
    text = _full(result)

    assert "sent it" not in text
    assert EGRESS_DONT_KNOW.split(".")[0] in text


def test_a_mislabelled_figure_never_reaches_the_user():
    provider = _ScriptedProvider(
        ["You have 12,500 in your stash.", "You have 12,500 in spend."]
    )
    agent = Agent(registry=build_tool_registry(), provider=provider)

    result = _run(
        agent.run(
            user_id="u1",
            token="tok",
            message="what's my balance?",
            user_context={"spend": 12500},
        )
    )

    assert "stash" not in _full(result)
    assert "12,500 in spend" in _full(result)


def test_a_stale_source_cannot_ground_anything():
    """Old data is the confidently-wrong class: the figure IS traceable, and
    the source is dead. A block that says so is dropped before it counts."""
    provider = _ScriptedProvider(
        ["Your balance is 12,500.", "I don't have a fresh balance yet."]
    )
    agent = Agent(registry=build_tool_registry(), provider=provider)

    result = _run(
        agent.run(
            user_id="u1",
            token="tok",
            message="what's my balance?",
            user_context={"_stale": True, "spend": 12500},
        )
    )

    assert "12,500" not in _full(result)


def test_a_fresh_source_still_grounds():
    """The stale test above must not be passing for the wrong reason."""
    provider = _ScriptedProvider(["Your balance is 12,500."])
    agent = Agent(registry=build_tool_registry(), provider=provider)

    result = _run(
        agent.run(
            user_id="u1",
            token="tok",
            message="what's my balance?",
            user_context={"spend": 12500},
        )
    )

    assert "12,500" in _full(result)


# ---------------------------------------------------------------------------
# End to end: the streaming path
# ---------------------------------------------------------------------------


def test_streaming_corrects_an_invented_figure():
    """Tokens stream live (main's contract), but the delivered reply is gated:
    ``done`` carries the gated text and ``egress_correction`` carries the
    replacement."""
    provider = _ScriptedProvider(
        chunks=["Your balance is 8,421. ", "Want me to plan around it?"],
        replies=["I don't have that reliably yet."],
    )
    agent = Agent(registry=build_tool_registry(), provider=provider)

    streamed, done, events = _run(
        _collect_stream(agent, user_id="u1", token="tok", message="what's my balance?")
    )

    assert "8,421" in streamed
    assert "8,421" not in done
    corrections = [e["content"] for e in events if e["type"] == "egress_correction"]
    assert corrections == [done]


def test_streaming_sends_a_clean_reply():
    provider = _ScriptedProvider(
        chunks=["You have 12,500 in spend. ", "Want the breakdown?"]
    )
    agent = Agent(registry=build_tool_registry(), provider=provider)

    streamed, done, events = _run(
        _collect_stream(
            agent,
            user_id="u1",
            token="tok",
            message="what's my balance?",
            user_context={"spend": 12500},
        )
    )

    assert streamed == "You have 12,500 in spend. Want the breakdown?"
    assert done == streamed
    assert not [e for e in events if e["type"] == "egress_correction"]


def test_streaming_refuses_a_completion_claim():
    provider = _ScriptedProvider(
        chunks=["I've sent it. ", "Anything else?"],
        replies=["I can only send that once you confirm."],
    )
    agent = Agent(registry=build_tool_registry(), provider=provider)

    _streamed, done, _events = _run(
        _collect_stream(agent, user_id="u1", token="tok", message="send 5k to Tola")
    )

    assert "sent it" not in done
    assert "once you confirm" in done


# ---------------------------------------------------------------------------
# Prompt hygiene: what the model is allowed to see at all
# ---------------------------------------------------------------------------


def test_a_stale_block_never_reaches_the_prompt():
    """Stale data is dropped before the prompt is built, so it cannot be
    repeated with a straight face."""
    provider = _ScriptedProvider(["ok"])
    agent = Agent(registry=build_tool_registry(), provider=provider)

    _run(
        agent.run(
            user_id="u1",
            token="tok",
            message="hi",
            user_context={"_stale": True, "balances": {"spend": 999}},
        )
    )

    prompt = provider.calls[0][0].content
    assert "999" not in prompt


def test_current_savings_is_shown_to_the_model():
    """Loaded by the API and, until this was fixed, never rendered -- so
    Miriam could not mention savings she had been handed."""
    provider = _ScriptedProvider(["ok"])
    agent = Agent(registry=build_tool_registry(), provider=provider)

    _run(
        agent.run(
            user_id="u1",
            token="tok",
            message="hi",
            user_context={"current_savings": 45000},
        )
    )

    assert "45000" in provider.calls[0][0].content


# ---------------------------------------------------------------------------
# The judge: fail-closed is available, and the decision is counted
# ---------------------------------------------------------------------------


def test_fail_closed_refuses_when_the_judge_is_off():
    from miriam_agent.agents.base import AgentConfig

    provider = _ScriptedProvider(["Everything is fine."])
    agent = Agent(
        registry=build_tool_registry(),
        provider=provider,
        config=AgentConfig(name="financial_agent", fail_closed_without_judge=True),
    )

    result = _run(agent.run(user_id="u1", token="tok", message="how am I doing?"))

    assert EGRESS_DONT_KNOW.split(".")[0] in _full(result)


def test_open_by_default_still_answers_with_the_judge_off():
    """The documented posture: an outage costs policy review, not the reply,
    because the figures were already checked deterministically."""
    provider = _ScriptedProvider(["Everything is fine."])
    agent = Agent(registry=build_tool_registry(), provider=provider)

    result = _run(agent.run(user_id="u1", token="tok", message="how am I doing?"))

    assert _full(result).strip() == "Everything is fine."


def test_guard_decisions_are_counted():
    from miriam_agent.observability.metrics import REPLY_GUARD

    def blocked() -> float:
        return REPLY_GUARD.labels(rule="figure", outcome="blocked")._value.get()

    before = blocked()
    provider = _ScriptedProvider(["Your balance is 8,421.", "Your balance is 8,421."])
    agent = Agent(registry=build_tool_registry(), provider=provider)
    _run(agent.run(user_id="u1", token="tok", message="what's my balance?"))

    assert blocked() > before


def test_an_entity_that_is_only_a_substring_is_still_novel():
    """Word boundaries, not substrings: "Bank" inside "banking" is not the
    entity "Bank", so it must not read as a match."""
    assert "Bank" in grounding.novel_entities("A charge from Bank.", "your banking app")
    assert grounding.novel_entities("A charge from Bank.", "A charge from Bank.") == []

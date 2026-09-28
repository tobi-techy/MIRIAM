"""The always-on figure guard: what it catches, and what it must never catch.

Miriam's figure rules used to live only in the onboarding linter, behind the
same code path that computed them and then sent the reply anyway. The guard is
now a pure function shared by both paths and it runs on every egress, before
and independently of the LLM judge -- so the one thing that must never depend
on a network call is the figures.

The tests come in two halves: the rules themselves, and the answer path
exercising them end to end with the judge switched off (``conftest`` sets
``TYPESAFE_ENABLED=false`` for the whole suite, which is exactly the condition
that used to leave a hallucinated number with nothing standing in its way).
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

_NAIRA = "\u20a6"
USER_ASK = "Yeah I would love you to talk me through funding my account"
# The exact reply from the incident report.
HALLUCINATED = (
    f"{_NAIRA}30000.00 left this month.\n"
    f"That's about {_NAIRA}10000.00/day if we keep it tidy."
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _ScriptedProvider:
    """Canned replies in order; records the messages of every call."""

    model = "mock-v1"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[list] = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append(messages)
        if not self._replies:
            return LLMResponse(content="All done.", model=self.model)
        return LLMResponse(content=self._replies.pop(0), model=self.model)

    async def stream(self, messages, tools=None, temperature=None, max_tokens=None):
        yield {"type": "done"}

    def cost_estimate(self, usage):
        return 0.0


def _full(result) -> str:
    parts = [result.response or ""]
    parts.extend(result.messages or [])
    return "\n".join(part for part in parts if part)


# -----------------------------------------------------------------------
# The rules
# -----------------------------------------------------------------------


def test_digits_are_always_figures():
    claims = grounding.figure_claims("You'd have 5,000 left after the 800 rent.")
    assert [c.text for c in claims] == ["5,000", "800"]


def test_spelled_out_money_is_a_figure():
    claims = grounding.figure_claims("That's about ten thousand naira a day.")
    assert [(c.text, c.value) for c in claims] == [("ten thousand", "10000")]


def test_spelled_prose_is_not_a_figure():
    """Miriam's own voice must never trip the guard, or it becomes noise."""
    for prose in (
        "Two moves first - lock the next month away.",
        "One idea, then one question.",
        "I'll take another look in three days.",
    ):
        assert grounding.figure_claims(prose) == [], prose


def test_spelled_figures_are_parsed():
    assert grounding.ungrounded_figures("thirty thousand naira", "")
    assert grounding.grounded_values("thirty thousand naira") == {"30000"}
    assert grounding.grounded_values("two hundred fifty naira") == {"250"}
    assert grounding.grounded_values("one hundred thousand naira") == {"100000"}


def test_a_spelled_source_grounds_a_digit_claim():
    """The old linter's blind spot in reverse: the user says a number in words
    and Miriam writes it in digits. That is not an invention."""
    assert (
        grounding.ungrounded_figures(
            "So 4,000 comes in each month.",
            "I take home four thousand a month",
        )
        == []
    )


def test_an_empty_corpus_grounds_nothing():
    """Strict on purpose: with nothing to trace to, every figure is a claim
    nobody made. Callers that want the old lenient behaviour opt out."""
    assert grounding.ungrounded_figures("That's 12,000 a month.", "") != []


def test_percentages_are_ratios():
    assert grounding.ungrounded_figures("I'm 60% sure", "confidence 0.6") == []
    assert grounding.ungrounded_figures("I'm 70% sure", "confidence 0.6") != []


def test_list_ordinals_are_structure_not_figures():
    assert (
        grounding.ungrounded_figures(
            "1. lock the month away\n2. split the lumps", "lock it away first"
        )
        == []
    )


def test_the_incident_reply_is_caught():
    offenders = grounding.ungrounded_figures(HALLUCINATED, USER_ASK)
    # Values are normalised, so the trailing ".00" drops: 30000.00 == 30000.
    assert sorted(f.value for f in offenders) == ["10000", "30000"]


# -----------------------------------------------------------------------
# The answer path, with the judge switched off
# -----------------------------------------------------------------------


def test_answer_path_replaces_an_invented_figure(monkeypatch):
    clean = "I can only talk about money you have given me, and I have none yet."
    provider = _ScriptedProvider([HALLUCINATED, clean])
    agent = Agent(registry=build_tool_registry(), provider=provider)

    result = _run(agent.run(user_id="u1", token="tok", message=USER_ASK))
    text = _full(result)

    assert "30000" not in text and "10000" not in text
    assert clean in text
    # One draft, one rewrite.
    assert len(provider.calls) == 2


def test_answer_path_refuses_when_the_rewrite_also_invents(monkeypatch):
    provider = _ScriptedProvider([HALLUCINATED, HALLUCINATED])
    agent = Agent(registry=build_tool_registry(), provider=provider)

    result = _run(agent.run(user_id="u1", token="tok", message=USER_ASK))
    text = _full(result)

    assert "30000" not in text and "10000" not in text
    assert EGRESS_DONT_KNOW.split(".")[0] in text


def test_answer_path_ships_a_grounded_figure_first_try():
    reply = "So 30,000 comes in each month. What goes out first?"
    provider = _ScriptedProvider([reply])
    agent = Agent(registry=build_tool_registry(), provider=provider)

    result = _run(
        agent.run(user_id="u1", token="tok", message="my take home is 30000 a month")
    )

    assert "30,000" in _full(result)
    assert len(provider.calls) == 1


def test_context_blocks_ground_a_figure():
    """A number from injected context is not an invention."""
    provider = _ScriptedProvider(["Your balance is 12,500 right now."])
    agent = Agent(registry=build_tool_registry(), provider=provider)

    result = _run(
        agent.run(
            user_id="u1",
            token="tok",
            message="what's my balance?",
            user_context={"balances": {"spending": 12500}},
        )
    )

    assert "12,500" in _full(result)
    assert len(provider.calls) == 1


def test_miriams_own_earlier_reply_is_not_a_source():
    """A number she invented last turn must not ground the number she states
    this turn, or one hallucination would launder its own successor."""
    provider = _ScriptedProvider(["Your balance is 8,000.", "Your balance is 8,000."])
    agent = Agent(registry=build_tool_registry(), provider=provider)

    result = _run(
        agent.run(
            user_id="u1",
            token="tok",
            message="what's my balance?",
            history=[
                {"role": "assistant", "content": "Your balance is 8,000."},
                {"role": "user", "content": "ok thanks"},
            ],
        )
    )

    assert "8,000" not in _full(result)

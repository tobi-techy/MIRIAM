"""Miriam is wired to the plan object, and the prompt says so.

The maths is already guarded by the schema and by the two narration gates. These
tests cover the third layer: the instructions that stop the model from trying in
the first place, and the operator-first framing the brief requires.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.agents.system_prompt import build_system_prompt
from miriam_agent.money.text import has_em_dash


def prompt() -> str:
    return build_system_prompt(
        user_context={"name": "Ada", "currency": "NGN"},
        memory_facts=[{"type": "goal", "content": "school fees"}],
    )


def flat() -> str:
    """The prompt with line wrapping collapsed.

    The rules are hard-wrapped in the source, so a phrase can span a newline.
    Normalizing whitespace keeps these assertions about wording rather than about
    where the editor happened to break a line.
    """
    return " ".join(prompt().split())


def test_the_prompt_is_operator_first():
    text = prompt()
    assert "operator first and their friend second" in text
    assert "friend first" not in text


def test_the_prompt_requires_the_plan_before_any_money_claim():
    text = prompt()
    assert "get_money_plan" in text
    assert "BEFORE ANY MONEY CLAIM" in text


def test_the_prompt_pins_what_the_model_may_and_may_not_rewrite():
    text = flat()
    assert "SPEAK FROM THE OBJECT THIS TURN" in text
    for pinned in (
        "surplus",
        "book weights",
        "glider.kind",
        "buffer target",
        "debt actions",
    ):
        assert pinned in text, pinned


def test_the_prompt_treats_the_safety_order_as_non_negotiable():
    text = flat()
    assert "THE SAFETY ORDER IS NOT NEGOTIABLE" in text
    assert "Do not soften a refusal into a maybe" in text


def test_the_prompt_forbids_accidental_onchain_actions():
    text = prompt()
    assert "NEVER GO ONCHAIN BY ACCIDENT" in text
    assert "you never enroll anyone" in text
    assert "cash-like, never Glider" in text


def test_the_prompt_requires_assumptions_to_be_labelled():
    text = flat()
    assert "LABEL WHAT IS ASSUMED" in text
    assert "placeholder" in text


def test_the_prompt_defines_the_two_modes():
    text = prompt()
    assert "Chat (default)" in text
    assert "Plan mode" in text
    assert "15-60 words" in text


def test_the_prompt_defines_do_it():
    text = flat()
    assert '"DO IT" EXECUTES THE LAST PROPOSAL' in text
    assert "never treat a confirmation as consent for anything beyond" in text


def test_the_prompt_bans_em_dashes_and_hype():
    text = prompt()
    assert "No em dashes" in text
    assert "no hype, no hustle language" in text
    assert not has_em_dash(text), "the prompt itself must obey the house rule"


def test_the_prompt_still_carries_the_context_blocks():
    """The money rules are additive, not a replacement for the existing prompt."""
    text = prompt()
    assert "CURRENT SITUATION" in text
    assert "WHAT YOU KNOW ABOUT THIS USER" in text
    assert "EXECUTION MODEL" in text

"""Tests for the shared text helpers (``miriam_agent.utils.text``).

Mirrors RAIL_BACKEND's ``humanize_test.go`` coverage so the Python scrub and
the Go scrub stay in lockstep, plus the chatty-turn affordances (tapback
reactions and bubble splitting) that the executor relays as native gestures.
"""

from __future__ import annotations

from miriam_agent.utils.text import (
    MAX_EXTRA_MESSAGES,
    REACTION_WHITELIST,
    bubble_sets,
    chatty_bubbles,
    clean_text,
    lift_reaction,
    valid_reaction,
)

# ----------------------------------------------------------------------
# clean_text: the typographic scrub (mirrors Go humanizeText)
# ----------------------------------------------------------------------


def test_clean_text_replaces_em_and_en_dashes():
    assert clean_text("rough month\u2014just about\xa0barely") == (
        "rough month, just about barely"
    )
    assert clean_text("paycheck\u2013then bills") == "paycheck, then bills"


def test_clean_text_straightens_curly_quotes_and_ellipsis():
    assert clean_text("She said \u2018hi\u2019 and I went \u201cwow\u201d\u2026") == (
        "She said 'hi' and I went \"wow\"..."
    )


def test_clean_text_is_idempotent_for_plain_prose():
    plain = "Yeah, that is the real issue. Want me to set it up?"
    assert clean_text(plain) == plain
    assert clean_text(clean_text(plain)) == clean_text(plain)


def test_clean_text_removes_leftover_dash_commas():
    # A dash adjacent to punctuation must not leave an awkward ", .".
    assert "plus" in clean_text("here,\u2014.plus more")


def test_clean_text_empty_pass_through():
    assert clean_text("") == ""
    assert clean_text(None) is None  # type: ignore[arg-type]


def test_agent_decorate_scrubs_em_dashes_from_response():
    from miriam_agent.agents.agent_loop import Agent, AgentRunResult
    from miriam_agent.agents.tools import get_registry

    agent = Agent(registry=get_registry())
    result = agent._decorate(
        AgentRunResult(response="That went well—really well", conversation_id="c")
    )
    assert "—" not in result.response
    assert all("—" not in m for m in result.messages)


def test_agent_decorate_scrubs_confirmation_response():
    from miriam_agent.agents.agent_loop import Agent, AgentRunResult
    from miriam_agent.agents.tools import get_registry

    agent = Agent(registry=get_registry())
    result = agent._decorate(
        AgentRunResult(
            response="Confirm — send 5k",
            conversation_id="c",
            requires_confirmation=True,
        )
    )
    assert "—" not in result.response


def test_system_prompt_has_no_em_dash():
    from miriam_agent.agents import system_prompt as sp

    assert "—" not in sp.BASE_PROMPT
    assert "NO EM DASHES" in sp.BASE_PROMPT


# ----------------------------------------------------------------------
# Reactions: the six universal tapbacks only
# ----------------------------------------------------------------------


def test_reaction_whitelist_is_the_six_universal_tapbacks():
    assert REACTION_WHITELIST == frozenset({"❤️", "👍", "👎", "😂", "‼️", "❓"})


def test_valid_reaction_strips_whitespace():
    assert valid_reaction("👍")
    assert valid_reaction(" 👍 ")
    assert not valid_reaction("🎉")
    assert not valid_reaction("")
    assert not valid_reaction("ok")


def test_lift_reaction_returns_first_whitelisted_emoji():
    assert lift_reaction("Nice! \u2764\ufe0f") == "❤️"
    assert lift_reaction("Oh no 😂 and then") == "😂"
    assert lift_reaction("No taper here") == ""
    assert lift_reaction("") == ""


def test_lift_reaction_ignores_non_whitelisted_emoji():
    assert lift_reaction("Party time 🎉") == ""


# ----------------------------------------------------------------------
# Chatty bubbles: split only genuinely long, multi-sentence replies
# ----------------------------------------------------------------------


def test_short_reply_stays_one_bubble():
    assert chatty_bubbles("Send it over.") == []
    main, extras = bubble_sets("Send it over.")
    assert main == "Send it over."
    assert extras == []


def test_long_single_sentence_does_not_split():
    w1 = (
        "Every single dollar is accounted for across every single bucket in the "
        "plan we built together this morning, and there is nowhere left to hide "
        "a surprise."
    )
    assert len(w1) >= 140
    assert w1.count(".") == 1
    assert chatty_bubbles(w1) == []


def test_chatty_reply_splits_into_short_bubbles():
    s1 = (
        "First we protect the next month, because that is the hardest stretch "
        "and the floor keeps everything else above water."
    )
    s2 = "Then we smooth your income so a late check stops wrecking the week."
    s3 = "After that we make the goal a monthly habit you barely notice."
    reply = " ".join([s1, s2, s3])
    assert len(reply) >= 140
    bubbles = chatty_bubbles(reply)
    assert len(bubbles) >= 2
    assert len(bubbles) <= MAX_EXTRA_MESSAGES + 1
    for b in bubbles:
        assert 0 < len(b) <= 120
        assert b.strip() == b
    main, extras = bubble_sets(reply)
    assert main == bubbles[0]
    assert extras == bubbles[1:MAX_EXTRA_MESSAGES]
    assert len(extras) <= MAX_EXTRA_MESSAGES


def test_chatty_reply_never_exceeds_bubble_budget():
    point = (
        "Point {i}: this is another short standalone sentence that fits "
        "its own bubble just fine."
    )
    reply = " ".join(point.format(i=i) for i in range(6))
    assert len(reply) >= 140
    bubbles = chatty_bubbles(reply)
    assert len(bubbles) <= MAX_EXTRA_MESSAGES + 1

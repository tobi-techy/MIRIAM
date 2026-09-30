"""Memory carries facts, never balances.

Supermemory is the long-term memory layer, so a figure written into it becomes
a durable "fact" a later turn may quote as *current*. Money turns narrate
computed figures ("you're at ₦720", "sent ₦50,000"), so the ingest boundary
redacts Miriam's own figures before they land in the graph, while leaving the
user's own words and the surrounding facts intact.

These tests pin both halves: the deterministic redactor, and the single ingest
choke point every surface (web, money, onboarding, spectrum) goes through.
"""

from __future__ import annotations

from miriam_agent.api import chat
from miriam_agent.conversational.redact import redact_money_figures


# ---------------------------------------------------------------------------
# The redactor
# ---------------------------------------------------------------------------


def test_currency_marked_amounts_are_redacted():
    assert redact_money_figures("You're at ₦720 of your ₦1,000 target.") == (
        "You're at [amount] of your [amount] target."
    )
    assert redact_money_figures("Sent ₦50,000 to @bisi. Fee ₦100.5.") == (
        "Sent [amount] to @bisi. Fee [amount]."
    )


def test_iso_code_and_trailing_forms_are_redacted():
    assert redact_money_figures("Your buffer is NGN 50,000 at 15%.") == (
        "Your buffer is [amount] at [pct]."
    )
    assert redact_money_figures("That is 50,000 NGN, about 12.5% of income.") == (
        "That is [amount], about [pct] of income."
    )


def test_the_money_layer_symbol_set_is_covered():
    # Every mark money/formatting.symbol() can emit, including the multi-char
    # GH₵/KSh and the trailing symbol form.
    assert redact_money_figures("KSh 50,000 spent, 500₦ left.") == (
        "[amount] spent, [amount] left."
    )
    assert redact_money_figures("GH₵500 total") == "[amount] total"
    assert redact_money_figures("R500 in the account") == "[amount] in the account"
    assert redact_money_figures("$1,234.56 and €900 and £40") == (
        "[amount] and [amount] and [amount]"
    )


def test_compact_magnitude_suffixes_are_consumed_whole():
    assert redact_money_figures("I earn ₦250k monthly") == "I earn [amount] monthly"
    assert redact_money_figures("₦1.5m inflow") == "[amount] inflow"


def test_bare_numbers_survive_because_they_may_be_facts():
    # "3 months" and "5 team members" are facts, not balances; the money layer
    # always marks a true balance, so bare numbers are left untouched.
    assert redact_money_figures("3 months covered, 5 team members") == (
        "3 months covered, 5 team members"
    )
    assert redact_money_figures("2 moves first, then 30 days later") == (
        "2 moves first, then 30 days later"
    )
    # "1.5m yearly" without a currency mark is not treated as an amount.
    assert redact_money_figures("and 1.5m yearly") == "and 1.5m yearly"


def test_ordinary_prose_is_not_touched():
    text = "No numbers here, just facts about the plan."
    assert redact_money_figures(text) == text


def test_redaction_is_idempotent_and_handles_empty():
    once = redact_money_figures("Sent ₦50,000 to @bisi at 15%.")
    assert redact_money_figures(once) == once
    assert redact_money_figures("") == ""


# ---------------------------------------------------------------------------
# The memory layer that owns the invariant
# ---------------------------------------------------------------------------


class _RecordingClient:
    """SupermemoryClient double that records the turn it was asked to keep."""

    enabled = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def ingest_conversation(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return {"ok": True}


async def test_ingest_turn_redacts_assistant_figures_but_keeps_user_words():
    from miriam_agent.conversational.supermemory_memory import SupermemoryMemory

    client = _RecordingClient()
    memory = SupermemoryMemory(client)

    await memory.ingest_turn(
        container_tag="user_x",
        conversation_id="miriam:web:user_x",
        user_message="send 5000 to john",
        assistant_message="Sent ₦50,000 to @john. You're at ₦720 now.",
    )

    assert len(client.calls) == 1
    messages = client.calls[0]["messages"]
    # The user's own words are memory material and stay verbatim.
    assert messages[0] == {"role": "user", "content": "send 5000 to john"}
    # Miriam's computed figures never become durable memory.
    assert messages[1] == {
        "role": "assistant",
        "content": "Sent [amount] to @john. You're at [amount] now.",
    }
    # The stable scope (diff-billing identity) is unchanged by redaction.
    assert client.calls[0]["conversation_id"] == "miriam:web:user_x"


async def test_ingest_turn_is_a_noop_when_memory_is_disabled():
    from miriam_agent.conversational.supermemory_memory import SupermemoryMemory

    client = _RecordingClient()
    client.enabled = False
    memory = SupermemoryMemory(client)

    result = await memory.ingest_turn(
        container_tag="user_x",
        conversation_id="c",
        user_message="hi",
        assistant_message="Sent ₦50,000.",
    )

    assert result is None
    assert client.calls == []

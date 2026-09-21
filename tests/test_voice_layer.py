"""Layer 3 tests. Voice narrates STATE and cannot do anything else.

The design's Voice guarantees, tested directly:

* a ``stay_quiet`` turn is not generated at all,
* a figure that is not in STATE is refused, so a sentence cannot carry a send
  amount the pipeline never produced,
* a ``CONFIRM:`` line is only honoured when it carries ``WAIT``, ``NO``, or the
  confirm id that is actually in STATE,
* swapping the model changes the wording and nothing else,
* a dead provider still yields the numbers, from STATE.

The tool-surface test is the structural one: the provider is called with no
tools, so there is no path from a narration to an action.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from layer_fakes import POLICY, FakeProvider, jev, judge_of, ledger_with

from miriam_agent.hands.ledger import money
from miriam_agent.hands.state import Execution, ProposedAction, build_state
from miriam_agent.judgment.decide import decide
from miriam_agent.voice.generate import (
    clamp_violations,
    deterministic_message,
    extract_confirm,
    speak,
    state_vocabulary,
)


def _state(*, spendable=184000, rent=120000, request="95000", decision=None):
    ledger = ledger_with(spendable=spendable, rent_required=rent, due_in_days=9)
    action = None
    if request:
        action = ProposedAction(
            type="transfer", amount=money(request), counterparty="Ada"
        )
    return build_state(
        ledger=ledger, policy=POLICY, proposed_action=action, decision=decision
    )


def _quiet_state(**extra):
    """A state Judgment left quiet, with nothing proposed and nothing executed."""
    state = _state(
        request=None,
        decision={
            "id": "dec_quiet",
            "next_mode": "stay_quiet",
            "action_choice": "classify_only",
            "reasons": [],
        },
    )
    return state.model_copy(update=extra) if extra else state


async def _decided_state():
    """A state whose decision came from the real rules, not from a fixture."""
    ledger = ledger_with(spendable=184000, rent_required=120000, due_in_days=9)
    proposed = ProposedAction(type="transfer", amount=money(95000), counterparty="Ada")
    state = build_state(ledger=ledger, policy=POLICY, proposed_action=proposed)
    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        utterance="send 95k to Ada",
        judge=judge_of(jev(mode="ask", action="deny")),
    )
    assert decision.next_mode == "ask"
    assert decision.action_choice == "deny"
    return state.model_copy(update={"decision": decision.model_dump(mode="json")})


# ---------------------------------------------------------------------------
# When Voice is not called
# ---------------------------------------------------------------------------


async def test_a_stay_quiet_turn_is_not_generated():
    provider = FakeProvider("this must never be sent")
    message = await speak(state=_quiet_state(), provider=provider)
    assert message is None
    assert provider.calls == []


async def test_a_turn_with_no_decision_is_never_narrated():
    provider = FakeProvider("nope")
    assert await speak(state=_state(decision=None), provider=provider) is None
    assert provider.calls == []


async def test_a_stay_quiet_turn_that_executed_is_still_narrated():
    """stay_quiet forbids chatter, not a receipt for something that moved."""
    state = _quiet_state(
        execution=Execution(
            receipt_id="r1",
            status="executed",
            action="inflow_split",
            amount=money(420000),
            sleeve="spendable",
            at=datetime(2026, 9, 20, tzinfo=UTC),
        )
    )
    message = await speak(state=state, provider=FakeProvider("Split done."))
    assert message is not None
    assert message.text == "Split done."


# ---------------------------------------------------------------------------
# The clamp
# ---------------------------------------------------------------------------


def test_state_is_the_vocabulary():
    allowed = state_vocabulary(_state())
    assert "184000" in allowed
    assert "120000" in allowed
    assert "95000" in allowed
    assert "999999" not in allowed


def test_an_invented_send_amount_is_a_violation():
    violations = clamp_violations(_state(), "Send 300000 to Ada instead.")
    assert "300000" in violations


def test_a_figure_state_holds_is_not_a_violation():
    assert clamp_violations(_state(), "You have 184000 and rent is 120000.") == []


async def test_an_invented_send_amount_is_clamped_to_the_deterministic_line():
    state = await _decided_state()
    provider = FakeProvider("Send 300000 to Ada, that is affordable.")

    message = await speak(state=state, provider=provider)

    assert message is not None
    assert message.status == "clamped"
    assert "300000" not in message.text.replace(",", "")
    assert "184,000" in message.text


async def test_an_over_long_reply_falls_back():
    state = await _decided_state()
    message = await speak(state=state, provider=FakeProvider("word " * 200))
    assert message is not None
    assert message.status == "clamped"
    assert "over_80_words" in message.violations


async def test_a_requested_breakdown_may_exceed_the_ceiling():
    state = await _decided_state()
    message = await speak(
        state=state, provider=FakeProvider("word " * 200), breakdown=True
    )
    assert message is not None
    assert message.status == "generated"


# ---------------------------------------------------------------------------
# CONFIRM parsing
# ---------------------------------------------------------------------------


def test_confirm_only_accepts_wait_no_or_a_real_id():
    state = _state(decision={"id": "d1", "next_mode": "ask", "confirm_id": "cf_123"})

    text, confirm = extract_confirm("Balance is 184000.\nCONFIRM: cf_123", state)
    assert confirm == "cf_123"
    assert "CONFIRM" not in text

    _, confirm = extract_confirm("Wait a bit.\nCONFIRM: WAIT", state)
    assert confirm == "WAIT"

    _, confirm = extract_confirm("No.\nCONFIRM: NO", state)
    assert confirm == "NO"


def test_an_unstructured_yes_is_not_a_confirmation():
    state = _state(decision={"id": "d1", "next_mode": "ask", "confirm_id": "cf_123"})
    text, confirm = extract_confirm("Done.\nCONFIRM: yes", state)
    assert confirm is None
    assert "CONFIRM" not in text


def test_a_confirm_id_hands_never_issued_is_refused():
    state = _state(decision={"id": "d1", "next_mode": "ask", "confirm_id": "cf_123"})
    _, confirm = extract_confirm("Sure.\nCONFIRM: cf_999", state)
    assert confirm is None


# ---------------------------------------------------------------------------
# Failures and model swaps
# ---------------------------------------------------------------------------


async def test_a_dead_provider_still_yields_the_numbers_from_state():
    state = await _decided_state()
    message = await speak(state=state, provider=FakeProvider(fail=True))
    assert message is not None
    assert message.status == "unavailable"
    assert "184,000" in message.text
    assert "RENT_SHORT" in message.text


async def test_voice_is_called_with_no_tools_at_all():
    state = await _decided_state()
    provider = FakeProvider("Spendable is 184,000. Rent is 120,000 in 9 days.")
    await speak(state=state, provider=provider)
    assert provider.offered_tools() == [None]


@pytest.mark.parametrize(
    "content",
    [
        "Spendable is 184,000. Rent lands in 9 days, so 95,000 leaves you short.",
        "184,000 liquid, 120,000 of rent due. That ask does not fit.",
    ],
)
async def test_swapping_the_model_changes_only_the_wording(content):
    state = await _decided_state()
    message = await speak(state=state, provider=FakeProvider(content))

    assert message is not None
    assert message.text == content
    # The verdict comes from STATE either way, and is not the model's to change.
    assert state.decision is not None
    assert state.decision["action_choice"] == "deny"
    assert "RENT_SHORT" in state.decision["reasons"]


def test_the_deterministic_line_is_built_from_state_alone():
    state = _state(
        decision={
            "id": "d1",
            "next_mode": "ask",
            "action_choice": "deny",
            "reasons": ["RENT_SHORT"],
            "confirm_id": "cf_123",
        }
    )
    text = deterministic_message(state)
    assert "184,000" in text
    assert "120,000" in text
    assert "RENT_SHORT" in text
    assert "CONFIRM: cf_123" in text

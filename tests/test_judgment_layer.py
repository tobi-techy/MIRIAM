"""Layer 2 tests. Judgment returns typed fields and nothing else.

The four tests the design names for Judgment are here by name:

* "send 200k to Femi" over the limit is an ask, never an act,
* "can I buy this phone" is advice with an affordability score and no transfer,
* an inflow with no utterance is classified and the split path stays quiet,
* a flaky JEV fails closed, which is an ask or a no-op and never an act.

The rules are code, so they are testable without a model. The judge is injected,
which is also how the flaky and unavailable paths are exercised.
"""

from __future__ import annotations

from decimal import Decimal

from layer_fakes import POLICY, broken_judge, jev, judge_of, ledger_with

from miriam_agent.hands.ledger import InMemoryLedgerStore, money, new_ledger
from miriam_agent.hands.state import PendingInflow, ProposedAction, build_state
from miriam_agent.hands.transfer import InMemoryRail, parse_transfer_utterance
from miriam_agent.judgment.decide import cap_for, decide
from miriam_agent.judgment.rules import Reason
from miriam_agent.orchestrator import Event, Orchestrator


def _state_for(ledger, text: str):
    action = parse_transfer_utterance(text)
    return build_state(ledger=ledger, policy=POLICY, proposed_action=action), action


# ---------------------------------------------------------------------------
# The four named guarantees
# ---------------------------------------------------------------------------


async def test_an_over_limit_order_is_an_ask_never_an_act():
    ledger = ledger_with(
        spendable=90000, locked=150000, rent_required=150000, rent_reserved=150000
    )
    state, action = _state_for(ledger, "Send 200k to Femi")

    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        utterance="Send 200k to Femi",
        judge=judge_of(jev(intent="order")),
    )

    assert decision.next_mode == "ask"
    assert decision.action_choice == "deny"
    assert decision.intent_type == "order"
    assert "OVER_BALANCE" in decision.reasons
    assert action is not None


async def test_the_95k_phone_is_advice_with_an_affordability_score():
    ledger = ledger_with(spendable=184000, rent_required=120000, due_in_days=9)
    state, _action = _state_for(ledger, "can I buy this phone for 95k")

    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        utterance="can I buy this phone for 95k",
        judge=judge_of(jev(intent="advice", afford=0.21, violation=0.9)),
    )

    assert decision.intent_type == "advice"
    assert decision.affordability == 0.21
    assert decision.action_choice == "none"
    assert "ADVICE_ONLY" in decision.reasons


async def test_an_inflow_with_no_utterance_classifies_and_stays_quiet():
    ledger = ledger_with(spendable=0, rent_required=150000)
    ledger.pending_inflow = PendingInflow(
        id="pay_9", amount=money(420000), source_raw="ACME PAYROLL"
    )
    state = build_state(ledger=ledger, policy=POLICY)

    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        judge=judge_of(
            jev(
                inflow="salary",
                intent="status",
                mode="stay_quiet",
                action="classify_only",
            )
        ),
    )

    assert decision.inflow_class == "salary"
    assert decision.next_mode == "stay_quiet"
    assert decision.action_choice == "classify_only"


async def test_a_flaky_jev_fails_closed_to_an_ask():
    ledger = ledger_with(spendable=500000)
    state, _action = _state_for(ledger, "send 5k to Ada")

    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        utterance="send 5k to Ada",
        judge=broken_judge(),
    )

    assert decision.degraded is True
    assert decision.next_mode == "ask"
    assert decision.action_choice == "defer"
    assert Reason.JEV_UNAVAILABLE.value in decision.reasons


async def test_a_flaky_jev_with_nothing_proposed_is_a_no_op():
    ledger = ledger_with(spendable=500000)
    state = build_state(ledger=ledger, policy=POLICY)

    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        utterance="hmm",
        judge=broken_judge(),
    )

    assert decision.degraded is True
    assert decision.next_mode == "stay_quiet"
    assert decision.action_choice == "classify_only"


# ---------------------------------------------------------------------------
# Hard overrides
# ---------------------------------------------------------------------------


async def test_a_locked_sleeve_is_denied():
    ledger = ledger_with(spendable=50000)
    action = ProposedAction(
        type="transfer", amount=money(1000), counterparty="Ada", sleeve="locked"
    )
    state = build_state(ledger=ledger, policy=POLICY, proposed_action=action)

    decision = await decide(
        state=state, ledger=ledger, policy=POLICY, judge=judge_of(jev())
    )
    assert decision.action_choice == "deny"
    assert Reason.LOCKED_SLEEVE.value in decision.reasons


async def test_rent_short_is_a_deny_with_the_rent_reason():
    """The spec's worked example: 184k spendable, 120k rent, 95k request."""
    ledger = ledger_with(spendable=184000, rent_required=120000, due_in_days=9)
    state, _action = _state_for(ledger, "send 95k to Ada")

    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        utterance="send 95k to Ada",
        judge=judge_of(jev(intent="order", afford=0.21, violation=0.9)),
    )

    assert decision.next_mode == "ask"
    assert decision.action_choice == "deny"
    assert Reason.RENT_SHORT.value in decision.reasons


async def test_above_the_auto_ceiling_offers_a_cap_computed_by_hands():
    ledger = ledger_with(spendable=500000)
    state, _action = _state_for(ledger, "send 5k to Ada")

    expected_cap = cap_for(state=state, ledger=ledger, policy=POLICY)
    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        utterance="send 5k to Ada",
        judge=judge_of(jev(intent="order")),
    )

    assert decision.next_mode == "ask"
    assert decision.action_choice == "allow_smaller"
    assert decision.suggested_amount == expected_cap == money(2000)
    assert Reason.OVER_AUTO.value in decision.reasons


async def test_under_every_ceiling_is_allowed_to_act():
    ledger = ledger_with(spendable=500000)
    state, _action = _state_for(ledger, "send 1.5k to Ada")

    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        utterance="send 1.5k to Ada",
        judge=judge_of(jev(intent="order", violation=0.0)),
    )

    assert decision.next_mode == "act"
    assert decision.action_choice == "allow"
    assert Reason.AFFORDABLE.value in decision.reasons


async def test_a_chat_yes_with_no_confirm_id_is_not_a_confirmation():
    ledger = ledger_with(spendable=500000)
    state = build_state(ledger=ledger, policy=POLICY)

    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        utterance="yes, do it",
        judge=judge_of(jev(intent="confirm", mode="act", action="none")),
    )

    assert Reason.CONFIRM_WITHOUT_ID.value in decision.reasons
    assert decision.action_choice == "none"


async def test_an_incomplete_state_is_an_ask():
    ledger = new_ledger("u1")
    ledger.sleeves.pop("locked")
    state = build_state(ledger=ledger, policy=POLICY)

    decision = await decide(
        state=state, ledger=ledger, policy=POLICY, judge=judge_of(jev())
    )
    assert decision.next_mode == "ask"
    assert Reason.INSUFFICIENT_STATE.value in decision.reasons


async def test_a_rule_never_loosens_what_jev_refused():
    ledger = ledger_with(spendable=500000)
    state, _action = _state_for(ledger, "send 1.5k to Ada")

    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        utterance="send 1.5k to Ada",
        judge=judge_of(jev(intent="order", mode="ask", action="deny")),
    )
    assert decision.action_choice == "deny"
    assert decision.next_mode == "ask"


async def test_a_concrete_instruction_is_never_dropped_in_silence():
    ledger = ledger_with(spendable=500000)
    state, _action = _state_for(ledger, "send 1.5k to Ada")

    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        utterance="send 1.5k to Ada",
        judge=judge_of(jev(intent="order", mode="stay_quiet", action="none")),
    )
    assert decision.next_mode == "ask"
    assert Reason.NEEDS_AN_ANSWER.value in decision.reasons


async def test_an_unknown_inflow_is_flagged_but_does_not_stop_the_arithmetic():
    ledger = ledger_with(spendable=0)
    ledger.pending_inflow = PendingInflow(
        id="pay_x", amount=money(5000), source_raw="CREDIT 5000"
    )
    state = build_state(ledger=ledger, policy=POLICY)

    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        judge=judge_of(jev(inflow="unknown", intent="status", mode="stay_quiet")),
    )
    assert decision.inflow_class == "unknown"
    assert Reason.UNKNOWN_INFLOW.value in decision.reasons
    assert decision.next_mode == "stay_quiet"


# ---------------------------------------------------------------------------
# Judgment is read-only
# ---------------------------------------------------------------------------


async def test_judgment_never_writes_to_the_ledger():
    ledger = ledger_with(spendable=500000)
    before = dict(ledger.sleeves)
    version = ledger.version
    state, _action = _state_for(ledger, "send 200k to Ada")

    await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        utterance="send 200k to Ada",
        judge=judge_of(jev(intent="order")),
    )

    assert ledger.sleeves == before
    assert ledger.version == version
    assert ledger.receipts == []


async def test_judgment_never_calls_a_rail():
    store = InMemoryLedgerStore()
    ledger = ledger_with(spendable=500000)
    store.seed(ledger)
    rail = InMemoryRail()
    orchestrator = Orchestrator(
        store=store,
        policy=POLICY,
        rail=rail,
        provider=None,
        judge=broken_judge(),
    )

    await orchestrator.handle(
        Event(type="utterance", user_id="u1", text="send 200k to Ada")
    )
    assert rail.calls == []


async def test_every_decision_carries_enum_reason_codes_only():
    ledger = ledger_with(spendable=184000, rent_required=120000)
    state, _action = _state_for(ledger, "send 95k to Ada")

    decision = await decide(
        state=state,
        ledger=ledger,
        policy=POLICY,
        utterance="send 95k to Ada",
        judge=judge_of(jev(intent="order", afford=0.2, violation=0.9)),
    )

    assert decision.reasons
    for reason in decision.reasons:
        assert reason == reason.upper()
        assert " " not in reason


def test_the_cap_is_never_negative_and_never_above_the_auto_ceiling():
    ledger = ledger_with(spendable=10, rent_required=100000)
    state = build_state(ledger=ledger, policy=POLICY)
    assert cap_for(state=state, ledger=ledger, policy=POLICY) == Decimal("0")


# ---------------------------------------------------------------------------
# The approval hold: no ceiling to act silently, but a tap still works
#
# APPROVAL_REQUIRED_ABOVE defaults to 0 until the inflow webhook is proven, which
# means every movement is above the ceiling. That must produce a challenge, not a
# refusal: a hold where the user has nothing to tap is money switched off, which
# is a different product from confirm-first.
# ---------------------------------------------------------------------------


def _held_policy():
    from miriam_agent.hands.limits import Policy

    return Policy(max_auto=money(0), max_with_confirm=money(5000))


async def test_a_zero_auto_ceiling_offers_a_challenge_rather_than_refusing():
    ledger = ledger_with(spendable=50000)
    state, _action = _state_for(ledger, "send 1000 to Ada")

    decision = await decide(
        state=state,
        ledger=ledger,
        policy=_held_policy(),
        utterance="send 1000 to Ada",
        judge=judge_of(jev(intent="order", violation=0.0)),
    )

    assert decision.next_mode == "ask"
    assert decision.action_choice == "allow", (
        "a zero ceiling must still offer the amount for confirmation; 'defer' "
        "creates no challenge, so the user could never send anything"
    )
    assert decision.suggested_amount is None
    assert Reason.OVER_AUTO.value in decision.reasons


async def test_the_held_amount_settles_on_a_tap():
    """End to end through the orchestrator: nothing moves, then the tap moves it."""
    from layer_fakes import FakeProvider

    from miriam_agent.hands.ledger import InMemoryLedgerStore
    from miriam_agent.hands.transfer import InMemoryRail
    from miriam_agent.orchestrator import Orchestrator

    ledger = ledger_with(spendable=50000)
    store = InMemoryLedgerStore()
    store.seed(ledger)
    rail = InMemoryRail()
    orchestrator = Orchestrator(
        store=store,
        policy=_held_policy(),
        rail=rail,
        provider=FakeProvider("Asking first."),
        judge=judge_of(jev(intent="order", violation=0.0)),
    )

    asked = await orchestrator.handle_utterance("u1", "send 1000 to Ada")
    assert asked.confirm_id, "the hold must offer something to tap"
    assert rail.calls == [], "nothing may move before the tap"

    tapped = await orchestrator.handle_confirm("u1", asked.confirm_id, yes=True)
    assert tapped.receipt is not None
    assert tapped.receipt.status == "executed"
    assert [c.amount for c in rail.calls] == [money(1000)]


def test_the_approval_ceiling_defaults_to_the_hold():
    """The shipped default is the hold, and it is what the setting means."""
    from miriam_agent.config.settings import Settings
    from miriam_agent.hands.limits import Policy

    settings = Settings(_env_file=None)
    assert settings.APPROVAL_REQUIRED_ABOVE == 0.0
    assert Policy.from_settings().max_auto == money(0)

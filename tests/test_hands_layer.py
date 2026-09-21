"""Layer 1 tests. Hands is deterministic, and these prove it.

The four tests the design names for Hands are here by name:

* the same inflow twice produces one split,
* an LLM emitting "send 200k" cannot cause a transfer,
* a limit breach moves nothing and returns a rejected receipt,
* a yield partner that is down parks the money instead of posting a yield.

Everything else covers the guarantees the rest of the system leans on: the split
reconciles to the cent, rent is frozen before the user can spend it, and an
incomplete STATE is reported rather than guessed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from layer_fakes import POLICY, FakeProvider, jev, judge_of, ledger_with

from miriam_agent.hands.audit import Receipt
from miriam_agent.hands.ledger import (
    InMemoryLedgerStore,
    Ledger,
    LedgerConflictError,
    LedgerError,
    LedgerUnavailable,
    RedisLedgerStore,
    Track,
    money,
    new_ledger,
)
from miriam_agent.hands.limits import evaluate_limits
from miriam_agent.hands.split import split_inflow, split_parts
from miriam_agent.hands.state import (
    HandlerState,
    InsufficientState,
    ProposedAction,
    build_state,
    require_complete,
)
from miriam_agent.hands.transfer import (
    InMemoryRail,
    execute_transfer,
    move_between_sleeves,
    parse_transfer_utterance,
    route_yield,
)
from miriam_agent.orchestrator import Event, Orchestrator


async def _seeded(ledger: Ledger) -> InMemoryLedgerStore:
    store = InMemoryLedgerStore()
    store.seed(ledger)
    return store


def _authorised(ledger: Ledger, amount: str, counterparty: str = "Femi"):
    """A STATE whose decision allows a transfer, for the Hands-level tests."""
    action = ProposedAction(
        type="transfer", amount=money(amount), counterparty=counterparty
    )
    state = build_state(
        ledger=ledger,
        policy=POLICY,
        proposed_action=action,
        decision={"id": "dec_test", "next_mode": "act", "action_choice": "allow"},
    )
    return state, action


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_same_inflow_twice_produces_one_split():
    store = await _seeded(ledger_with(spendable=0, rent_required=150000))
    ledger = await store.load("u1")

    first = await split_inflow(
        store=store, ledger=ledger, inflow_id="pay_1", amount=money(420000)
    )
    after_first = await store.load("u1")
    second = await split_inflow(
        store=store, ledger=after_first, inflow_id="pay_1", amount=money(420000)
    )
    after_second = await store.load("u1")

    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert after_first.sleeves == after_second.sleeves
    assert after_second.sleeves["savings"] == money(126000)


async def test_idempotency_survives_a_fresh_ledger_load():
    """The guard is the stored ledger, not an in-process cache."""
    store = await _seeded(ledger_with())
    await split_inflow(
        store=store,
        ledger=await store.load("u1"),
        inflow_id="pay_2",
        amount=money(1000),
    )
    reloaded = await store.load("u1")
    replay = await split_inflow(
        store=store, ledger=reloaded, inflow_id="pay_2", amount=money(1000)
    )
    assert replay.idempotent_replay is True
    assert (await store.load("u1")).sleeves["spendable"] == money(700)


async def test_an_old_key_still_replays_after_many_later_receipts():
    """An idempotency key is only a guard while its receipt is still kept."""
    store = await _seeded(ledger_with())
    await split_inflow(
        store=store,
        ledger=await store.load("u1"),
        inflow_id="first",
        amount=money(1000),
    )
    for index in range(60):
        await split_inflow(
            store=store,
            ledger=await store.load("u1"),
            inflow_id=f"later_{index}",
            amount=money(10),
        )

    ledger = await store.load("u1")
    before = dict(ledger.sleeves)
    replay = await split_inflow(
        store=store, ledger=ledger, inflow_id="first", amount=money(1000)
    )
    after = await store.load("u1")

    assert replay.idempotent_replay is True
    assert after.sleeves == before


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("amount", ["420000", "420001.07", "1", "999.99", "0.01"])
async def test_the_split_reconciles_to_the_cent(amount):
    parts = split_parts(Track(), money(amount))
    assert sum(parts.values()) == money(amount)


async def test_rent_is_frozen_before_the_user_can_touch_it():
    store = await _seeded(ledger_with(spendable=0, rent_required=150000))
    await split_inflow(
        store=store,
        ledger=await store.load("u1"),
        inflow_id="pay_3",
        amount=money(420000),
    )
    ledger = await store.load("u1")

    # 70% of 420,000 is 294,000; rent needs 150,000 of it.
    assert ledger.sleeves["spendable"] == money(144000)
    assert ledger.sleeves["locked"] == money(150000)
    assert ledger.rent_first.reserved == money(150000)
    assert ledger.rent_first.gap == 0


async def test_rent_is_only_reserved_up_to_what_arrived():
    store = await _seeded(ledger_with(spendable=0, rent_required=500000))
    await split_inflow(
        store=store,
        ledger=await store.load("u1"),
        inflow_id="pay_4",
        amount=money(10000),
    )
    ledger = await store.load("u1")
    assert ledger.rent_first.reserved == money(7000)
    assert ledger.sleeves["spendable"] == money(0)
    assert ledger.rent_first.gap == money(493000)


# ---------------------------------------------------------------------------
# The four named guarantees
# ---------------------------------------------------------------------------


async def test_limit_breach_moves_nothing_and_returns_a_rejected_receipt():
    ledger = ledger_with(spendable=500000)
    store = await _seeded(ledger)
    state, _action = _authorised(ledger, "200000")

    outcome = await execute_transfer(
        store=store, ledger=ledger, state=state, policy=POLICY, rail=InMemoryRail()
    )

    assert outcome.receipt.status == "rejected"
    assert "OVER_LIMIT" in outcome.receipt.reasons
    assert outcome.receipt.sleeves_before == outcome.receipt.sleeves_after
    assert (await store.load("u1")).sleeves["spendable"] == money(500000)


async def test_a_limit_breach_at_the_orchestrator_leaves_a_rejected_receipt():
    """A refused order is still a result, and the trail shows the refusal."""
    store = await _seeded(
        ledger_with(
            spendable=90000, locked=150000, rent_required=150000, rent_reserved=150000
        )
    )
    rail = InMemoryRail()
    orchestrator = Orchestrator(
        store=store,
        policy=POLICY,
        rail=rail,
        provider=FakeProvider("Not moving that."),
        judge=judge_of(jev(intent="order", afford=0.1, violation=0.9)),
    )

    result = await orchestrator.handle(
        Event(type="utterance", user_id="u1", text="Send 200k to Femi")
    )

    assert rail.calls == []
    assert result.receipt is not None
    assert result.receipt.status == "rejected"
    assert result.decision["next_mode"] == "ask"


async def test_an_llm_emitting_send_200k_cannot_cause_a_transfer():
    """Prose from a model never becomes an action, a challenge, or a movement.

    The model's entire output is "send 200k to Femi". It is not even said: the
    200k is a figure STATE never contained, so the clamp replaces the sentence
    with the deterministic STATE line. Nothing was parsed, nothing moved, and
    there was never anything to confirm.
    """
    store = await _seeded(ledger_with(spendable=500000))
    rail = InMemoryRail()
    orchestrator = Orchestrator(
        store=store,
        policy=POLICY,
        rail=rail,
        provider=FakeProvider("send 200k to Femi"),
        judge=judge_of(jev(intent="smalltalk", mode="act", action="none")),
    )

    result = await orchestrator.handle(
        Event(type="utterance", user_id="u1", text="hey Miriam")
    )

    assert result.narration is not None
    assert "200k" not in result.narration
    assert "500,000" in result.narration
    assert rail.calls == []
    assert result.receipt is None
    assert result.confirm_id == ""
    assert (await store.load("u1")).sleeves["spendable"] == money(500000)


async def test_a_yield_partner_that_is_down_parks_the_money():
    ledger = ledger_with(spendable=100000)
    store = await _seeded(ledger)

    outcome = await route_yield(
        store=store,
        ledger=ledger,
        amount=money(50000),
        policy=POLICY,
        yield_rail=InMemoryRail(fail_with="partner 502"),
    )
    reloaded = await store.load("u1")

    assert outcome.receipt.status == "parked"
    assert "YIELD_RAIL_DOWN" in outcome.receipt.reasons
    assert reloaded.sleeves["yield"] == money(0)
    assert reloaded.sleeves["savings"] == money(50000)
    assert reloaded.sleeves["spendable"] == money(50000)
    assert "No yield was posted" in outcome.receipt.detail


async def test_a_healthy_yield_rail_does_post_to_the_yield_sleeve():
    ledger = ledger_with(spendable=100000)
    store = await _seeded(ledger)

    outcome = await route_yield(
        store=store,
        ledger=ledger,
        amount=money(50000),
        policy=POLICY,
        yield_rail=InMemoryRail(),
    )
    reloaded = await store.load("u1")
    assert outcome.receipt.status == "executed"
    assert reloaded.sleeves["yield"] == money(50000)


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------


async def test_a_missing_required_field_is_reported_not_guessed():
    ledger = new_ledger("u1")
    ledger.sleeves.pop("locked")
    state = build_state(ledger=ledger, policy=POLICY)

    assert state.status == "insufficient_state"
    assert "sleeves.locked" in state.missing


async def test_an_incomplete_state_cannot_be_executed():
    ledger = new_ledger("u1")
    ledger.sleeves.pop("locked")
    state = build_state(ledger=ledger, policy=None)
    with pytest.raises(InsufficientState):
        require_complete(state)


async def test_state_carries_the_policy_slice_and_the_receipts():
    ledger = ledger_with(spendable=5000)
    store = await _seeded(ledger)
    state, _action = _authorised(ledger, "1500")
    await execute_transfer(
        store=store, ledger=ledger, state=state, policy=POLICY, rail=InMemoryRail()
    )
    reloaded = await store.load("u1")
    refreshed = build_state(ledger=reloaded, policy=POLICY)

    assert refreshed.policy["max_auto"] == "2000.00"
    assert refreshed.policy["locked_sleeves"] == ["locked"]
    assert refreshed.last_receipts
    assert isinstance(refreshed.last_receipts[-1], Receipt)


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


async def test_money_reserved_for_rent_cannot_be_unlocked():
    ledger = ledger_with(
        spendable=0, locked=150000, rent_required=150000, rent_reserved=150000
    )
    store = await _seeded(ledger)

    outcome = await move_between_sleeves(
        store=store,
        ledger=ledger,
        from_sleeve="locked",
        to_sleeve="spendable",
        amount=money(10000),
        policy=POLICY,
        action="unlock",
        decision_id="dec_unlock",
    )

    assert outcome.receipt.status == "rejected"
    assert "RENT_RESERVED" in outcome.receipt.reasons
    assert (await store.load("u1")).sleeves["locked"] == money(150000)


async def test_a_lock_is_an_internal_move_that_never_leaves_the_user():
    ledger = ledger_with(spendable=50000)
    store = await _seeded(ledger)

    outcome = await move_between_sleeves(
        store=store,
        ledger=ledger,
        from_sleeve="spendable",
        to_sleeve="locked",
        amount=money(20000),
        policy=POLICY,
        action="lock",
        decision_id="dec_lock",
    )
    reloaded = await store.load("u1")
    assert outcome.receipt.status == "executed"
    assert reloaded.sleeves["locked"] == money(20000)
    assert reloaded.sleeves["spendable"] == money(30000)


async def test_a_locked_sleeve_is_never_a_funding_source():
    report = evaluate_limits(
        policy=POLICY,
        amount=money(100),
        sleeve="locked",
        spendable=money(0),
        rent_required=money(0),
        rent_reserved=money(0),
    )
    assert report.allowed is False
    assert "LOCKED_SLEEVE" in report.reasons


# ---------------------------------------------------------------------------
# Rollback and concurrency
# ---------------------------------------------------------------------------


class _SaveFailsOnCommit(InMemoryLedgerStore):
    """A store whose commit fails, after the rail has already moved the money."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    async def save(self, ledger: Ledger) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("ledger write failed")
        await super().save(ledger)


async def test_a_failed_commit_is_compensated_by_a_rail_reversal():
    ledger = ledger_with(spendable=50000)
    store = _SaveFailsOnCommit()
    store.seed(ledger)
    rail = InMemoryRail()
    state, _action = _authorised(ledger, "1500")

    store.fail_next = True
    outcome = await execute_transfer(
        store=store, ledger=ledger, state=state, policy=POLICY, rail=rail
    )

    assert outcome.receipt.status == "rejected"
    assert "ROLLBACK" in outcome.receipt.reasons
    assert rail.reversals, "the rail was asked to put the money back"

    # The persisted ledger must be the pre-rail state: the debit was taken back
    # by the rail, so recording it would be a lie, and the ROLLBACK receipt must
    # be there or the incident is invisible to a later turn.
    persisted = await store.load("u1")
    assert persisted.sleeves["spendable"] == money(50000)
    assert persisted.receipts, "the rollback must be persisted, not just returned"
    assert persisted.receipts[-1].reasons == ["ROLLBACK"]
    assert persisted.receipt_for(outcome.receipt.idempotency_key) is not None


async def test_the_ledger_rejects_a_stale_write():
    store = await _seeded(ledger_with(spendable=1000))
    stale = await store.load("u1")
    fresh = await store.load("u1")
    fresh.credit("spendable", money(500))
    await store.save(fresh)

    with pytest.raises(LedgerConflictError):
        await store.save(stale)


# ---------------------------------------------------------------------------
# The parse, and the shape of STATE
# ---------------------------------------------------------------------------


def test_the_parse_is_regex_and_needs_a_clear_sentence():
    action = parse_transfer_utterance("Send 200k to Femi")
    assert action is not None
    assert action.amount == money(200000)
    assert action.counterparty == "Femi"
    assert action.source == "user"

    assert parse_transfer_utterance("what is my balance") is None
    assert parse_transfer_utterance("") is None


def test_the_parse_reads_a_purchase_as_advice_shaped():
    action = parse_transfer_utterance("can I buy this phone for 95k")
    assert action is not None
    assert action.type == "purchase"
    assert action.amount == money(95000)


def test_an_unknown_sleeve_is_a_hard_error():
    ledger = new_ledger("u1")
    with pytest.raises(LedgerError):
        ledger.credit("chequing", Decimal("10"))


def test_state_exposes_spendable_without_arithmetic():
    state = build_state(ledger=ledger_with(spendable=1234), policy=POLICY)
    assert isinstance(state, HandlerState)
    assert state.spendable == money(1234)


# ---------------------------------------------------------------------------
# A ledger that cannot be reached is a typed failure, not a guess
# ---------------------------------------------------------------------------


class _BrokenRedis:
    """A Redis that is down: every call raises, as a real outage would."""

    async def get(self, key):  # noqa: ANN001, ANN201, ARG002
        raise ConnectionError("redis is down")

    async def set(self, key, value):  # noqa: ANN001, ANN201, ARG002
        raise ConnectionError("redis is down")


def _broken_store(*, single_process: bool) -> RedisLedgerStore:
    store = RedisLedgerStore(single_process=single_process)
    store._redis = _BrokenRedis()  # type: ignore[assignment]
    return store


async def test_a_redis_outage_raises_rather_than_inventing_a_balance():
    """With more than one worker the store refuses; it does not fall back.

    A silent fallback would give the same user two ledgers, so the ledger says it
    could not be read and the caller tells the user the turn did not complete.
    """
    store = _broken_store(single_process=False)

    with pytest.raises(LedgerUnavailable):
        await store.load("u1")
    with pytest.raises(LedgerUnavailable):
        await store.save(ledger_with(spendable=1000))


async def test_a_single_process_deployment_may_degrade_in_memory():
    """The one exception, and it has to be opted into explicitly."""
    store = _broken_store(single_process=True)
    ledger = ledger_with(spendable=1000)

    assert await store.load("u1") is None  # nothing to read yet, not an error
    await store.save(ledger)
    reloaded = await store.load("u1")
    assert reloaded is not None
    assert reloaded.sleeves["spendable"] == money(1000)


def test_the_single_process_flag_defaults_off():
    """A default of on would make the divergence bug the silent one."""
    from miriam_agent.config.settings import Settings

    settings = Settings(_env_file=None)
    assert settings.MONEY_SINGLE_PROCESS is False
    assert RedisLedgerStore().single_process is False


async def test_two_writers_holding_the_same_version_cannot_both_win():
    """A `>` check let both through, so the second save silently overwrote the
    first -- movements and idempotency keys included. The guard is equality."""
    store = await _seeded(ledger_with(spendable=1000))
    first = await store.load("u1")
    second = await store.load("u1")  # same version, before either writes

    first.credit("spendable", money(500))
    await store.save(first)

    second.credit("spendable", money(1))
    with pytest.raises(LedgerConflictError):
        await store.save(second)

    # The loser's write is not in the ledger.
    assert (await store.load("u1")).sleeves["spendable"] == money(1500)


async def test_a_write_that_lands_advances_the_callers_version():
    """The caller's version moves only once the CAS has succeeded, so the next
    save from the same object is not rejected as stale."""
    store = await _seeded(ledger_with(spendable=1000))
    ledger = await store.load("u1")

    await store.save(ledger)
    before = ledger.version

    ledger.credit("spendable", money(10))
    await store.save(ledger)  # must not conflict with its own last write

    assert ledger.version == before + 1
    assert (await store.load("u1")).sleeves["spendable"] == money(1010)


async def test_the_rollback_receipt_carries_the_rail_reference():
    """Reconciliation needs to name the rail movement that was reversed."""
    ledger = ledger_with(spendable=50000)
    store = _SaveFailsOnCommit()
    store.seed(ledger)
    rail = InMemoryRail()
    state, _action = _authorised(ledger, "1500")

    store.fail_next = True
    outcome = await execute_transfer(
        store=store, ledger=ledger, state=state, policy=POLICY, rail=rail
    )

    assert outcome.receipt.rail_reference, "the rollback must name the rail move"
    # The same reference the rail was asked to reverse, so the reversed movement
    # can be found on the rail's side.
    assert outcome.receipt.rail_reference in rail.reversals

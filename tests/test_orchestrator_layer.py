"""The acceptance fixture pack. Five scenarios, the ones the design names.

1. Payday 420k, track 70/30, rent in 6 days 150k. The split happens in Hands and
   Voice sends a receipt carrying those numbers.
2. "Send 200k to Femi" when spendable after the rent reserve is 90k. Judgment
   asks and denies, and zero transfers happen.
3. The user taps CONFIRM on a challenge. Hands moves the exact challenge amount
   and Voice reports the receipt.
4. Swap the Voice model. Same STATE, same execution, only the wording changes.
5. Kill the LLM. The auto-split still runs.

Scenario 5 is the one that decides what this is: if it fails, the thing is a
chatbot with a wallet rather than a ledger with a narrator.
"""

from __future__ import annotations

from layer_fakes import POLICY, FakeProvider, jev, judge_of, ledger_with

from miriam_agent.hands.ledger import InMemoryLedgerStore, money
from miriam_agent.hands.split import split_parts
from miriam_agent.hands.transfer import InMemoryRail
from miriam_agent.orchestrator import Event, Orchestrator


async def _orchestrator(ledger, *, provider, judge=None, rail=None, yield_rail=None):
    store = InMemoryLedgerStore()
    store.seed(ledger)
    return (
        store,
        Orchestrator(
            store=store,
            policy=POLICY,
            rail=rail or InMemoryRail(),
            yield_rail=yield_rail,
            provider=provider,
            judge=judge or judge_of(jev()),
        ),
    )


# ---------------------------------------------------------------------------
# 1. Payday
# ---------------------------------------------------------------------------


async def test_acceptance_1_payday_splits_in_hands_and_voice_sends_the_receipt():
    ledger = ledger_with(spendable=0, rent_required=150000, due_in_days=6)
    voice = FakeProvider(
        "420,000 came in. Spendable is 144,000, savings 126,000, and 150,000 is "
        "reserved for rent in 6 days."
    )
    store, orchestrator = await _orchestrator(ledger, provider=voice)

    result = await orchestrator.handle(
        Event(
            type="inflow",
            user_id="u1",
            inflow_id="payday_sept",
            amount=money(420000),
            source_raw="ACME PAYROLL SEPTEMBER",
        )
    )
    after = await store.load("u1")

    # The split is Hands' arithmetic, and it reconciles exactly.
    assert after.sleeves["spendable"] == money(144000)
    assert after.sleeves["savings"] == money(126000)
    assert after.sleeves["locked"] == money(150000)
    assert after.rent_first.reserved == money(150000)

    # Voice narrated the receipt, and its numbers are the ones in STATE.
    assert result.receipt is not None
    assert result.receipt.status == "executed"
    assert result.state.execution is not None
    assert result.state.execution.action == "inflow_split"
    assert "144,000" in result.narration


async def test_acceptance_1_the_receipt_numbers_are_the_ledger_numbers():
    ledger = ledger_with(spendable=0, rent_required=150000, due_in_days=6)
    store, orchestrator = await _orchestrator(
        ledger, provider=FakeProvider("Split done.")
    )
    result = await orchestrator.handle(
        Event(
            type="inflow",
            user_id="u1",
            inflow_id="payday_sept",
            amount=money(420000),
        )
    )
    parts = split_parts(ledger.track, money(420000))
    receipt = result.receipt
    assert receipt is not None
    assert receipt.amount == money(420000)
    assert receipt.sleeves_after["savings"] == str(parts["savings"])
    assert receipt.sleeves_after["locked"] == "150000.00"


# ---------------------------------------------------------------------------
# 2. The over-limit send
# ---------------------------------------------------------------------------


async def test_acceptance_2_over_limit_send_moves_nothing():
    ledger = ledger_with(
        spendable=90000,
        savings=126000,
        locked=150000,
        rent_required=150000,
        rent_reserved=150000,
    )
    rail = InMemoryRail()
    store, orchestrator = await _orchestrator(
        ledger,
        provider=FakeProvider("You have 90,000. 200,000 is not there."),
        judge=judge_of(jev(intent="order", afford=0.1, violation=1.0)),
        rail=rail,
    )

    result = await orchestrator.handle(
        Event(type="utterance", user_id="u1", text="Send 200k to Femi")
    )
    after = await store.load("u1")

    assert rail.calls == []
    assert after.sleeves["spendable"] == money(90000)
    assert result.decision["next_mode"] == "ask"
    assert result.decision["action_choice"] == "deny"
    assert result.receipt is not None
    assert result.receipt.status == "rejected"


async def test_acceptance_2_rent_short_is_called_out_by_name():
    """The spec's worked example: 184k liquid, 120k rent in 9 days, 95k ask."""
    ledger = ledger_with(spendable=184000, rent_required=120000, due_in_days=9)
    rail = InMemoryRail()
    _store, orchestrator = await _orchestrator(
        ledger,
        provider=FakeProvider("95,000 leaves you short of rent."),
        judge=judge_of(jev(intent="order", afford=0.21, violation=0.9)),
        rail=rail,
    )

    result = await orchestrator.handle(
        Event(type="utterance", user_id="u1", text="send 95k to Ada")
    )

    assert rail.calls == []
    assert "RENT_SHORT" in result.decision["reasons"]
    assert result.decision["action_choice"] == "deny"


# ---------------------------------------------------------------------------
# 3. The confirm tap
# ---------------------------------------------------------------------------


async def test_acceptance_3_a_confirm_tap_moves_the_exact_challenge_amount():
    ledger = ledger_with(
        spendable=90000, locked=150000, rent_required=150000, rent_reserved=150000
    )
    rail = InMemoryRail()
    store, orchestrator = await _orchestrator(
        ledger,
        provider=FakeProvider("Asking first."),
        judge=judge_of(jev(intent="order", afford=0.9)),
        rail=rail,
    )

    asked = await orchestrator.handle(
        Event(type="utterance", user_id="u1", text="send 5k to Ada")
    )
    assert asked.confirm_id
    assert asked.decision["action_choice"] == "allow_smaller"
    assert rail.calls == []

    tapped = await orchestrator.handle(
        Event(type="confirm", user_id="u1", confirm_id=asked.confirm_id)
    )
    after = await store.load("u1")

    assert tapped.receipt is not None
    assert tapped.receipt.status == "executed"
    # The challenge amount is the cap Hands computed, not the 5,000 requested.
    assert tapped.receipt.amount == money(2000)
    assert rail.calls[0].amount == money(2000)
    assert after.sleeves["spendable"] == money(88000)
    assert tapped.narration


async def test_acceptance_3_a_challenge_cannot_be_replayed():
    ledger = ledger_with(spendable=90000)
    rail = InMemoryRail()
    _store, orchestrator = await _orchestrator(
        ledger,
        provider=FakeProvider("Asking first."),
        judge=judge_of(jev(intent="order")),
        rail=rail,
    )
    asked = await orchestrator.handle(
        Event(type="utterance", user_id="u1", text="send 5k to Ada")
    )
    await orchestrator.handle(
        Event(type="confirm", user_id="u1", confirm_id=asked.confirm_id)
    )
    second = await orchestrator.handle(
        Event(type="confirm", user_id="u1", confirm_id=asked.confirm_id)
    )

    assert len(rail.calls) == 1
    assert second.receipt is not None
    assert second.receipt.status == "rejected"
    assert "CHALLENGE_EXPIRED" in "".join(second.receipt.reasons)


async def test_acceptance_3_a_tap_on_an_id_hands_never_issued_is_refused():
    ledger = ledger_with(spendable=90000)
    rail = InMemoryRail()
    _store, orchestrator = await _orchestrator(
        ledger,
        provider=FakeProvider("Asking first."),
        judge=judge_of(jev(intent="order")),
        rail=rail,
    )
    result = await orchestrator.handle(
        Event(type="confirm", user_id="u1", confirm_id="confirm_made_up")
    )

    assert rail.calls == []
    assert result.receipt is not None
    assert result.receipt.status == "rejected"
    assert "NO_SUCH_CHALLENGE" in result.receipt.reasons


# ---------------------------------------------------------------------------
# 4. Swap the model
# ---------------------------------------------------------------------------


async def test_acceptance_4_swapping_the_model_changes_only_the_wording():
    """Same STATE, same execution, different words. Nothing else moves."""
    runs = []
    for text in (
        "Send to Ada: 2,000. That is what is safe before rent.",
        "2,000 is the number. Everything above it breaks rent.",
    ):
        ledger = ledger_with(spendable=90000)
        rail = InMemoryRail()
        store, orchestrator = await _orchestrator(
            ledger,
            provider=FakeProvider(text),
            judge=judge_of(jev(intent="order")),
            rail=rail,
        )
        asked = await orchestrator.handle(
            Event(type="utterance", user_id="u1", text="send 5k to Ada")
        )
        tapped = await orchestrator.handle(
            Event(type="confirm", user_id="u1", confirm_id=asked.confirm_id)
        )
        runs.append((text, store, tapped, rail))

    (text_a, store_a, result_a, rail_a), (text_b, store_b, result_b, rail_b) = runs

    # Identical execution, identical decision, identical balances.
    assert result_a.receipt.amount == result_b.receipt.amount == money(2000)
    assert [c.amount for c in rail_a.calls] == [c.amount for c in rail_b.calls]
    assert (await store_a.load("u1")).sleeves == (await store_b.load("u1")).sleeves
    assert result_a.decision["action_choice"] == result_b.decision["action_choice"]
    # Only the wording differs.
    assert text_a != text_b
    assert result_a.narration == text_a
    assert result_b.narration == text_b


# ---------------------------------------------------------------------------
# 5. Kill the LLM
# ---------------------------------------------------------------------------


async def test_acceptance_5_killing_the_llm_does_not_stop_the_split():
    ledger = ledger_with(spendable=0, rent_required=150000, due_in_days=6)
    store, orchestrator = await _orchestrator(ledger, provider=FakeProvider(fail=True))

    result = await orchestrator.handle(
        Event(
            type="inflow",
            user_id="u1",
            inflow_id="payday_no_llm",
            amount=money(420000),
            source_raw="payroll",
        )
    )
    after = await store.load("u1")

    assert after.sleeves["spendable"] == money(144000)
    assert after.sleeves["savings"] == money(126000)
    assert after.sleeves["locked"] == money(150000)
    assert result.receipt is not None
    assert result.receipt.status == "executed"
    # Narration degrades to STATE; it does not disappear.
    assert result.narration


async def test_acceptance_5_the_split_is_identical_with_and_without_a_model():
    ledgers = []
    for provider in (FakeProvider("Split done."), None):
        ledger = ledger_with(spendable=0, rent_required=150000, due_in_days=6)
        store = InMemoryLedgerStore()
        store.seed(ledger)
        orchestrator = Orchestrator(
            store=store,
            policy=POLICY,
            rail=InMemoryRail(),
            provider=provider,
            judge=judge_of(jev()),
        )
        await orchestrator.handle(
            Event(
                type="inflow",
                user_id="u1",
                inflow_id="payday_cmp",
                amount=money(420000),
            )
        )
        ledgers.append((await store.load("u1")).sleeves)

    assert ledgers[0] == ledgers[1]


async def test_acceptance_5_a_dead_model_cannot_block_a_typed_transfer():
    """Voice is not on the path that moves money, so its death is not fatal."""
    ledger = ledger_with(spendable=50000)
    rail = InMemoryRail()
    store, orchestrator = await _orchestrator(
        ledger,
        provider=FakeProvider(fail=True),
        judge=judge_of(jev(intent="order")),
        rail=rail,
    )

    result = await orchestrator.handle(
        Event(type="utterance", user_id="u1", text="send 1.5k to Ada")
    )
    after = await store.load("u1")

    assert result.decision["next_mode"] == "act"
    assert len(rail.calls) == 1
    assert after.sleeves["spendable"] == money(48500)


# ---------------------------------------------------------------------------
# The shape of the system
# ---------------------------------------------------------------------------


def test_no_llm_money_tool_is_defined_in_any_of_the_three_layers():
    """A grep for the forbidden tool names, so one cannot be added quietly.

    Voice and Judgment must not reach the tool registry at all: a model that can
    be handed a tool named ``send_money`` is a model that can move money.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "miriam_agent"
    forbidden = (
        "send_money",
        "transfer_stash_to_spending",
        "transfer_spending_to_stash",
        "pay_bill",
        "buy_asset",
        "sell_asset",
        "enroll_strategy",
        "split_income",
    )
    for package in ("voice", "judgment"):
        for path in (root / package).rglob("*.py"):
            source = path.read_text()
            for name in forbidden:
                assert f'"{name}"' not in source, f"{path} names a money tool"
            assert "get_registry" not in source, f"{path} reaches the tool registry"
            assert "registry.register" not in source, f"{path} registers a tool"


def test_hands_never_imports_a_model_or_a_judge():
    """The deterministic layer stays deterministic, checked by import graph."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "miriam_agent" / "hands"
    for path in root.rglob("*.py"):
        source = path.read_text()
        assert "get_llm_provider" not in source, path
        assert "typesafe" not in source, path
        assert "ChatMessage" not in source, path

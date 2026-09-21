"""Dual-run: replay 50 recorded transcripts through the Orchestrator and compare.

Each transcript in ``tests/fixtures/money_layers/transcripts.json`` is a recording
of one turn of pre-flip traffic: the ledger it started from, the user's words,
the JEV answers it was judged with, and what the **old path** actually did with
it. The new path is replayed through ``Orchestrator`` and compared.

The pass condition is the one the design states: **zero unexpected movements.**

* A turn with no confirmation moves nothing. This is the strictest form of the
  check and the one that matters most, because moving money on an unconfirmed
  turn is the failure the whole layer split exists to prevent.
* A turn with a confirmation moves no more than the old path did, to the same
  sleeve and the same counterparty. Moving *less* is not a failure: the old path
  staged the amount the user asked for, while the new one settles the amount the
  ledger's own cap allows.
* An inflow splits exactly as recorded, with no rail call at all. Money moving
  between a user's own sleeves is not a rail movement.

This is a replay, not a live comparison: the old writer is deleted, so its
behaviour can only be compared against, not re-run. That is why the recording
is the fixture.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal
from typing import Any

import pytest
from layer_fakes import FakeProvider, broken_judge, jev, judge_of

from miriam_agent.hands.ledger import (
    InMemoryLedgerStore,
    Ledger,
    RentFirst,
    Track,
    money,
)
from miriam_agent.hands.limits import Policy
from miriam_agent.hands.transfer import InMemoryRail, TransferInstruction
from miriam_agent.orchestrator import Orchestrator

FIXTURE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "money_layers"
    / "transcripts.json"
)


def _load() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text())["transcripts"]


TRANSCRIPTS = _load()


class RecordingRail(InMemoryRail):
    """A rail that records what it was asked to move, not just that it was."""

    def __init__(self) -> None:
        super().__init__()
        self.moves: list[dict[str, str]] = []

    async def execute(self, instruction: TransferInstruction):  # noqa: ANN201
        self.moves.append(
            {
                "amount": str(instruction.amount),
                "sleeve": instruction.sleeve,
                "counterparty": instruction.counterparty,
            }
        )
        return await super().execute(instruction)


def _ledger_from(spec: dict[str, Any], user_id: str) -> Ledger:
    ledger = Ledger(
        user_id=user_id,
        track=Track(name="70/30", spend=Decimal("70"), save=Decimal("30")),
    )
    ledger.sleeves = {
        "spendable": money(spec.get("spendable", 0)),
        "savings": money(spec.get("savings", 0)),
        "yield": money(spec.get("yield", 0)),
        "locked": money(spec.get("locked", 0)),
    }
    ledger.rent_first = RentFirst(
        required=money(spec.get("rent_required", 0)),
        reserved=money(spec.get("rent_reserved", 0)),
        due_in_days=spec.get("due_in_days"),
    )
    return ledger


def _judge_for(transcript: dict[str, Any]):
    spec = transcript.get("judge")
    if spec is None:
        return broken_judge()
    return judge_of(
        jev(
            inflow=spec.get("inflow", "salary"),
            intent=spec.get("intent", "order"),
            afford=spec.get("afford", 0.9),
            violation=spec.get("violation", 0.0),
            reversible=spec.get("reversible", 0.9),
            mode=spec.get("mode", "act"),
            action=spec.get("action", "allow"),
        )
    )


async def _replay(transcript: dict[str, Any]) -> dict[str, Any]:
    """Run one transcript through the new path and report what happened."""
    user_id = f"dual_{transcript['id']}"
    store = InMemoryLedgerStore()
    store.seed(_ledger_from(transcript["ledger"], user_id))
    rail = RecordingRail()
    orchestrator = Orchestrator(
        store=store,
        policy=Policy(
            max_auto=money(2000),
            max_with_confirm=money(100000),
            reversible_under=money(2000),
        ),
        rail=rail,
        provider=FakeProvider("Noted."),
        judge=_judge_for(transcript),
    )

    kind = transcript["kind"]
    splits = 0
    confirm_id = ""
    if kind == "utterance":
        result = await orchestrator.handle_utterance(user_id, transcript["text"])
        confirm_id = result.confirm_id
        if transcript.get("confirmed") and confirm_id:
            await orchestrator.handle_confirm(user_id, confirm_id, yes=True)
    elif kind == "inflow":
        await orchestrator.handle_inflow(
            user_id,
            payment_id=transcript["payment_id"],
            amount=money(transcript["amount"]),
            source_raw=transcript.get("source_raw", ""),
        )
        splits = 1
    elif kind == "inflow_twice":
        for _ in range(2):
            await orchestrator.handle_inflow(
                user_id,
                payment_id=transcript["payment_id"],
                amount=money(transcript["amount"]),
                source_raw=transcript.get("source_raw", ""),
            )
        splits = 1
    elif kind == "confirm_unknown":
        await orchestrator.handle_confirm(user_id, transcript["confirm_id"], yes=True)
    elif kind == "confirm_declined":
        issued = await orchestrator.handle_utterance(user_id, transcript["text"])
        if issued.confirm_id:
            await orchestrator.handle_confirm(user_id, issued.confirm_id, yes=False)
    else:
        raise AssertionError(f"unknown transcript kind {kind!r}")

    ledger = await store.load(user_id)
    return {
        "rail_moves": rail.moves,
        "sleeves": {k: str(v) for k, v in ledger.sleeves.items()},
        "splits": splits,
        "confirm_id": confirm_id,
    }


def _amount_of(move: dict[str, str]) -> Decimal:
    return Decimal(move["amount"])


def test_the_pack_is_fifty_transcripts():
    assert len(TRANSCRIPTS) == 50
    assert len({t["id"] for t in TRANSCRIPTS}) == 50


@pytest.mark.parametrize("transcript", TRANSCRIPTS, ids=[t["id"] for t in TRANSCRIPTS])
async def test_replayed_transcript_has_no_unexpected_movement(transcript):
    """The dual-run gate, one transcript at a time."""
    new = await _replay(transcript)
    old = transcript["old_path"]

    if not transcript["confirmed"]:
        # The heart of it: no confirmation, no movement, no exceptions.
        assert new["rail_moves"] == [], transcript["note"]
    else:
        old_moves = old["rail_moves"]
        assert len(new["rail_moves"]) <= len(old_moves), transcript["note"]
        for new_move, old_move in zip(new["rail_moves"], old_moves):
            # Same sleeve, same counterparty, and never more money.
            assert new_move["sleeve"] == old_move["sleeve"], transcript["note"]
            assert new_move["counterparty"] == old_move["counterparty"], transcript[
                "note"
            ]
            assert _amount_of(new_move) <= _amount_of(old_move), transcript["note"]


@pytest.mark.parametrize("transcript", TRANSCRIPTS, ids=[t["id"] for t in TRANSCRIPTS])
async def test_no_movement_is_ever_absent_from_the_recording(transcript):
    """The unconditional form: a movement the old path never made is a failure."""
    new = await _replay(transcript)
    recorded = {
        (m["sleeve"], m["counterparty"]) for m in transcript["old_path"]["rail_moves"]
    }
    for move in new["rail_moves"]:
        assert (move["sleeve"], move["counterparty"]) in recorded, transcript["note"]


@pytest.mark.parametrize(
    "transcript",
    [t for t in TRANSCRIPTS if t["kind"] == "inflow"],
    ids=[t["id"] for t in TRANSCRIPTS if t["kind"] == "inflow"],
)
async def test_a_recorded_inflow_splits_the_same_way_with_no_rail_call(transcript):
    """Money arriving is Hands' arithmetic, and it never touches a rail."""
    new = await _replay(transcript)
    recorded = transcript["old_path"]["split"]
    assert new["rail_moves"] == []
    assert new["sleeves"]["spendable"] == recorded["spendable"]
    assert new["sleeves"]["savings"] == recorded["savings"]
    assert new["sleeves"]["locked"] == recorded["locked"]


@pytest.mark.parametrize(
    "transcript",
    [t for t in TRANSCRIPTS if t["kind"] == "inflow_twice"],
    ids=[t["id"] for t in TRANSCRIPTS if t["kind"] == "inflow_twice"],
)
async def test_a_double_delivered_payment_splits_once(transcript):
    """The rail retrying a webhook must not pay the user twice."""
    new = await _replay(transcript)
    recorded = transcript["old_path"]["split"]
    assert new["splits"] == transcript["old_path"]["splits"] == 1
    assert new["sleeves"]["savings"] == recorded["savings"]
    assert new["sleeves"]["spendable"] == recorded["spendable"]
    assert new["sleeves"]["locked"] == recorded["locked"]


async def test_the_whole_pack_moves_money_only_where_it_was_confirmed():
    """The summary the flip decision is made on."""
    unconfirmed_moves = 0
    confirmed_moves = 0
    for transcript in TRANSCRIPTS:
        new = await _replay(transcript)
        if transcript["confirmed"]:
            confirmed_moves += len(new["rail_moves"])
        else:
            unconfirmed_moves += len(new["rail_moves"])

    assert unconfirmed_moves == 0, "the Orchestrator moved money unconfirmed"
    assert confirmed_moves > 0, "no transcript exercised the confirmed path"

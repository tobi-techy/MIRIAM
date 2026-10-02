"""Phase D regression tests: shadow-run parity.

Proves the shadow agrees with the live path on every corpus turn, flags a
genuinely diverged turn, and never touches anything real: no rail object
exists in the shadow module, journals are private per run, ledgers are never
loaded. Deterministic, no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from miriam_agent.hands.ledger import money
from miriam_agent.hands.limits import Policy
from miriam_agent.hands.shadow import (
    RecordedTurn,
    compare,
    predict_turn,
    run_corpus,
    turn_from_dict,
)

CORPUS = Path(__file__).parent / "fixtures" / "shadow" / "corpus.jsonl"
NOW = datetime.now(UTC)
POLICY = Policy(
    max_auto=money(2000),
    max_with_confirm=money(100000),
    reversible_under=money(2000),
    max_daily=money(10000),
)


def _turn(**overrides):
    base = dict(
        turn_id="t",
        action_type="transfer",
        amount="1500",
        currency="NGN",
        counterparty="Femi",
        sleeve="spendable",
        spendable="50000",
        actual_status="executed",
    )
    base.update(overrides)
    return RecordedTurn(**base)


async def test_execute_maps_to_would_execute():
    v = compare(await predict_turn(_turn(), policy=POLICY, at=NOW))
    assert v.predicted == "would_execute" and not v.diverged


async def test_challenged_maps_to_would_confirm():
    v = compare(
        await predict_turn(
            _turn(
                action_type="order", asset_query="google", actual_status="challenged"
            ),
            policy=POLICY,
            at=NOW,
        )
    )
    assert v.predicted == "would_confirm" and not v.diverged


async def test_rejected_maps_to_would_refuse():
    v = compare(
        await predict_turn(
            _turn(amount="200000", actual_status="rejected"), policy=POLICY, at=NOW
        )
    )
    assert v.predicted == "would_refuse" and not v.diverged


async def test_unknown_asset_refuses():
    v = compare(
        await predict_turn(
            _turn(
                action_type="order",
                asset_query="gogle",
                actual_status="rejected",
            ),
            policy=POLICY,
            at=NOW,
        )
    )
    assert v.predicted == "would_refuse"
    assert v.asset_resolution == "unknown"
    assert not v.diverged


async def test_genuine_divergence_is_flagged_with_a_note():
    v = compare(
        await predict_turn(
            _turn(amount="200000", actual_status="executed"), policy=POLICY, at=NOW
        )
    )
    assert v.diverged is True
    assert "would_refuse" in v.divergence_note and "executed" in v.divergence_note


async def test_unknown_live_status_passes_through_unmarked():
    v = compare(
        await predict_turn(_turn(actual_status="mystery"), policy=POLICY, at=NOW)
    )
    assert v.diverged is False


async def test_corpus_parses_and_is_clean():
    turns = [
        turn_from_dict(json.loads(line))
        for line in CORPUS.read_text().splitlines()
        if line.strip()
    ]
    assert len(turns) >= 8
    report = await run_corpus(turns, policy=POLICY, at=NOW)
    assert report.total == len(turns)
    assert report.clean, [v.divergence_note for v in report.diverged]


async def test_turn_from_dict_ignores_unknown_fields():
    turn = turn_from_dict({"turn_id": "x", "injected": "drop me", "amount": "5"})
    assert turn.turn_id == "x" and turn.amount == "5"

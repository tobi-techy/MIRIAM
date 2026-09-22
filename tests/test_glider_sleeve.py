"""Stocklana sleeve loop: reads fail closed, invest parses, binding holds.

Hermetic: no network, no JEV, no Glider. Go-host calls are fakes shaped like
the real ``/api/v1/investments/*`` responses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from miriam_agent.charts import sleeve_donut, split_bar
from miriam_agent.hands.invest import (
    INVEST_SLEEVE,
    parse_invest_utterance,
    prepare_allocate,
    settle_allocate,
)
from miriam_agent.hands.ledger import InMemoryLedgerStore, Ledger, money
from miriam_agent.hands.limits import Policy
from miriam_agent.orchestrator import Orchestrator, _action_type
from miriam_agent.tools.glider_sleeve import (
    deposit_address_from_caip,
    parse_caip10,
    parse_caip19,
)
from tests.layer_fakes import jev, judge_of, ledger_with

GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
OWNER = (
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp:"
    "7xKXTagR7k8J2y8K9mN3pQ5rS7tU9vW1xY3zABcD5eF"
)
SLEEVE_ROW = {
    "id": "d15c0ba1-71c0-4a11-8aa1-000000000001",
    "name": "Rail Stock Sleeve",
    "glider_strategy_id": "01M333PQNR9Y2WJ4D78FSRECEQ",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def make_calls(
    *,
    strategies: list[dict[str, Any]] | None = None,
    owner: str = OWNER,
    sign_payload: str = "AgAB signed-bytes",
    flow_id: str = "flow_test123",
    enrolled: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fake Go-host calls shaped like the staged-confirmation API."""
    prepared = {
        "status": "COMPLETED",
        "flow_id": flow_id,
        "account_index": "0",
        "agent_account_id": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp:Ag3nt",
        "chain_ids": [1399811149],
        "sign_payload": sign_payload,
        "deposit_account_id": OWNER,
        "live": True,
    }
    done = (
        enrolled
        if enrolled is not None
        else {
            "status": "COMPLETED",
            "enrollment": {"glider_portfolio_id": "pf_1"},
            "funding": {"status": "SUBMITTED"},
        }
    )

    async def list_strategies() -> dict[str, Any]:
        return {"strategies": strategies if strategies is not None else [SLEEVE_ROW]}

    async def get_owner() -> dict[str, Any]:
        return {"owner_account_id": owner}

    async def prepare_call(payload: dict[str, Any]) -> dict[str, Any]:
        if "confirmation_token" not in payload:
            return {
                "status": "AWAITING_CONFIRMATION",
                "confirmation": {"token": "cfm-1"},
            }
        assert payload["strategy_id"] == SLEEVE_ROW["id"]
        assert payload["owner_account_id"] == owner
        return prepared

    async def complete_call(payload: dict[str, Any]) -> dict[str, Any]:
        if "confirmation_token" not in payload:
            return {
                "status": "AWAITING_CONFIRMATION",
                "confirmation": {"token": "cfm-2"},
            }
        assert payload["flow_id"] == flow_id
        assert payload["account_index"] == "0"
        return done

    return {
        "list_strategies": list_strategies,
        "get_owner": get_owner,
        "prepare_call": prepare_call,
        "complete_call": complete_call,
    }


def funded_ledger() -> Ledger:
    ledger = ledger_with("u1", savings=100)
    return ledger


# -- CAIP --------------------------------------------------------------------


def test_parse_caip10_solana() -> None:
    parsed = parse_caip10(OWNER)
    assert parsed["namespace"] == "solana"
    assert parsed["address"] == OWNER.rsplit(":", 1)[1]


def test_parse_caip10_rejects_garbage() -> None:
    from miriam_agent.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        parse_caip10("not-an-account")


def test_parse_caip19_solana_spl() -> None:
    parsed = parse_caip19(
        "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/spl:XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp"
    )
    assert parsed["asset_reference"].endswith(
        "XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp"
    )


def test_deposit_address_uses_caip_parse() -> None:
    assert deposit_address_from_caip(OWNER) == OWNER.rsplit(":", 1)[1]


# -- invest parse --------------------------------------------------------------


def test_parse_invest_sentence() -> None:
    action = parse_invest_utterance("put 30 of this deposit into stocks")
    assert action is not None
    assert action.type == "invest"
    assert action.amount == Decimal("30")
    assert action.sleeve == INVEST_SLEEVE == "savings"
    assert action.source == "user"


def test_single_name_is_not_the_sleeve() -> None:
    assert parse_invest_utterance("buy NVDA for 50") is None
    assert parse_invest_utterance("put 30 into TSLA") is None


def test_invest_without_amount_is_no_action() -> None:
    assert parse_invest_utterance("tell me about stocks") is None


def test_action_type_allows_invest() -> None:
    assert _action_type("invest") == "invest"
    assert _action_type("buy_asset") == "none"


# -- prepare (tap) ---------------------------------------------------------------


async def test_prepare_happy_path_queues_card() -> None:
    store = InMemoryLedgerStore()
    ledger = funded_ledger()
    await store.save(ledger)
    calls = make_calls()
    receipt, card, _ = await prepare_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        token="t",
        amount=money(30),
        decision_id="dec_1",
        list_strategies=calls["list_strategies"],
        get_owner=calls["get_owner"],
        prepare_call=calls["prepare_call"],
    )
    assert receipt.status == "queued"
    assert card is not None
    assert card["kind"] == "allocate"
    assert card["flow_id"] == "flow_test123"
    assert card["source"] == "stash"
    assert "AAPLx" in card["primary"]
    assert ledger.pending_invest["flow_test123"].amount == "30.00"


async def test_prepare_missing_sleeve_fails_closed() -> None:
    store = InMemoryLedgerStore()
    ledger = funded_ledger()
    await store.save(ledger)
    calls = make_calls(strategies=[{"id": "x", "name": "Something Else"}])
    receipt, card, _ = await prepare_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        token="t",
        amount=money(30),
        decision_id="dec_1",
        list_strategies=calls["list_strategies"],
        get_owner=calls["get_owner"],
        prepare_call=calls["prepare_call"],
    )
    assert receipt.status == "rejected"
    assert card is None
    assert receipt.reasons == ["SLEEVE_MISSING"]


async def test_prepare_simulated_backend_issues_no_payload() -> None:
    store = InMemoryLedgerStore()
    ledger = funded_ledger()
    await store.save(ledger)
    calls = make_calls()
    real_prepare = calls["prepare_call"]

    async def sim_prepare(payload: dict[str, Any]) -> dict[str, Any]:
        out = await real_prepare(payload)
        if out.get("status") != "AWAITING_CONFIRMATION":
            return {"status": "REJECTED", "simulated": True, "live": False}
        return out

    calls["prepare_call"] = sim_prepare
    receipt, card, _ = await prepare_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        token="t",
        amount=money(30),
        decision_id="dec_1",
        list_strategies=calls["list_strategies"],
        get_owner=calls["get_owner"],
        prepare_call=calls["prepare_call"],
    )
    assert receipt.status == "rejected"
    assert card is None
    assert "GLIDER_NOT_LIVE" in receipt.reasons


async def test_prepare_refuses_over_balance() -> None:
    store = InMemoryLedgerStore()
    ledger = ledger_with("u1", savings=10)
    await store.save(ledger)
    calls = make_calls()
    receipt, card, _ = await prepare_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        token="t",
        amount=money(30),
        decision_id="dec_1",
        list_strategies=calls["list_strategies"],
        get_owner=calls["get_owner"],
        prepare_call=calls["prepare_call"],
    )
    assert receipt.status == "rejected"
    assert card is None
    assert receipt.reasons == ["OVER_BALANCE"]


# -- settle (wallet signature) ----------------------------------------------------


async def _prepared(store: InMemoryLedgerStore, ledger: Ledger) -> str:
    await store.save(ledger)
    calls = make_calls()
    _, card, _ = await prepare_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        token="t",
        amount=money(30),
        decision_id="dec_1",
        list_strategies=calls["list_strategies"],
        get_owner=calls["get_owner"],
        prepare_call=calls["prepare_call"],
    )
    assert card is not None
    return str(card["flow_id"])


async def test_settle_happy_path_debits_savings() -> None:
    store = InMemoryLedgerStore()
    ledger = funded_ledger()
    flow = await _prepared(store, ledger)
    calls = make_calls()
    receipt, result, ledger = await settle_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        flow_id=flow,
        signed_tx="AgAB user-signed",
        complete_call=calls["complete_call"],
    )
    assert receipt.status == "executed"
    assert result["ok"] is True
    assert ledger.balance("savings") == money(70)
    assert ledger.pending_invest[flow].status == "enrolled"


async def test_settle_unknown_flow_rejects() -> None:
    store = InMemoryLedgerStore()
    ledger = funded_ledger()
    await store.save(ledger)
    calls = make_calls()
    receipt, result, _ = await settle_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        flow_id="flow_nope",
        signed_tx="AgAB x",
        complete_call=calls["complete_call"],
    )
    assert receipt.status == "rejected"
    assert result["reasons"] == ["NO_SUCH_FLOW"]
    assert ledger.balance("savings") == money(100)


async def test_settle_is_idempotent_on_flow() -> None:
    store = InMemoryLedgerStore()
    ledger = funded_ledger()
    flow = await _prepared(store, ledger)
    calls = make_calls()
    first, _, ledger = await settle_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        flow_id=flow,
        signed_tx="AgAB user-signed",
        complete_call=calls["complete_call"],
    )
    assert first.status == "executed"
    second, result, ledger = await settle_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        flow_id=flow,
        signed_tx="AgAB user-signed",
        complete_call=calls["complete_call"],
    )
    assert second.idempotent_replay is True
    assert ledger.balance("savings") == money(70)


# -- orchestrator loop --------------------------------------------------------------


def _orchestrator() -> Orchestrator:
    return Orchestrator(
        store=InMemoryLedgerStore(),
        policy=Policy(
            max_auto=money(2000),
            max_with_confirm=money(100000),
            reversible_under=money(2000),
        ),
        judge=judge_of(jev(intent="order", mode="ask", action="allow")),
    )


async def test_utterance_stages_invest_challenge_never_executes() -> None:
    orch = _orchestrator()
    ledger = funded_ledger()
    await orch.store.save(ledger)
    result = await orch.handle_utterance("u1", "put 30 of this deposit into stocks")
    assert result.confirm_id
    assert result.receipt is None  # nothing moved from an utterance
    assert result.card is None


async def test_tap_returns_sign_card() -> None:
    orch = _orchestrator()
    orch.go_token = None  # fail closed without a host token
    ledger = funded_ledger()
    await orch.store.save(ledger)
    uttered = await orch.handle_utterance("u1", "put 30 of this deposit into stocks")
    tapped = await orch.handle_confirm("u1", uttered.confirm_id, True)
    assert tapped.receipt is not None
    assert tapped.receipt.status == "rejected"
    assert tapped.receipt.reasons == ["GLIDER_UNREACHABLE"]


# -- charts --------------------------------------------------------------------------


def test_donut_renders_and_empty_returns_none() -> None:
    raw = sleeve_donut(
        [
            {"symbol": "AAPLx", "value_usd": "40"},
            {"symbol": "NVDAx", "value_usd": "30"},
            {"symbol": "TSLAx", "value_usd": "30"},
        ]
    )
    assert raw is not None and len(raw) > 20000
    assert sleeve_donut([]) is None
    assert sleeve_donut([{"symbol": "AAPLx", "value_usd": "0"}]) is None


def test_split_bar_renders_and_zeros_return_none() -> None:
    assert split_bar(70, 30) is not None
    assert split_bar(0, 0) is None


# -- cut list ----------------------------------------------------------


def test_no_alpaca_or_direct_glider_on_the_path() -> None:
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "miriam_agent"
    hits: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text()
        low = text.lower()
        if "alpaca" in low:
            hits.append(f"{path}: Alpaca")
        if "glider_client" in text:
            hits.append(f"{path}: direct glider_client")
    assert hits == [], hits


def test_no_glider_api_key_setting() -> None:
    from miriam_agent.config.settings import Settings

    assert not hasattr(Settings.model_fields, "GLIDER_API_KEY")
    assert "GLIDER_API_KEY" not in Settings().model_dump()


def test_sleeve_reads_registered_and_writes_are_not() -> None:
    from miriam_agent.tools import build_tool_registry

    registry = build_tool_registry()
    names = set(registry.list_names())
    assert {
        "glider_get_strategy",
        "glider_get_portfolio",
        "glider_get_positions",
    } <= names
    assert "glider_prepare_enroll" not in names
    assert "glider_complete_enroll" not in names

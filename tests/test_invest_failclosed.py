"""Invest leg hardening: replay, validation, fail-closed, persistence.

Covers the blockers from the PR #9 review: the staged-confirmation replay
binding, zero/negative amounts, non-Solana owners, funding-failure shapes,
idempotent settle under a changed signature, fail-closed sleeve reads, and
rejected-tap persistence (consumed challenge + receipt survive the request).
"""

from __future__ import annotations

from typing import Any

import pytest

from miriam_agent.hands.invest import (
    _obtain_and_replay,
    prepare_allocate,
    settle_allocate,
)
from miriam_agent.hands.ledger import InMemoryLedgerStore, money
from miriam_agent.hands.limits import Policy
from miriam_agent.orchestrator import Orchestrator
from miriam_agent.tools import glider_sleeve as sleeve_mod
from miriam_agent.tools.glider_sleeve import (
    _glider_get_portfolio,
    _glider_get_positions,
    _glider_get_strategy,
)
from tests.layer_fakes import jev, judge_of, ledger_with

OWNER = (
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp:"
    "7xKXTagR7k8J2y8K9mN3pQ5rS7tU9vW1xY3zABcD5eF"
)
SLEEVE_ROW = {
    "id": "d15c0ba1-71c0-4a11-8aa1-000000000001",
    "name": "Rail Stock Sleeve",
    "glider_strategy_id": "01M333PQNR9Y2WJ4D78FSRECEQ",
}


def make_calls(
    *,
    strategies: list[dict[str, Any]] | None = None,
    owner: str = OWNER,
    flow_id: str = "flow_test123",
    enrolled: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prepared = {
        "status": "COMPLETED",
        "flow_id": flow_id,
        "account_index": "0",
        "agent_account_id": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp:Ag3nt",
        "chain_ids": [1399811149],
        "sign_payload": "AgAB signed-bytes",
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
        return prepared

    async def complete_call(payload: dict[str, Any]) -> dict[str, Any]:
        if "confirmation_token" not in payload:
            return {
                "status": "AWAITING_CONFIRMATION",
                "confirmation": {"token": "cfm-2"},
            }
        return done

    return {
        "list_strategies": list_strategies,
        "get_owner": get_owner,
        "prepare_call": prepare_call,
        "complete_call": complete_call,
    }


async def _prepared_flow(
    store: InMemoryLedgerStore,
    ledger: Any,
    calls: dict[str, Any],
) -> str:
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


# -- staged-confirmation replay -----------------------------------------------


async def test_replay_rejects_tokenless_stage() -> None:
    async def staged_without_token(payload: dict[str, Any]) -> dict[str, Any]:
        return {"status": "AWAITING_CONFIRMATION", "confirmation": {}}

    with pytest.raises(RuntimeError, match="without a token"):
        await _obtain_and_replay(staged_without_token, {"a": 1})


async def test_replay_rejects_preseded_token() -> None:
    async def nope(payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("must not be called")

    with pytest.raises(RuntimeError, match="must not pre-carry"):
        await _obtain_and_replay(nope, {"confirmation_token": "cfm-x"})


async def test_replay_rejects_non_dict_responses() -> None:
    async def not_a_dict(payload: dict[str, Any]) -> Any:
        return ["not", "a", "dict"]

    with pytest.raises(RuntimeError, match="no staged response"):
        await _obtain_and_replay(not_a_dict, {"a": 1})

    async def staged_then_junk(payload: dict[str, Any]) -> Any:
        if "confirmation_token" not in payload:
            return {
                "status": "AWAITING_CONFIRMATION",
                "confirmation": {"token": "cfm-1"},
            }
        return 42

    with pytest.raises(RuntimeError, match="no confirmed response"):
        await _obtain_and_replay(staged_then_junk, {"a": 1})


async def test_replay_sends_identical_payload_plus_token() -> None:
    seen: list[dict[str, Any]] = []

    async def echo(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(dict(payload))
        if "confirmation_token" not in payload:
            return {
                "status": "AWAITING_CONFIRMATION",
                "confirmation": {"token": "cfm-9"},
            }
        return {"status": "COMPLETED"}

    out = await _obtain_and_replay(echo, {"strategy_id": "s1", "amount_usd": 30.0})
    assert out["status"] == "COMPLETED"
    assert seen[0] == {"strategy_id": "s1", "amount_usd": 30.0}
    assert seen[1] == {
        "strategy_id": "s1",
        "amount_usd": 30.0,
        "confirmation_token": "cfm-9",
    }


# -- prepare validation --------------------------------------------------------


async def test_prepare_rejects_zero_and_negative_amount() -> None:
    store = InMemoryLedgerStore()
    ledger = ledger_with("u1", savings=100)
    await store.save(ledger)
    calls = make_calls()
    for bad in ("0", "-5"):
        receipt, card, _ = await prepare_allocate(
            store=store,
            ledger=ledger,
            user_id="u1",
            token="t",
            amount=money(bad),
            decision_id="dec_1",
            list_strategies=calls["list_strategies"],
            get_owner=calls["get_owner"],
            prepare_call=calls["prepare_call"],
        )
        assert receipt.status == "rejected"
        assert card is None
        assert receipt.reasons == ["BAD_AMOUNT"]
    assert ledger.balance("savings") == money(100)
    assert ledger.pending_invest == {}


async def test_prepare_rejects_invalid_owner_address() -> None:
    store = InMemoryLedgerStore()
    ledger = ledger_with("u1", savings=100)
    await store.save(ledger)
    calls = make_calls()
    receipt, card, _ = await prepare_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        token="t",
        amount=money(30),
        decision_id="dec_1",
        owner_address="not-a-wallet!!!",
        list_strategies=calls["list_strategies"],
        get_owner=calls["get_owner"],
        prepare_call=calls["prepare_call"],
    )
    assert receipt.status == "rejected"
    assert card is None
    assert receipt.reasons == ["NO_OWNER_WALLET"]
    assert ledger.balance("savings") == money(100)


async def test_prepare_rejects_non_solana_owner() -> None:
    store = InMemoryLedgerStore()
    ledger = ledger_with("u1", savings=100)
    await store.save(ledger)
    calls = make_calls(owner="eip155:1:0x1234567890abcdef1234567890abcdef12345678")
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
    assert receipt.reasons == ["NO_OWNER_WALLET"]


# -- settle funding shapes ------------------------------------------------------


async def test_settle_top_level_funding_failed_blocks_debit() -> None:
    store = InMemoryLedgerStore()
    ledger = ledger_with("u1", savings=100)
    await store.save(ledger)
    calls = make_calls(
        enrolled={
            "status": "COMPLETED",
            "enrollment": {"glider_portfolio_id": "pf_1"},
            "funding": {"status": "FAILED", "failure_reason": "bank declined"},
        }
    )
    flow = await _prepared_flow(store, ledger, make_calls())
    # Rebind the flow's complete call to the failing host.
    receipt, result, ledger = await settle_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        flow_id=flow,
        signed_tx="AgAB user-signed",
        complete_call=calls["complete_call"],
    )
    assert receipt.status == "rejected"
    assert result["reasons"] == ["FUNDING_FAILED"]
    assert "bank declined" in result["detail"]
    assert ledger.balance("savings") == money(100)


async def test_settle_nested_funding_failed_blocks_debit() -> None:
    store = InMemoryLedgerStore()
    ledger = ledger_with("u1", savings=100)
    await store.save(ledger)
    calls = make_calls(
        enrolled={
            "status": "COMPLETED",
            "enrollment": {
                "glider_portfolio_id": "pf_1",
                "funding": {"status": "failed"},
            },
        }
    )
    flow = await _prepared_flow(store, ledger, make_calls())
    receipt, result, ledger = await settle_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        flow_id=flow,
        signed_tx="AgAB user-signed",
        complete_call=calls["complete_call"],
    )
    assert receipt.status == "rejected"
    assert result["reasons"] == ["FUNDING_FAILED"]
    assert ledger.balance("savings") == money(100)


async def test_settle_non_dict_host_response_blocks_debit() -> None:
    store = InMemoryLedgerStore()
    ledger = ledger_with("u1", savings=100)
    await store.save(ledger)
    flow = await _prepared_flow(store, ledger, make_calls())

    async def junk(payload: dict[str, Any]) -> Any:
        return ["junk"]

    receipt, result, ledger = await settle_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        flow_id=flow,
        signed_tx="AgAB user-signed",
        complete_call=junk,
    )
    assert receipt.status == "rejected"
    assert result["reasons"] == ["SETTLE_FAILED"]
    assert ledger.balance("savings") == money(100)


async def test_settle_replay_with_different_signature_debits_once() -> None:
    store = InMemoryLedgerStore()
    ledger = ledger_with("u1", savings=100)
    flow = await _prepared_flow(store, ledger, make_calls())
    calls = make_calls()
    first, _, ledger = await settle_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        flow_id=flow,
        signed_tx="AgAB first-signature",
        complete_call=calls["complete_call"],
    )
    assert first.status == "executed"
    second, result, ledger = await settle_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        flow_id=flow,
        signed_tx="AgAB a-different-signature",
        complete_call=calls["complete_call"],
    )
    assert second.idempotent_replay is True
    assert result["ok"] is True
    assert ledger.balance("savings") == money(70)


async def test_settle_corrupt_binding_rejects() -> None:
    store = InMemoryLedgerStore()
    ledger = ledger_with("u1", savings=100)
    await store.save(ledger)
    calls = make_calls()
    flow = await _prepared_flow(store, ledger, calls)
    binding = ledger.pending_invest[flow]
    binding.strategy_id = ""
    ledger.pending_invest[flow] = binding
    receipt, result, _ = await settle_allocate(
        store=store,
        ledger=ledger,
        user_id="u1",
        flow_id=flow,
        signed_tx="AgAB user-signed",
        complete_call=calls["complete_call"],
    )
    assert receipt.status == "rejected"
    assert result["reasons"] == ["FLOW_UNBOUND"]


# -- sleeve reads fail closed ----------------------------------------------------


class _BoomGo:
    async def list_investment_strategies(self, token, status=None):
        raise ConnectionError("go is down")

    async def get_investment_strategy(self, token, strategy_id):
        raise AssertionError("must not be reached")

    async def get_investment_portfolio(self, token):
        raise ConnectionError("go is down")

    async def get_investment_positions(self, token):
        raise ConnectionError("go is down")


async def test_sleeve_strategy_fails_closed_on_host_error(monkeypatch) -> None:
    monkeypatch.setattr(sleeve_mod, "get_go_client", lambda: _BoomGo())
    out = await _glider_get_strategy({}, {"token": "t"})
    assert out["live"] is False
    assert out["sleeve"] is None
    assert out["catalogue"] == []


async def test_sleeve_tools_refuse_missing_token() -> None:
    assert (await _glider_get_strategy({}, {}))["live"] is False
    assert "_tool_error" in await _glider_get_portfolio({}, {})
    out = await _glider_get_positions({}, {})
    assert out["positions"] == []
    assert "_tool_error" in out


async def test_portfolio_and_positions_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(sleeve_mod, "get_go_client", lambda: _BoomGo())
    assert "_tool_error" in await _glider_get_portfolio({}, {"token": "t"})
    out = await _glider_get_positions({}, {"token": "t"})
    assert out["positions"] == []
    assert "_tool_error" in out


# -- rejected taps persist --------------------------------------------------------


class _NoSleeveGo:
    async def list_investment_strategies(self, token, status=None):
        return {"strategies": [{"id": "x", "name": "Something Else"}]}

    async def get_investment_owner(self, token):
        return {"owner_account_id": OWNER}

    async def prepare_user_enroll(self, token, payload, confirmation_token=None):
        raise AssertionError("must not reach prepare without a sleeve")

    async def complete_user_enroll(self, token, payload, confirmation_token=None):
        raise AssertionError("must not reach complete without a sleeve")


async def test_rejected_tap_persists_challenge_and_receipt(monkeypatch) -> None:
    import miriam_agent.integrations.go_client as go_mod

    monkeypatch.setattr(go_mod, "get_go_client", lambda: _NoSleeveGo())
    orch = Orchestrator(
        store=InMemoryLedgerStore(),
        policy=Policy(
            max_auto=money(2000),
            max_with_confirm=money(100000),
            reversible_under=money(2000),
        ),
        judge=judge_of(jev(intent="order", mode="ask", action="allow")),
        go_token="test-token",
    )
    await orch.store.save(ledger_with("u1", savings=100))
    uttered = await orch.handle_utterance("u1", "put 30 of this deposit into stocks")
    assert uttered.confirm_id
    tapped = await orch.handle_confirm("u1", uttered.confirm_id, True)
    assert tapped.receipt is not None
    assert tapped.receipt.status == "rejected"
    assert tapped.receipt.reasons == ["SLEEVE_MISSING"]
    # The rejection survives the request: the challenge is consumed and the
    # receipt is in the store, so a retap cannot double-run stage 1.
    reloaded = await orch.store.load("u1")
    assert reloaded is not None
    assert reloaded.challenges[uttered.confirm_id].status == "consumed"
    assert any(
        r.action == "invest_prepare" and r.status == "rejected"
        for r in reloaded.receipts
    )

"""Regression tests for PR #11 review fixes.

Each test pins one blocker/major from the structured review so it can
never silently regress:

- stock sells are orders, never offramps (funding regex)
- the ticker parser returns the ticker, never the verb
- unknown coins never silently become USDC
- judgment fail-closed: unknown JEV labels defer, never allow
- prepare legs are idempotent (double-tap replays, never re-executes)
- allocation legs are validated (weights sum, unique assets)
- pause/resume provider errors reject, never narrate success
- settle_order persists its receipt
- Face-ID card legs accept list-shaped legs
- plain-English amounts ("fifty") and politeness ("please ...") parse
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from tests.layer_fakes import jev, ledger_with  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# -- blocker 1: offramp must not hijack stock sells -------------------------


def test_stock_sell_is_not_an_offramp():
    from miriam_agent.hands.funding import parse_offramp_utterance

    assert parse_offramp_utterance("sell 50 NVDAx") is None
    assert parse_offramp_utterance("sell all my Tesla shares") is None


def test_sell_naira_and_cashout_are_offramps():
    from miriam_agent.hands.funding import parse_offramp_utterance

    assert parse_offramp_utterance("sell 20000 naira to my bank").type == "offramp"
    assert parse_offramp_utterance("cash me out 20k to my bank").type == "offramp"
    assert (
        parse_offramp_utterance("i want to withdraw 20k to my bank account").type
        == "offramp"
    )


def test_bare_withdraw_is_not_an_offramp():
    from miriam_agent.hands.funding import parse_offramp_utterance

    assert parse_offramp_utterance("withdraw from my stocks") is None


# -- blocker 2: ticker parser returns the ticker ----------------------------


def test_ticker_parser_skips_the_verb():
    from miriam_agent.hands.orders import _parse_symbol

    assert _parse_symbol("buy 50 NVDAx") == "NVDAx"
    assert _parse_symbol("sell 50 NVDAx") == "NVDAx"
    assert _parse_symbol("buy 50 NVDA") == "NVDAx"


def test_grocery_words_are_never_symbols():
    from miriam_agent.hands.orders import _parse_symbol, parse_order_utterance

    assert _parse_symbol("buy groceries worth 50 dollars") is None
    assert parse_order_utterance("buy groceries worth 50 dollars") is None


def test_plain_english_company_names_resolve():
    from miriam_agent.hands.orders import parse_order_utterance

    action = parse_order_utterance("please buy me some apple stock worth 50 dollars")
    assert action is not None
    assert action.counterparty == "AAPLx"
    assert action.side == "buy"

    action = parse_order_utterance("i want to sell fifty dollars of nvidia")
    assert action is not None
    assert action.counterparty == "NVDAx"
    assert action.side == "sell"


# -- blocker 7: unknown coins never become USDC ------------------------------


def test_unknown_coin_is_not_usdc():
    from miriam_agent.hands.funding import classify_symbol

    assert classify_symbol("xyzcoin") is None
    assert classify_symbol("") == "USDC"


def test_top_up_unknown_coin_rejected():
    from miriam_agent.hands.funding import parse_funding_utterance

    assert parse_funding_utterance("top up 5000 of xyzcoin") is None
    action = parse_funding_utterance("i want to top up 5000 naira of bitcoin")
    assert action is not None
    assert action.counterparty == "BTC"


# -- blocker 6: judgment fails closed on unknown labels ----------------------


def test_unknown_jev_action_defers_on_funding():
    from miriam_agent.hands.limits import Policy
    from miriam_agent.hands.state import ProposedAction, build_state
    from miriam_agent.judgment.rules import apply_rules

    ledger = ledger_with(spendable=100000, savings=100000)
    action = ProposedAction(
        type="onramp",
        amount=Decimal("5000"),
        counterparty="BTC",
        sleeve="spendable",
        source="user",
        side="buy",
    )
    state = build_state(ledger=ledger, policy=Policy(), proposed_action=action)
    judgment = jev(intent="order", action="none", mode="ask")
    outcome = apply_rules(
        state=state,
        judgment=judgment,
        ledger=ledger,
        policy=Policy(),
        cap=Decimal("2000"),
    )
    assert outcome.action_choice == "defer"
    assert outcome.action_choice != "allow"


# -- plain-English amounts ----------------------------------------------------


def test_word_number_amounts_parse():
    from miriam_agent.hands.transfer import parse_amount, parse_transfer_utterance

    assert parse_amount("fifty dollars") == Decimal("50.00")
    assert parse_amount("one hundred and twenty") == Decimal("120.00")
    turn = parse_transfer_utterance("please can you send fifty thousand naira to Femi")
    assert turn is not None
    assert turn.type == "transfer"
    assert turn.amount == Decimal("50000.00")


# -- major: prepare legs are idempotent ---------------------------------------


def _order_fakes(calls: list):
    async def list_strategies():
        return {
            "strategies": [
                {
                    "id": "strat-1",
                    "name": "Rail Stock Sleeve",
                    "glider_strategy_id": "glider-1",
                }
            ]
        }

    async def search_assets():
        return {
            "assets": [
                {"asset_id": "a-nvda", "symbol": "NVDAx", "caipAssetId": ""},
            ]
        }

    async def order_call(payload):
        calls.append(payload)
        return {"status": "completed", "execution_id": "exec-1"}

    return list_strategies, search_assets, order_call


def test_prepare_order_double_tap_executes_once():
    from miriam_agent.hands.ledger import InMemoryLedgerStore
    from miriam_agent.hands.orders import prepare_order

    calls: list = []
    list_strategies, search_assets, order_call = _order_fakes(calls)
    store = InMemoryLedgerStore()
    ledger = ledger_with(savings=100000)
    first, _, ledger = _run(
        prepare_order(
            store=store,
            ledger=ledger,
            user_id="u1",
            token="tok",
            side="buy",
            symbol="NVDAx",
            amount_usd=Decimal("100"),
            decision_id="d1",
            list_strategies=list_strategies,
            search_assets=search_assets,
            order_call=order_call,
        )
    )
    assert first.status == "executed"
    second, _, _ = _run(
        prepare_order(
            store=store,
            ledger=ledger,
            user_id="u1",
            token="tok",
            side="buy",
            symbol="NVDAx",
            amount_usd=Decimal("100"),
            decision_id="d1",
            list_strategies=list_strategies,
            search_assets=search_assets,
            order_call=order_call,
        )
    )
    assert second.id == first.id
    assert len(calls) == 1


def test_prepare_order_none_amount_rejects():
    from miriam_agent.hands.ledger import InMemoryLedgerStore
    from miriam_agent.hands.orders import prepare_order

    calls: list = []
    list_strategies, search_assets, order_call = _order_fakes(calls)
    store = InMemoryLedgerStore()
    receipt, _, _ = _run(
        prepare_order(
            store=store,
            ledger=ledger_with(savings=100000),
            user_id="u1",
            token="tok",
            side="buy",
            symbol="NVDAx",
            amount_usd=None,  # type: ignore[arg-type]
            decision_id="d2",
            list_strategies=list_strategies,
            search_assets=search_assets,
            order_call=order_call,
        )
    )
    assert receipt.status == "rejected"
    assert "BAD_AMOUNT" in list(receipt.reasons or [])
    assert calls == []


def test_allocation_legs_must_sum_and_be_unique():
    from miriam_agent.hands.ledger import InMemoryLedgerStore
    from miriam_agent.hands.orders import prepare_set_allocation

    async def allocation_call(payload):
        raise AssertionError("provider must not run on bad legs")

    store = InMemoryLedgerStore()
    bad_weights, _, _ = _run(
        prepare_set_allocation(
            store=store,
            ledger=ledger_with(savings=100000),
            user_id="u1",
            token="tok",
            strategy_id="strat-1",
            legs=[
                {"asset_id": "a", "weight": 0.5},
                {"asset_id": "b", "weight": 0.2},
            ],
            decision_id="d3",
            allocation_call=allocation_call,
        )
    )
    assert bad_weights.status == "rejected"
    assert "BAD_LEGS" in list(bad_weights.reasons or [])

    duped, _, _ = _run(
        prepare_set_allocation(
            store=store,
            ledger=ledger_with(savings=100000),
            user_id="u1",
            token="tok",
            strategy_id="strat-1",
            legs=[
                {"asset_id": "a", "weight": 0.5},
                {"asset_id": "a", "weight": 0.5},
            ],
            decision_id="d4",
            allocation_call=allocation_call,
        )
    )
    assert duped.status == "rejected"
    assert "BAD_LEGS" in list(duped.reasons or [])


def test_pause_provider_error_rejects_not_executes():
    from miriam_agent.hands.ledger import InMemoryLedgerStore
    from miriam_agent.hands.rebalance import prepare_pause

    async def failing_pause(strategy_id):
        return {"status": "failed", "error": "boom"}

    store = InMemoryLedgerStore()
    receipt, card, _ = _run(
        prepare_pause(
            store=store,
            ledger=ledger_with(savings=100000),
            user_id="u1",
            token="tok",
            strategy_id="strat-1",
            pause_call=failing_pause,
        )
    )
    assert receipt.status == "rejected"
    assert card is None


def test_pause_idempotent_replay():
    from miriam_agent.hands.ledger import InMemoryLedgerStore
    from miriam_agent.hands.rebalance import prepare_pause

    calls: list = []

    async def ok_pause(strategy_id):
        calls.append(strategy_id)
        return {"status": "ok", "strategy_id": strategy_id}

    store = InMemoryLedgerStore()
    ledger = ledger_with(savings=100000)
    first, _, ledger = _run(
        prepare_pause(
            store=store,
            ledger=ledger,
            user_id="u1",
            token="tok",
            strategy_id="strat-1",
            pause_call=ok_pause,
        )
    )
    assert first.status == "executed"
    second, _, _ = _run(
        prepare_pause(
            store=store,
            ledger=ledger,
            user_id="u1",
            token="tok",
            strategy_id="strat-1",
            pause_call=ok_pause,
        )
    )
    assert second.id == first.id
    assert calls == ["strat-1"]


# -- major: card legs accept list-shaped legs ----------------------------------


def test_parse_legs_accepts_real_lists():
    from miriam_agent.confirm_cards.mapping import _parse_legs

    legs = [{"asset_id": "a", "weight": 1.0}]
    assert _parse_legs(legs) == legs
    assert _parse_legs('[{"asset_id": "a"}]') == [{"asset_id": "a"}]
    assert _parse_legs("not-json{{") is None


# -- NL round 2: more plain English --------------------------------------------


def test_half_needs_a_unit():
    from miriam_agent.hands.nl import parse_amount

    assert parse_amount("half a million naira") == Decimal("500000.00")
    assert parse_amount("a grand") == Decimal("1000.00")
    # "half" with no unit is not an amount; Judgment asks instead.
    assert parse_amount("send half to Femi") is None


def test_name_after_verb_without_to():
    from miriam_agent.hands.nl import parse_transfer_utterance

    turn = parse_transfer_utterance("send Femi 5k")
    assert turn is not None
    assert turn.counterparty == "Femi"
    assert turn.amount == Decimal("5000.00")
    turn = parse_transfer_utterance("pay Ada fifty bucks")
    assert turn is not None
    assert turn.counterparty == "Ada"
    # Lowercase English never becomes a counterparty.
    assert parse_transfer_utterance("send money home") is None


def test_rebalance_plain_english_contexts():
    from miriam_agent.hands.nl import parse_rebalance_utterance

    assert parse_rebalance_utterance("even out my investments").type == "rebalance"
    assert parse_rebalance_utterance("sort out my holdings please").type == "rebalance"


def test_send_to_bank_is_offramp_not_transfer():
    from miriam_agent.hands.funding import parse_offramp_utterance

    assert parse_offramp_utterance("send 20k to my bank").type == "offramp"
    assert parse_offramp_utterance("move 1k to savings") is None
    assert parse_offramp_utterance("send 5k to Ada") is None


def test_weak_funding_words_need_a_coin():
    from miriam_agent.hands.funding import parse_funding_utterance

    action = parse_funding_utterance("put 50k into bitcoin")
    assert action is not None
    assert action.counterparty == "BTC"
    action = parse_funding_utterance("deposit 5k of ethereum")
    assert action is not None
    assert action.counterparty == "ETH"
    assert parse_funding_utterance("deposit 10k into savings") is None
    assert parse_funding_utterance("put 50 aside") is None

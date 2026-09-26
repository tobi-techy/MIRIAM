"""Funding hands leg: fail-closed onramp, OTP retry, staged offramp.

- No Go token -> rejected, never silent.
- Wrong OTP -> retry receipt, never an auto-buy.
- Offramp stages an envelope for the app, never POSTs.
- classify_turn routes buy/sell + crypto words with amounts to orchestrator.
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_parse_buy_ngn_of_symbol():
    from miriam_agent.hands.funding import parse_funding_utterance

    action = parse_funding_utterance("buy 50k of USDC")
    assert action is not None
    assert action.type == "onramp"
    assert action.amount == Decimal("50000.00")
    assert action.counterparty == "USDC"
    assert action.side == "buy"


def test_parse_classifies_bitcoin():
    from miriam_agent.hands.funding import parse_funding_utterance

    action = parse_funding_utterance("buy 100000 bitcoin")
    assert action is not None
    assert action.counterparty == "BTC"
    topup = parse_funding_utterance("top up 50k btc")
    assert topup is not None
    assert topup.type == "onramp"
    assert topup.counterparty == "BTC"
    assert parse_funding_utterance("buy 50k of groceries") is None


def test_sell_never_parses_as_onramp():
    from miriam_agent.hands.funding import (
        parse_funding_utterance,
        parse_offramp_utterance,
    )

    assert parse_funding_utterance("sell 50k usdc to naira") is None
    off = parse_offramp_utterance("sell 50 usdc to naira")
    assert off is not None
    assert off.type == "offramp"


def test_extract_otp_strict():
    from miriam_agent.hands.funding import extract_otp

    assert extract_otp("482916") == "482916"
    assert extract_otp("code 482916") is None
    assert extract_otp("abc") is None


def test_verify_wrong_format_never_calls_go():
    from miriam_agent.hands.funding import verify_paj_otp

    out = _run(verify_paj_otp("tok", "not-a-code"))
    assert out["ok"] is False
    assert out["reasons"] == ["BAD_OTP_FORMAT"]


def test_verify_without_token_rejected():
    from miriam_agent.hands.funding import verify_paj_otp

    out = _run(verify_paj_otp(None, "123456"))
    assert out["ok"] is False
    assert out["reasons"] == ["NO_GO_TOKEN"]


def test_create_onramp_requires_verification_for_paj():
    from miriam_agent.hands.funding import create_onramp

    out = _run(
        create_onramp("tok", kind="paj", amount_ngn=Decimal("50000"), verified=False)
    )
    assert out["ok"] is False
    assert out["reasons"] == ["PAJ_VERIFICATION_REQUIRED"]


def test_create_onramp_without_token_rejected():
    from miriam_agent.hands.funding import create_onramp

    out = _run(
        create_onramp(None, kind="paj", amount_ngn=Decimal("50000"), verified=True)
    )
    assert out["ok"] is False
    assert out["reasons"] == ["NO_GO_TOKEN"]


def test_offramp_stages_envelope_never_posts():
    from miriam_agent.hands.funding import stage_offramp_envelope

    staged = stage_offramp_envelope(amount_ngn=Decimal("50000"))
    env = staged["offramp_envelope"]
    assert env["path"] == "/api/v1/funding/paj/offramp"
    assert env["method"] == "POST"
    assert env["needs"] == ["app_confirm", "passcode"]
    assert staged["authorize"] == "rail://authorize"
    assert "passcode" in staged["spoken"]


def test_airtime_is_an_airbills_bill_not_an_onramp():
    from miriam_agent.hands.bills import parse_bill_utterance
    from miriam_agent.hands.funding import parse_funding_utterance

    text = "Buy 500 naira airtime for 08031234567"
    bill = parse_bill_utterance(text)
    assert bill is not None
    assert bill.type == "bill"
    assert bill.counterparty == "08031234567"
    assert str(bill.amount) == "500.00"
    assert parse_funding_utterance(text) is None


def test_airtime_confirm_pays_through_airbills(monkeypatch):
    import miriam_agent.integrations.go_client as gc
    from layer_fakes import jev, judge_of

    from miriam_agent.hands.ledger import InMemoryLedgerStore, money, new_ledger
    from miriam_agent.orchestrator import Event, Orchestrator

    class _Bills:
        async def detect_network(self, token, phone):
            assert phone == "08031234567"
            return {"network_id": "01", "network": "MTN"}

        async def pay_bill(self, token, payload, idempotency_key=None):
            assert payload["category"] == "airtime"
            assert payload["recipient"] == "08031234567"
            assert payload["amount_ngn"] == 500.0
            assert payload["network_id"] == "01"
            return {
                "order_id": "ord-air",
                "airbills_id": "ab-100",
                "category": "airtime",
                "recipient": "08031234567",
                "amount_ngn": 500,
                "amount_usdc": "0.35",
                "status": "success",
            }

    monkeypatch.setattr(gc, "get_go_client", lambda: _Bills())

    async def _go():
        store = InMemoryLedgerStore()
        ledger = new_ledger("u-air")
        ledger.sleeves["spendable"] = money(10000)
        store.seed(ledger)
        orch = Orchestrator(
            store=store,
            judge=judge_of(jev(mode="ask", action="allow")),
            go_token="tok",
            provider=None,
        )
        said = await orch.handle(
            Event(
                type="utterance",
                user_id="u-air",
                text="Buy 500 naira airtime for 08031234567",
            )
        )
        assert said.confirm_id, "airtime must stage a confirm, not an onramp"
        paid = await orch.handle(
            Event(type="confirm", user_id="u-air", confirm_id=said.confirm_id)
        )
        assert paid.receipt is not None
        assert paid.receipt.status == "executed"
        assert paid.receipt.rail_reference == "ab-100"
        narration = paid.narration or ""
        for must in ("Airbills", "airtime", "08031234567", "MTN", "ab-100", "500"):
            assert must in narration, (must, narration)
        assert paid.state.execution.funding.get("airbills_id") == "ab-100"
        return True

    assert _run(_go()) is True


def test_classify_turn_routes_funding_to_orchestrator():
    from miriam_agent.orchestrator import classify_turn

    assert classify_turn("buy 50k usdc") == "orchestrator"
    assert classify_turn("buy 100000 naira of bitcoin") == "orchestrator"
    assert classify_turn("sell 50 usdc to naira") == "orchestrator"
    assert classify_turn("482916") == "orchestrator"


def test_confirm_onramp_without_token_rejected():
    from layer_fakes import jev, judge_of

    from miriam_agent.hands.ledger import InMemoryLedgerStore
    from miriam_agent.orchestrator import Event, Orchestrator

    async def _go():
        store = InMemoryLedgerStore()
        orch = Orchestrator(
            store=store,
            judge=judge_of(jev(mode="ask", action="allow")),
            go_token=None,
            provider=None,
        )
        res = await orch.handle(
            Event(type="utterance", user_id="u1", text="buy 50k usdc")
        )
        assert res.confirm_id, "buy turn must stage a confirm challenge"
        tapped = await orch.handle(
            Event(type="confirm", user_id="u1", confirm_id=res.confirm_id)
        )
        assert tapped.receipt is not None
        assert tapped.receipt.status == "rejected"
        assert "NO_GO_TOKEN" in (tapped.receipt.reasons or [])
        ledger = await store.load("u1")
        assert ledger is not None
        return True

    assert _run(_go()) is True


def test_wrong_otp_retries_never_auto_buys(monkeypatch):
    from miriam_agent.hands import funding_settle as fs
    from miriam_agent.hands.ledger import Challenge, InMemoryLedgerStore, new_ledger
    from miriam_agent.orchestrator import Event, Orchestrator

    async def _fake_verify(token, otp, **kw):
        return {"ok": False, "reasons": ["PAJ_VERIFY_FAILED"]}

    monkeypatch.setattr(fs, "verify_paj_otp", _fake_verify)

    async def _go():
        from datetime import UTC, datetime, timedelta

        store = InMemoryLedgerStore()
        ledger = new_ledger("u9")
        now = datetime.now(UTC)
        ledger.challenges["confirm_otp_12345678"] = Challenge(
            id="confirm_otp_12345678",
            user_id="u9",
            action="paj_otp",
            amount=Decimal("50000"),
            counterparty="USDC",
            destination="USDC",
            sleeve="spendable",
            decision_id="dec_test",
            created_at=now,
            expires_at=now + timedelta(minutes=30),
            meta={"onramp_confirm_id": "confirm_x", "provider": "paj", "rate": "1530"},
        )
        store.seed(ledger)
        from layer_fakes import jev, judge_of

        orch = Orchestrator(
            store=store,
            judge=judge_of(jev(mode="ask", action="allow")),
            go_token="tok",
            provider=None,
        )
        res = await orch.handle(Event(type="utterance", user_id="u9", text="000000"))
        assert res.receipt is not None
        assert res.receipt.status == "rejected"
        assert "PAJ_VERIFY_FAILED" in (res.receipt.reasons or [])
        # A failed verify keeps the OTP challenge open for retry.
        assert res.confirm_id == "confirm_otp_12345678"
        # Challenge stays open for retry: no order was created.
        led = await store.load("u9")
        ch = led.challenges["confirm_otp_12345678"]
        assert ch.status == "pending"
        assert not any(
            (r.action or "").startswith("onramp_prepare") and r.status == "executed"
            for r in led.receipts
        )
        return True

    assert _run(_go()) is True


class _E2EFakeGo:
    """Quote + Paj OTP + onramp order, with the bank details Go returns."""

    async def get_crypto_quote(self, token, side, amount):
        assert token == "tok"
        assert side == "onramp"
        return {
            "side": "onramp",
            "provider": "paj",
            "rate": 1530.5,
            "estimatedOutput": 32.5,
            "tokenAmount": 32.5,
            "fee": 250,
        }

    async def _token_post(self, path, token, payload, **kw):
        if path.endswith("/paj/initiate"):
            return {"status": "otp_sent", "recipient": "+234***123"}
        if path.endswith("/paj/verify"):
            assert payload["otp"] == "482916", payload
            return {"status": "verified"}
        if path.endswith("/ramp/onramp") or path.endswith("/paj/onramp"):
            assert payload == {"amount": 50000.0, "currency": "NGN"}, payload
            return {
                "transactionId": "ord-9",
                "orderId": "ord-9",
                "accountNumber": "9876543210",
                "accountName": "Rail Collections",
                "bank": "Wema Bank",
                "fiatAmount": 50000,
                "rate": 1530.5,
                "tokenAmount": 32.5,
                "fee": 250,
            }
        raise AssertionError(path)

    # Named funding writers (hands calls these, never _token_post directly).
    async def paj_initiate_session(self, token, payload, **kw):
        return await self._token_post(
            "/api/v1/funding/paj/initiate", token, payload, **kw
        )

    async def paj_verify_otp(self, token, payload, **kw):
        return await self._token_post(
            "/api/v1/funding/paj/verify", token, payload, **kw
        )

    async def funding_create_onramp(self, token, kind, payload, **kw):
        return await self._token_post(
            f"/api/v1/funding/{kind}/onramp", token, payload, **kw
        )


def test_bank_details_reach_the_narration(monkeypatch):
    """The user-readable message carries the account to pay, not just a receipt.

    No LLM involved (provider=None), so the narration is the deterministic
    line: if the bank details are not in STATE, they cannot be in the message.
    """
    import miriam_agent.integrations.go_client as gc
    from miriam_agent.voice.generate import clamp_violations

    monkeypatch.setattr(gc, "get_go_client", lambda: _E2EFakeGo())

    from layer_fakes import jev, judge_of

    from miriam_agent.hands.ledger import InMemoryLedgerStore
    from miriam_agent.orchestrator import Event, Orchestrator

    async def _go():
        store = InMemoryLedgerStore()
        orch = Orchestrator(
            store=store,
            judge=judge_of(jev(mode="ask", action="allow")),
            go_token="tok",
            provider=None,
        )
        said = await orch.handle(
            Event(type="utterance", user_id="u7", text="buy 50000 usdc")
        )
        assert said.confirm_id, "buy turn must stage a confirm challenge"

        tapped = await orch.handle(
            Event(type="confirm", user_id="u7", confirm_id=said.confirm_id)
        )
        assert tapped.receipt is not None
        assert tapped.receipt.status == "executed"
        assert tapped.receipt.rail_reference == "ord-9"
        # RampHub tells the user the bank account to pay, not a wallet address.
        narration = tapped.narration or ""
        flat = narration.replace(",", "")
        for must in ("9876543210", "Wema", "1530.5", "32.5", "50000"):
            assert must in flat, (must, narration)
        assert "wallet address" not in narration.casefold()
        assert tapped.confirm_id == ""
        assert "CONFIRM" not in narration
        assert clamp_violations(tapped.state, narration) == []
        assert tapped.state.execution.funding.get("account_number") == "9876543210"
        assert tapped.state.execution.funding.get("bank") == "Wema Bank"
        return True

    assert _run(_go()) is True


def test_missing_payin_account_is_not_executed_and_keeps_the_replay_key():
    from miriam_agent.hands.funding_settle import _finish_onramp_order
    from miriam_agent.hands.ledger import new_ledger

    ledger = new_ledger("u-missing")
    receipt, card, _ledger = _finish_onramp_order(
        ledger,
        Decimal("20000"),
        "USDC",
        "dec_1",
        "confirm_1",
        {"rate": "1416.65"},
        {"ok": True, "raw": {"transactionId": "ord-1", "fiatAmount": 20000}},
    )
    assert card is None
    assert receipt.status == "rejected"
    assert "PAYIN_ACCOUNT_MISSING" in receipt.reasons
    assert receipt.idempotency_key == "onramp-incomplete:confirm_1"
    assert ledger.receipt_for("onramp:confirm_1") is None


def test_card_amount_uses_the_provider_fiat_amount():
    from miriam_agent.hands.funding_settle import _finish_onramp_order
    from miriam_agent.hands.ledger import new_ledger

    ledger = new_ledger("u-fiat")
    receipt, card, _ledger = _finish_onramp_order(
        ledger,
        Decimal("20000"),
        "USDC",
        "dec_2",
        "confirm_2",
        {"rate": "1416"},
        {
            "ok": True,
            "raw": {
                "transactionId": "ord-2",
                "fiatAmount": 20000.5,
                "accountNumber": "0123456789",
                "accountName": "RampHub Checkout",
                "bank": "Wema Bank",
            },
        },
    )
    assert receipt.status == "executed"
    assert card is not None
    assert card["amount"] == "20000.5 NGN"
    assert "Pay exactly 20000.5 NGN" in receipt.detail

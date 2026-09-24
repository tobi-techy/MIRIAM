"""Live Face ID confirmation cards (Go card <-> Miriam challenge join).

Mapping is pure (every wiring-table row), settle/card-terminal endpoints are
rail-key authed and idempotent, and first-writer-wins holds across the chat
tap and the settle endpoint in both orders.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from layer_fakes import FakeProvider, jev, judge_of  # noqa: E402

from miriam_agent.api import chat, dependencies  # noqa: E402
from miriam_agent.api.main import app  # noqa: E402
from miriam_agent.config.settings import get_settings  # noqa: E402
from miriam_agent.confirm_cards import client as cards_client  # noqa: E402
from miriam_agent.confirm_cards.client import (  # noqa: E402
    CARD_TTL_SECONDS,
    CardUnavailable,
    mark_card_terminal,
    mint_card,
)
from miriam_agent.confirm_cards.mapping import card_for_action  # noqa: E402
from miriam_agent.database.models import User  # noqa: E402
from miriam_agent.hands.ledger import (  # noqa: E402
    Challenge,
    InMemoryLedgerStore,
    Ledger,
    Track,
    money,
)
from miriam_agent.hands.limits import Policy  # noqa: E402
from miriam_agent.hands.save_rule import (  # noqa: E402
    parse_save_rule_utterance,
    save_rule_binding,
    select_save_automation,
)
from miriam_agent.hands.transfer import InMemoryRail  # noqa: E402
from miriam_agent.orchestrator import (  # noqa: E402
    CHALLENGE_TTL_MINUTES,
    Event,
    Orchestrator,
)

USER_ID = "u-confirm-cards"
SERVICE_KEY = "test-rail-service-key-0123456789abcdef"


@pytest.fixture
def service_key(monkeypatch):
    monkeypatch.setenv("RAIL_SERVICE_KEY", SERVICE_KEY)
    get_settings.cache_clear()
    yield SERVICE_KEY
    get_settings.cache_clear()


def _now() -> datetime:
    return datetime.now(UTC)


def _challenge(action: str = "transfer", **extra: Any) -> Challenge:
    now = _now()
    base: dict[str, Any] = {
        "id": "confirm_test01",
        "user_id": USER_ID,
        "action": action,
        "amount": money("20000"),
        "counterparty": "Funsho",
        "destination": "Funsho",
        "sleeve": "spendable",
        "decision_id": "dec_test01",
        "created_at": now,
        "expires_at": now + timedelta(minutes=CHALLENGE_TTL_MINUTES),
    }
    base.update(extra)
    return Challenge(**base)


# ---------------------------------------------------------------------------
# mapping: every wiring-table row
# ---------------------------------------------------------------------------


def test_map_transfer():
    spec = card_for_action(_challenge("transfer"))
    assert spec is not None
    assert spec.go_action == "transfer.send"
    assert spec.payload == {
        "identifier": "Funsho",
        "amount": "20000.00",
        "miriam_confirm_id": "confirm_test01",
    }
    assert spec.title is None


def test_map_internal_move():
    spec = card_for_action(
        _challenge("internal_move", counterparty="", destination="Stash")
    )
    assert spec is not None
    assert spec.go_action == "save.sweep"
    assert spec.payload["destination"] == "Stash"
    assert spec.payload["amount"] == "20000.00"
    assert spec.payload["miriam_confirm_id"] == "confirm_test01"
    # Honest copy: the save.sweep renderer would call a stash move "Park …".
    assert spec.title is not None and "Stash" in spec.title


def test_map_order_buy_and_sell():
    buy = _challenge(
        "order",
        counterparty="NVDAx",
        amount=money("50"),
        meta={"side": "buy", "symbol": "NVDAx"},
    )
    spec = card_for_action(buy)
    assert spec is not None
    assert spec.go_action == "invest.buy"
    assert spec.payload["symbol"] == "NVDAx"
    assert spec.payload["amount_usd"] == "50.00"
    assert "strategy_id" not in spec.payload  # sleeve tag is not a Glider id

    sell = _challenge(
        "order",
        counterparty="GOOGLx",
        amount=money("100"),
        meta={"side": "sell", "symbol": "GOOGLx", "strategy_id": "strat_9"},
    )
    spec = card_for_action(sell)
    assert spec is not None
    assert spec.go_action == "invest.sell"
    assert spec.payload["strategy_id"] == "strat_9"


def test_map_set_allocation_uses_override_not_enum():
    import json as _json

    legs = [{"symbol": "NVDAx", "weight": 60}, {"symbol": "GOOGLx", "weight": 40}]
    spec = card_for_action(
        _challenge(
            "set_allocation",
            amount=money("0"),
            meta={"legs": _json.dumps(legs), "strategy_id": "strat_9"},
        )
    )
    assert spec is not None
    assert spec.go_action == "invest.buy"
    assert spec.title == "Set allocation · 2 legs"
    assert spec.payload["legs"] == legs
    assert spec.payload["strategy_id"] == "strat_9"


def test_map_rebalance_pause_resume():
    spec = card_for_action(
        _challenge("rebalance", meta={"strategy": "rail-stock-sleeve"})
    )
    assert spec is not None
    assert spec.go_action == "invest.buy"
    assert spec.title == "Rebalance · rail-stock-sleeve"

    pause = card_for_action(_challenge("pause", meta={"strategy": "rail-stock-sleeve"}))
    assert pause is not None
    assert pause.go_action == "mandate.revoke"
    assert pause.title is not None and "Pause" in pause.title
    assert pause.subtitle is not None  # mandate.* has no Go renderer

    resume = card_for_action(
        _challenge("resume", meta={"strategy": "rail-stock-sleeve"})
    )
    assert resume is not None
    assert resume.go_action == "mandate.approve"
    assert resume.title is not None and "Resume" in resume.title


def test_map_save_rule():
    spec = card_for_action(_challenge("save_rule", meta={"percentage": "15"}))
    assert spec is not None
    assert spec.go_action == "save.sweep"
    assert spec.payload["percentage"] == "15"
    assert spec.title is None  # the renderer already writes the right copy

    flat = card_for_action(_challenge("save_rule", meta={"amount": "5000.00"}))
    assert flat is not None and flat.payload["amount"] == "5000.00"

    unbound = card_for_action(_challenge("save_rule", meta={}))
    assert unbound is None


def test_map_no_card_actions():
    # lock/unlock stay on chat confirm; onramp/offramp stay on Paj OTP;
    # invest enroll keeps its wallet-signature two-tap flow.
    for action in ("lock", "unlock", "onramp", "offramp", "invest"):
        assert card_for_action(_challenge(action)) is None, action


# ---------------------------------------------------------------------------
# save_rule parsing (deterministic regex)
# ---------------------------------------------------------------------------


def test_parse_save_rule_percentage():
    action = parse_save_rule_utterance("park 15% of this inflow")
    assert action is not None
    assert action.type == "save_rule"
    assert action.amount == Decimal("15")
    assert save_rule_binding(action.raw or "") == ("percentage", "15")


def test_parse_save_rule_amount():
    action = parse_save_rule_utterance("change the save rule to 5000")
    assert action is not None
    assert action.type == "save_rule"
    assert save_rule_binding(action.raw or "") == ("amount", "5000.00")


def test_parse_save_rule_leaves_stash_moves_alone():
    from miriam_agent.hands.transfer import parse_transfer_utterance

    assert parse_save_rule_utterance("move 5k to stash") is None
    assert parse_save_rule_utterance("save 5k") is None
    moved = parse_transfer_utterance("move 5k to stash")
    assert moved is not None and moved.type == "internal_move"


def test_select_save_automation():
    autos = [
        {"id": "a1", "type": "save_sweep", "name": "park inflow"},
        {"id": "a2", "type": "bill_pay", "name": "rent"},
    ]
    assert select_save_automation(autos)["id"] == "a1"
    assert select_save_automation(autos, automation_id="a2")["id"] == "a2"
    assert select_save_automation(autos, automation_id="nope") is None
    assert select_save_automation([autos[1]])["id"] == "a2"  # only one
    assert select_save_automation([]) is None


# ---------------------------------------------------------------------------
# orchestrator: mint on imessage, byte-identical legacy when disabled
# ---------------------------------------------------------------------------


def _ledger() -> Ledger:
    ledger = Ledger(
        user_id=USER_ID,
        track=Track(name="70/30", spend=Decimal("70"), save=Decimal("30")),
    )
    ledger.sleeves = {
        "spendable": money("90000"),
        "savings": money("0"),
        "yield": money("0"),
        "locked": money("0"),
    }
    return ledger


def _orchestrator(store, *, rail=None, cards_enabled=False) -> Orchestrator:
    return Orchestrator(
        store=store,
        policy=Policy.from_settings(),
        rail=rail or InMemoryRail(),
        provider=FakeProvider("Asking first."),
        judge=judge_of(jev(intent="order", afford=0.9)),
        go_token="test-token",
        cards_enabled=cards_enabled,
    )


class _Minted:
    def __init__(self, action_id: str):
        self.action_id = action_id
        self.confirm_url = f"https://example.com/confirm/{action_id}"
        self.calls: list[dict[str, Any]] = []


async def test_mint_on_imessage_appends_face_id_line(monkeypatch) -> None:
    minted = _Minted("act_face1")

    async def _mint(token, **kwargs):
        minted.calls.append({"token": token, **kwargs})
        from miriam_agent.confirm_cards.client import MintedCard

        return MintedCard(action_id=minted.action_id, confirm_url=minted.confirm_url)

    monkeypatch.setattr(cards_client, "mint_card", _mint)
    store = InMemoryLedgerStore()
    store.seed(_ledger())
    orchestrator = _orchestrator(store, cards_enabled=True)

    asked = await orchestrator.handle_utterance(
        USER_ID, "send 5k to Ada", channel="imessage", thread_id="space1"
    )
    assert asked.confirm_id
    assert asked.card_action_id == "act_face1"
    assert "Face ID" in (asked.narration or "")
    assert f"confirm {asked.confirm_id}" in (asked.narration or "")
    assert minted.calls and minted.calls[0]["token"] == "test-token"
    assert minted.calls[0]["go_action"] == "transfer.send"
    assert minted.calls[0]["payload"]["miriam_confirm_id"] == asked.confirm_id
    assert minted.calls[0]["payload"]["thread_id"] == "space1"

    stored = await store.load(USER_ID)
    assert stored is not None
    assert stored.challenges[asked.confirm_id].card_action_id == "act_face1"


async def test_cards_disabled_is_byte_identical_legacy(monkeypatch) -> None:
    async def _boom(token, **kwargs):
        raise AssertionError("no Go calls when cards are disabled")

    monkeypatch.setattr(cards_client, "mint_card", _boom)
    store = InMemoryLedgerStore()
    store.seed(_ledger())
    orchestrator = _orchestrator(store, cards_enabled=False)

    asked = await orchestrator.handle_utterance(
        USER_ID, "send 5k to Ada", channel="imessage", thread_id="space1"
    )
    assert asked.confirm_id
    assert asked.card_action_id == ""
    assert "Face ID" not in (asked.narration or "")
    # The chat fallback rides on the decision, untouched by the cards flag.
    assert asked.decision is not None
    assert asked.decision.get("confirm_id") == asked.confirm_id


async def test_mint_failure_falls_back_to_text(monkeypatch) -> None:
    async def _down(token, **kwargs):
        from miriam_agent.confirm_cards.client import CardUnavailable

        raise CardUnavailable("go down")

    monkeypatch.setattr(cards_client, "mint_card", _down)
    store = InMemoryLedgerStore()
    store.seed(_ledger())
    orchestrator = _orchestrator(store, cards_enabled=True)

    asked = await orchestrator.handle_utterance(
        USER_ID, "send 5k to Ada", channel="imessage", thread_id="space1"
    )
    assert asked.confirm_id
    assert asked.card_action_id == ""
    assert asked.decision is not None
    assert asked.decision.get("confirm_id") == asked.confirm_id


async def test_non_allowlisted_channel_mints_nothing(monkeypatch) -> None:
    async def _boom(token, **kwargs):
        raise AssertionError("terminal must not mint")

    monkeypatch.setattr(cards_client, "mint_card", _boom)
    store = InMemoryLedgerStore()
    store.seed(_ledger())
    orchestrator = _orchestrator(store, cards_enabled=True)

    asked = await orchestrator.handle_utterance(
        USER_ID, "send 5k to Ada", channel="terminal", thread_id="t1"
    )
    assert asked.confirm_id and asked.card_action_id == ""


def test_card_ttl_under_challenge_ttl() -> None:
    assert CARD_TTL_SECONDS * 2 < CHALLENGE_TTL_MINUTES * 60


# ---------------------------------------------------------------------------
# endpoints: settle + card-terminal
# ---------------------------------------------------------------------------


class _StubMemory:
    async def store_interaction(self, **kwargs: Any) -> None:
        return None

    async def get_conversation_history(self, *args: Any, **kwargs: Any) -> list:
        return []


def _seeded(challenge: Challenge, *, rail=None):
    store = InMemoryLedgerStore()
    ledger = _ledger()
    ledger.challenges[challenge.id] = challenge
    store.seed(ledger)
    return store, (rail or InMemoryRail())


def _wire(monkeypatch, *, store, orchestrator) -> None:
    monkeypatch.setattr(chat, "_orchestrator_for", lambda token: orchestrator)
    monkeypatch.setattr(chat, "_get_ledger_store", lambda: store)
    user = User()
    user.id = USER_ID
    user.username = "tester"
    user.roles = ["verified"]
    overrides = {
        **app.dependency_overrides,
        dependencies.get_current_user: lambda: user,
        dependencies.get_bearer_token: lambda: "test-token",
        dependencies.get_memory_store: lambda: _StubMemory(),
        dependencies.get_supermemory_memory_dep: lambda: None,
    }
    monkeypatch.setattr(app, "dependency_overrides", overrides)


def _settle_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer test-token",
        "X-Rail-Service-Key": SERVICE_KEY,
    }


def _open_transfer_challenge(**extra: Any) -> Challenge:
    now = _now()
    base: dict[str, Any] = {
        "id": "confirm_face1",
        "user_id": USER_ID,
        "action": "transfer",
        "amount": money("2000"),
        "counterparty": "Ada",
        "destination": "Ada",
        "sleeve": "spendable",
        "decision_id": "dec_face1",
        "created_at": now,
        "expires_at": now + timedelta(minutes=CHALLENGE_TTL_MINUTES),
    }
    base.update(extra)
    return Challenge(**base)


async def test_settle_pass_executes(monkeypatch, service_key) -> None:
    rail = InMemoryRail()
    store, _ = _seeded(_open_transfer_challenge(), rail=rail)
    orchestrator = _orchestrator(store, rail=rail)
    _wire(monkeypatch, store=store, orchestrator=orchestrator)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/chat/settle",
        headers=_settle_headers(),
        json={
            "confirm_id": "confirm_face1",
            "biometric": "pass",
            "card_action_id": "act_face1",
        },
    )
    assert response.status_code == 200, response.text[:400]
    body = response.json()
    assert body["status"] == "completed"
    assert body["state"] == "completed"
    assert body["receipt_id"]
    assert len(rail.calls) == 1


async def test_face_id_provenance_lands_on_audit_rows() -> None:
    rail = InMemoryRail()
    store, _ = _seeded(_open_transfer_challenge(), rail=rail)
    orchestrator = _orchestrator(store, rail=rail)

    result = await orchestrator.handle(
        Event(
            type="confirm",
            user_id=USER_ID,
            confirm_id="confirm_face1",
            provenance="face_id",
        )
    )
    assert result.receipt is not None and result.receipt.status == "executed"
    assert result.audit
    assert all("[via face_id]" in row.detail for row in result.audit)

    store2, _ = _seeded(_open_transfer_challenge(), rail=rail)
    orchestrator2 = _orchestrator(store2, rail=rail)
    plain = await orchestrator2.handle(
        Event(type="confirm", user_id=USER_ID, confirm_id="confirm_face1")
    )
    assert plain.receipt is not None and plain.receipt.status == "executed"
    assert all("[via " not in row.detail for row in plain.audit)


def test_settle_rejects_non_pass_biometric(monkeypatch, service_key) -> None:
    store, _ = _seeded(_open_transfer_challenge())
    _wire(monkeypatch, store=store, orchestrator=_orchestrator(store))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/chat/settle",
        headers=_settle_headers(),
        json={"confirm_id": "confirm_face1", "biometric": "fail"},
    )
    assert response.status_code == 422


def test_settle_unknown_is_terminal_not_500(monkeypatch, service_key) -> None:
    store, _ = _seeded(_open_transfer_challenge())
    _wire(monkeypatch, store=store, orchestrator=_orchestrator(store))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/chat/settle",
        headers=_settle_headers(),
        json={"confirm_id": "confirm_nope", "biometric": "pass"},
    )
    assert response.status_code == 200, response.text[:400]
    assert response.json()["status"] == "already_settled"


def test_settle_replay_executes_rail_once(monkeypatch, service_key) -> None:
    rail = InMemoryRail()
    store, _ = _seeded(_open_transfer_challenge(), rail=rail)
    _wire(monkeypatch, store=store, orchestrator=_orchestrator(store, rail=rail))
    client = TestClient(app, raise_server_exceptions=False)
    body = {"confirm_id": "confirm_face1", "biometric": "pass"}

    first = client.post("/api/v1/chat/settle", headers=_settle_headers(), json=body)
    second = client.post("/api/v1/chat/settle", headers=_settle_headers(), json=body)
    assert first.json()["status"] == "completed"
    assert second.json()["status"] == "already_settled"
    assert len(rail.calls) == 1


def test_settle_requires_service_key(monkeypatch, service_key) -> None:
    store, _ = _seeded(_open_transfer_challenge())
    _wire(monkeypatch, store=store, orchestrator=_orchestrator(store))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/chat/settle",
        headers={"Authorization": "Bearer test-token"},
        json={"confirm_id": "confirm_face1", "biometric": "pass"},
    )
    assert response.status_code == 401


async def test_first_writer_wins_chat_then_settle(monkeypatch, service_key) -> None:
    rail = InMemoryRail()
    store, _ = _seeded(_open_transfer_challenge(), rail=rail)
    orchestrator = _orchestrator(store, rail=rail)
    _wire(monkeypatch, store=store, orchestrator=orchestrator)
    client = TestClient(app, raise_server_exceptions=False)

    tapped = await orchestrator.handle_confirm(USER_ID, "confirm_face1", True)
    assert tapped.receipt is not None and tapped.receipt.status == "executed"
    response = client.post(
        "/api/v1/chat/settle",
        headers=_settle_headers(),
        json={"confirm_id": "confirm_face1", "biometric": "pass"},
    )
    assert response.json()["status"] == "already_settled"
    assert len(rail.calls) == 1


async def test_first_writer_wins_settle_then_chat(monkeypatch, service_key) -> None:
    rail = InMemoryRail()
    store, _ = _seeded(_open_transfer_challenge(), rail=rail)
    orchestrator = _orchestrator(store, rail=rail)
    _wire(monkeypatch, store=store, orchestrator=orchestrator)
    client = TestClient(app, raise_server_exceptions=False)

    first = client.post(
        "/api/v1/chat/settle",
        headers=_settle_headers(),
        json={"confirm_id": "confirm_face1", "biometric": "pass"},
    )
    assert first.json()["status"] == "completed"
    tapped = await orchestrator.handle_confirm(USER_ID, "confirm_face1", True)
    assert tapped.receipt is not None and tapped.receipt.status == "rejected"
    assert len(rail.calls) == 1


async def test_chat_settle_marks_card_completed(monkeypatch, service_key) -> None:
    rail = InMemoryRail()
    challenge = _open_transfer_challenge(card_action_id="act_face9")
    store, _ = _seeded(challenge, rail=rail)
    orchestrator = _orchestrator(store, rail=rail)
    marks: list[dict[str, Any]] = []

    async def _mark(service_key_arg, action_id, state, result=""):
        marks.append({"action": action_id, "state": state, "result": result})

    monkeypatch.setattr(cards_client, "mark_card_terminal", _mark)

    tapped = await orchestrator.handle_confirm(USER_ID, "confirm_face1", True)
    assert tapped.receipt is not None and tapped.receipt.status == "executed"
    assert len(marks) == 1
    assert marks[0]["action"] == "act_face9"
    assert marks[0]["state"] == "completed"


async def test_card_terminal_rejected_declines(monkeypatch, service_key) -> None:
    store, _ = _seeded(_open_transfer_challenge(card_action_id="act_face1"))
    _wire(monkeypatch, store=store, orchestrator=_orchestrator(store))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/chat/card-terminal",
        headers=_settle_headers(),
        json={"confirm_id": "confirm_face1", "state": "rejected"},
    )
    assert response.status_code == 200, response.text[:400]

    stored = await store.load(USER_ID)
    assert stored is not None
    challenge = stored.challenges["confirm_face1"]
    assert challenge.status == "consumed"
    assert any(
        r.action == "decline" and "USER_DECLINED" in (r.reasons or [])
        for r in stored.receipts
    )
    # Replay is a no-op: no second receipt.
    before = len(stored.receipts)
    again = client.post(
        "/api/v1/chat/card-terminal",
        headers=_settle_headers(),
        json={"confirm_id": "confirm_face1", "state": "rejected"},
    )
    assert again.status_code == 200
    stored = await store.load(USER_ID)
    assert stored is not None and len(stored.receipts) == before


async def test_card_terminal_expired_expires(monkeypatch, service_key) -> None:
    store, _ = _seeded(_open_transfer_challenge())
    _wire(monkeypatch, store=store, orchestrator=_orchestrator(store))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/chat/card-terminal",
        headers=_settle_headers(),
        json={"confirm_id": "confirm_face1", "state": "expired"},
    )
    assert response.status_code == 200, response.text[:400]

    stored = await store.load(USER_ID)
    assert stored is not None
    assert stored.challenges["confirm_face1"].status == "expired"
    assert any("CHALLENGE_EXPIRED" in (r.reasons or []) for r in stored.receipts)


def test_card_terminal_unknown_is_200_noop(monkeypatch, service_key) -> None:
    store, _ = _seeded(_open_transfer_challenge())
    _wire(monkeypatch, store=store, orchestrator=_orchestrator(store))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/chat/card-terminal",
        headers=_settle_headers(),
        json={"confirm_id": "confirm_nope", "state": "expired"},
    )
    assert response.status_code == 200, response.text[:400]


def test_card_terminal_rejects_bad_state(monkeypatch, service_key) -> None:
    store, _ = _seeded(_open_transfer_challenge())
    _wire(monkeypatch, store=store, orchestrator=_orchestrator(store))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/chat/card-terminal",
        headers=_settle_headers(),
        json={"confirm_id": "confirm_face1", "state": "completed"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# client fail-closed behaviour
# ---------------------------------------------------------------------------


async def test_mint_unreachable_is_card_unavailable(monkeypatch) -> None:
    import httpx

    class _Down:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def post(self, *args: Any, **kwargs: Any):
            raise httpx.ConnectError("down")

    monkeypatch.setattr(cards_client.httpx, "AsyncClient", _Down)
    try:
        await mint_card("tok", go_action="transfer.send", payload={})
    except CardUnavailable:
        return
    raise AssertionError("unreachable Go must raise CardUnavailable")


async def test_mark_without_service_key_is_card_unavailable() -> None:
    try:
        await mark_card_terminal("", "act_1", "completed")
    except CardUnavailable:
        return
    raise AssertionError("empty service key must raise CardUnavailable")

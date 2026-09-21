"""Pre-flip gates: the live chat path routes money to the Orchestrator.

Four checks, in the order the design names them:

1. no money tool name appears in ``api/`` or in the agent loop, so there is
   nothing for a chat turn to call,
2. a chat instruction to send money reaches the Orchestrator, and moves nothing
   without a confirmation,
3. the ``confirm_id`` a reply carries is a Hands challenge, and the safety
   ledger that used to hold confirmations no longer exists,
4. ``build_money_plan`` is not called on an inflow or a send turn.

They go through ``/api/v1/chat`` rather than calling the router by hand, because
the thing being tested is the wiring, not the classifier.
"""

from __future__ import annotations

import importlib.util
import pathlib
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from layer_fakes import FakeProvider, jev, judge_of

from miriam_agent.api import chat, dependencies
from miriam_agent.api.main import app
from miriam_agent.database.models import User
from miriam_agent.hands.ledger import (
    InMemoryLedgerStore,
    Ledger,
    LedgerUnavailable,
    RentFirst,
    Track,
    money,
)
from miriam_agent.hands.limits import Policy
from miriam_agent.hands.transfer import InMemoryRail, TransferInstruction
from miriam_agent.orchestrator import Orchestrator
from miriam_agent.safety.money_tools import MONEY_TOOL_NAMES

ROOT = pathlib.Path(__file__).resolve().parents[1] / "miriam_agent"
USER_ID = "u-live-1"


class RecordingRail(InMemoryRail):
    def __init__(self) -> None:
        super().__init__()
        self.moves: list[dict[str, Any]] = []

    async def execute(self, instruction: TransferInstruction):  # noqa: ANN201
        self.moves.append(
            {
                "amount": str(instruction.amount),
                "sleeve": instruction.sleeve,
                "counterparty": instruction.counterparty,
            }
        )
        return await super().execute(instruction)


class StubMemory:
    """The three memory calls a money turn makes, and nothing else."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    async def ensure_user(self, user: Any) -> None:  # noqa: ARG002
        return None

    async def get_conversation(self, conversation_id: str) -> None:  # noqa: ARG002
        return None

    async def store_interaction(self, **kwargs: Any) -> None:
        self.writes.append(str(kwargs.get("role")))

    async def get_conversation_history(self, *args: Any, **kwargs: Any) -> list:
        return []


def _ledger(*, spendable: str, rent_required: str = "0", reserved: str = "0") -> Ledger:
    ledger = Ledger(
        user_id=USER_ID,
        track=Track(name="70/30", spend=Decimal("70"), save=Decimal("30")),
    )
    ledger.sleeves = {
        "spendable": money(spendable),
        "savings": money(0),
        "yield": money(0),
        "locked": money(reserved),
    }
    ledger.rent_first = RentFirst(
        required=money(rent_required), reserved=money(reserved), due_in_days=9
    )
    return ledger


def _wire(monkeypatch, *, ledger: Ledger, judge=None):
    """Point the chat endpoints at a test Orchestrator.

    ``chat._orchestrator_for`` is the seam where the live path picks the shared
    Redis ledger and the Go rail. Replacing it here is what keeps this test off
    the network while still exercising the real routing above it.
    """
    monkeypatch.setattr(chat._validator, "validate_rate_limit", _async_true)
    store = InMemoryLedgerStore()
    store.seed(ledger)
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
        judge=judge or judge_of(jev(intent="order", afford=0.9)),
    )
    monkeypatch.setattr(chat, "_orchestrator_for", lambda token: orchestrator)

    user = User()
    user.id = USER_ID
    user.username = "tester"
    user.email = "t@test.invalid"
    user.full_name = "Tester"
    user.is_active = True
    user.roles = ["verified"]
    memory = StubMemory()
    overrides = {
        **app.dependency_overrides,
        dependencies.get_current_user: lambda: user,
        dependencies.get_bearer_token: lambda: "test-token",
        dependencies.get_memory_store: lambda: memory,
        dependencies.get_supermemory_memory_dep: lambda: None,
    }
    monkeypatch.setattr(app, "dependency_overrides", overrides)
    return store, rail, memory


async def _async_true(*args: Any, **kwargs: Any) -> bool:
    return True


def await_sync(coro: Any) -> Any:
    """Run a coroutine from a sync test (pytest-asyncio is not in play here)."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _post(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/api/v1/chat",
        headers={"Authorization": "Bearer test-token"},
        json=body,
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 1. No money tool name in api/ or the agent loop
# ---------------------------------------------------------------------------


def test_no_money_tool_name_appears_in_api_or_the_agent_loop():
    """A grep for the forbidden names, so one cannot be re-added quietly.

    ``api/`` and the agent loop are the paths a chat turn travels. If a money
    tool name appears in either, something there can still ask for a rail.
    """
    targets = sorted((ROOT / "api").rglob("*.py")) + [
        ROOT / "agents" / "agent_loop.py",
    ]
    for path in targets:
        source = path.read_text()
        for name in sorted(MONEY_TOOL_NAMES):
            assert f'"{name}"' not in source, f"{path} names the money tool {name}"


def test_the_agent_loop_has_no_rail_and_no_approval_path():
    """The loop's own text, checked for the machinery it used to hold."""
    source = (ROOT / "agents" / "agent_loop.py").read_text()
    for gone in (
        "approved_actions",
        "confirmation_store",
        "PendingConfirmationStore",
        "_replay_staged_confirmation",
        "confirmation_token",
        "requires_confirmation",
        "proposed_actions",
    ):
        assert gone not in source, f"agent_loop still carries {gone}"


# ---------------------------------------------------------------------------
# 2. A send instruction reaches the Orchestrator and moves nothing
# ---------------------------------------------------------------------------


def test_chat_send_200k_reaches_the_orchestrator_and_moves_nothing(monkeypatch):
    """The spec's fixture, through the live endpoint."""
    ledger = _ledger(spendable="184000", rent_required="120000")
    _store, rail, memory = _wire(
        monkeypatch,
        ledger=ledger,
        judge=judge_of(jev(intent="order", afford=0.21, violation=0.9)),
    )
    client = TestClient(app, raise_server_exceptions=False)

    payload = _post(client, {"message": "Send 200k to Femi"})

    # It was classified as a money turn and answered by the money layer.
    assert payload["decision"] is not None
    assert payload["decision"]["next_mode"] == "ask"
    assert payload["decision"]["action_choice"] == "deny"
    assert "RENT_SHORT" in payload["decision"]["reasons"]
    # And nothing moved. The response carries no confirmation protocol, so the
    # only signal is the absence of an id plus the receipt that says refused.
    assert rail.moves == []
    assert payload["confirm_id"] == ""
    assert "requires_confirmation" not in payload
    assert "cards" not in payload
    assert payload["receipt"]["status"] == "rejected"
    assert memory.writes == ["user", "assistant"]


def test_chat_does_not_route_a_money_turn_through_the_agent(monkeypatch):
    """The agent loop must not even be constructed for a money turn."""
    ledger = _ledger(spendable="50000")
    _wire(monkeypatch, ledger=ledger)

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a money turn must not build the agent loop")

    monkeypatch.setattr(chat, "Agent", _explode)
    client = TestClient(app, raise_server_exceptions=False)
    payload = _post(client, {"message": "send 1k to Ada"})

    assert payload["decision"]["intent_type"] == "order"


# ---------------------------------------------------------------------------
# 3. The confirm id is a Hands challenge, not the safety ledger
# ---------------------------------------------------------------------------


def test_the_confirm_id_in_the_reply_is_a_hands_challenge(monkeypatch):
    ledger = _ledger(spendable="90000")
    store, rail, _memory = _wire(monkeypatch, ledger=ledger)
    client = TestClient(app, raise_server_exceptions=False)

    payload = _post(client, {"message": "send 5k to Ada"})
    confirm_id = payload["confirm_id"]
    assert confirm_id
    assert "requires_confirmation" not in payload
    assert rail.moves == []

    # It is a challenge Hands stored on the ledger, bound to the cap.
    stored = await_sync(store.load(USER_ID))
    challenge = stored.challenges[confirm_id]
    assert challenge.status == "pending"
    assert challenge.amount == money(2000)
    assert challenge.counterparty == "Ada"


def test_the_safety_confirmation_ledger_no_longer_exists():
    """One confirmation mechanism, and it is Hands."""
    assert importlib.util.find_spec("miriam_agent.safety.confirmations") is None


def test_a_tap_settles_the_challenge_by_id(monkeypatch):
    ledger = _ledger(spendable="90000")
    store, rail, _memory = _wire(monkeypatch, ledger=ledger)
    client = TestClient(app, raise_server_exceptions=False)

    issued = _post(client, {"message": "send 5k to Ada"})
    settled = _post(
        client, {"confirm_id": issued["confirm_id"], "yes": True, "message": ""}
    )

    assert settled["receipt"]["status"] == "executed"
    assert settled["receipt"]["amount"] == "2000.00"
    assert [m["amount"] for m in rail.moves] == ["2000.00"]
    assert settled["confirm_id"] == issued["confirm_id"]


def test_a_declined_tap_moves_nothing(monkeypatch):
    ledger = _ledger(spendable="90000")
    _store, rail, _memory = _wire(monkeypatch, ledger=ledger)
    client = TestClient(app, raise_server_exceptions=False)

    issued = _post(client, {"message": "send 5k to Ada"})
    settled = _post(
        client, {"confirm_id": issued["confirm_id"], "yes": False, "message": ""}
    )

    assert rail.moves == []
    assert settled["receipt"]["status"] == "rejected"
    assert "USER_DECLINED" in settled["receipt"]["reasons"]


def test_typing_yes_in_a_message_settles_nothing(monkeypatch):
    """No free-text settlement: "yes" is a message like any other."""
    ledger = _ledger(spendable="90000")
    store, rail, _memory = _wire(monkeypatch, ledger=ledger)
    client = TestClient(app, raise_server_exceptions=False)

    issued = _post(client, {"message": "send 5k to Ada"})
    _post(client, {"message": "yes, do it"})

    assert rail.moves == []
    # The challenge is untouched: nothing consumed it, because nothing settled.
    reloaded = await_sync(_load_for(store))
    assert reloaded.challenges[issued["confirm_id"]].status == "pending"


async def _load_for(store: InMemoryLedgerStore) -> Ledger:
    ledger = await store.load(USER_ID)
    assert ledger is not None
    return ledger


# ---------------------------------------------------------------------------
# 4. build_money_plan is not called on a money turn
# ---------------------------------------------------------------------------


def test_build_money_plan_is_not_called_on_a_send_or_an_inflow(monkeypatch):
    """The plan engine is for advice, not for moving money.

    ``get_money_plan`` calls it; a money turn never reaches a tool, so it must
    not be called at all. A recorder rather than a raise, so a failure says how
    many times instead of just that it happened.
    """
    import miriam_agent.tools.money_definitions as money_defs

    calls: list[str] = []

    def _recorder(*args: Any, **kwargs: Any) -> Any:
        calls.append("build_money_plan")
        raise AssertionError("build_money_plan must not run on a money turn")

    monkeypatch.setattr(money_defs, "build_money_plan", _recorder)

    ledger = _ledger(spendable="90000")
    _wire(monkeypatch, ledger=ledger)
    client = TestClient(app, raise_server_exceptions=False)

    _post(client, {"message": "send 5k to Ada"})
    inflow = client.post(
        "/api/v1/money/inflow",
        headers={"Authorization": "Bearer test-token"},
        json={"payment_id": "pay_1", "amount": "420000", "source_raw": "PAYROLL"},
    )
    assert inflow.status_code == 200, inflow.text

    assert calls == []


# ---------------------------------------------------------------------------
# The inflow endpoint
# ---------------------------------------------------------------------------


def test_the_inflow_endpoint_splits_without_a_model(monkeypatch):
    ledger = _ledger(spendable="0", rent_required="150000")
    _store, rail, _memory = _wire(monkeypatch, ledger=ledger)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/money/inflow",
        headers={"Authorization": "Bearer test-token"},
        json={"payment_id": "pay_42", "amount": "420000", "source_raw": "PAYROLL"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["receipt"]["status"] == "executed"
    assert payload["receipt"]["action"] == "inflow_split"
    assert payload["receipt"]["sleeves_after"]["savings"] == "126000.00"
    assert payload["receipt"]["sleeves_after"]["locked"] == "150000.00"
    # A split is internal: nothing goes to a rail.
    assert rail.moves == []


def test_a_pasted_inflow_alert_is_split_and_re_pasting_splits_once(monkeypatch):
    """A forwarded alert is money arriving, not an instruction.

    It goes to the inflow path with an id derived from the alert, so the same
    alert pasted twice splits once. This is the one inflow shape that reaches
    chat; the rail webhook is the authoritative path.
    """
    ledger = _ledger(spendable="0", rent_required="150000")
    store, rail, _memory = _wire(monkeypatch, ledger=ledger)
    client = TestClient(app, raise_server_exceptions=False)
    alert = "Credit alert: NGN420,000.00 from ACME PAYROLL"

    first = _post(client, {"message": alert})
    second = _post(client, {"message": alert})

    assert first["receipt"]["action"] == "inflow_split"
    assert "idempotent" not in second  # the reply carries no second split
    reloaded = await_sync(_load_for(store))
    assert reloaded.sleeves["savings"] == money(126000)
    assert reloaded.sleeves["locked"] == money(150000)
    assert rail.moves == []


# ---------------------------------------------------------------------------
# The streaming endpoint routes the same way
# ---------------------------------------------------------------------------


def test_chat_stream_routes_a_money_turn_to_the_orchestrator(monkeypatch):
    ledger = _ledger(spendable="90000")
    _store, rail, _memory = _wire(monkeypatch, ledger=ledger)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/chat/stream",
        headers={"Authorization": "Bearer test-token"},
        json={"message": "send 5k to Ada"},
    )

    assert response.status_code == 200, response.text
    body = response.text
    # The money layer announces itself first, then streams its one reply, then
    # closes the stream. The verdict arrives before the words about it.
    assert body.index('"type": "money_layer"') < body.index('"type": "token"')
    assert body.index('"type": "token"') < body.index('"type": "done"')
    assert '"confirm_id"' in body
    # Still nothing moved: the reply asks, it does not send.
    assert rail.moves == []


def test_chat_stream_does_not_route_a_money_turn_to_the_agent(monkeypatch):
    """The streaming path must not fall through to the loop either."""
    ledger = _ledger(spendable="50000")
    _wire(monkeypatch, ledger=ledger)

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a money turn must not build the agent loop")

    monkeypatch.setattr(chat, "build_tool_registry", _explode)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/v1/chat/stream",
        headers={"Authorization": "Bearer test-token"},
        json={"message": "send 1k to Ada"},
    )
    assert response.status_code == 200
    assert '"type": "money_layer"' in response.text


def test_the_inflow_endpoint_needs_a_payment_id(monkeypatch):
    """Without the rail's id there is no idempotency key, so there is no split."""
    ledger = _ledger(spendable="0")
    _wire(monkeypatch, ledger=ledger)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/money/inflow",
        headers={"Authorization": "Bearer test-token"},
        json={"amount": "420000"},
    )
    assert response.status_code == 400
    assert "payment_id" in response.json()["detail"]


def test_a_repeated_inflow_webhook_splits_once(monkeypatch):
    ledger = _ledger(spendable="0")
    store, _rail, _memory = _wire(monkeypatch, ledger=ledger)
    client = TestClient(app, raise_server_exceptions=False)
    body = {"payment_id": "pay_dup", "amount": "100000", "source_raw": "PAYROLL"}

    for _ in range(2):
        response = client.post(
            "/api/v1/money/inflow",
            headers={"Authorization": "Bearer test-token"},
            json=body,
        )
        assert response.status_code == 200, response.text

    reloaded = await_sync(_load_for(store))
    assert reloaded.sleeves["spendable"] + reloaded.sleeves["savings"] == money(100000)


# ---------------------------------------------------------------------------
# A ledger that cannot be read
# ---------------------------------------------------------------------------


class _UnavailableStore:
    """A ledger store that is down, as a Redis outage with workers > 1 is."""

    async def load(self, user_id: str) -> None:  # noqa: ARG002
        raise LedgerUnavailable("the ledger store is unavailable: redis is down")

    async def save(self, ledger: Any) -> None:  # noqa: ARG002
        raise LedgerUnavailable("the ledger store is unavailable: redis is down")


def _wire_unavailable_ledger(monkeypatch):
    monkeypatch.setattr(chat._validator, "validate_rate_limit", _async_true)
    orchestrator = Orchestrator(
        store=_UnavailableStore(),  # type: ignore[arg-type]
        policy=Policy(max_auto=money(2000), max_with_confirm=money(100000)),
        rail=RecordingRail(),
        provider=FakeProvider("unused"),
        judge=judge_of(jev(intent="order")),
    )
    monkeypatch.setattr(chat, "_orchestrator_for", lambda token: orchestrator)

    user = User()
    user.id = USER_ID
    user.username = "tester"
    user.email = "t@test.invalid"
    user.full_name = "Tester"
    user.is_active = True
    user.roles = ["verified"]
    monkeypatch.setattr(
        app,
        "dependency_overrides",
        {
            **app.dependency_overrides,
            dependencies.get_current_user: lambda: user,
            dependencies.get_bearer_token: lambda: "test-token",
            dependencies.get_memory_store: lambda: StubMemory(),
            dependencies.get_supermemory_memory_dep: lambda: None,
        },
    )


def test_a_money_turn_says_so_when_the_ledger_is_unreachable(monkeypatch):
    """The typed failure Voice can say, and nothing invented in its place."""
    _wire_unavailable_ledger(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)

    payload = _post(client, {"message": "send 5k to Ada"})

    assert payload["response"] == "I could not complete that. Try again."
    # No STATE was read, so no decision and no receipt may be claimed.
    assert payload["decision"] is None
    assert payload["receipt"] is None
    assert payload["confirm_id"] == ""


def test_an_inflow_webhook_fails_retryably_when_the_ledger_is_unreachable(monkeypatch):
    """The rail must retry: a 200 would say the payment had been recorded."""
    _wire_unavailable_ledger(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/money/inflow",
        headers={"Authorization": "Bearer test-token"},
        json={"payment_id": "pay_down", "amount": "420000"},
    )
    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]


# ---------------------------------------------------------------------------
# The inflow amount is a client-input boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["abc", {}, True, "nan", "1e400", 0, -5, "-100", []],
    ids=[
        "text",
        "object",
        "bool",
        "nan",
        "overflow",
        "zero",
        "negative",
        "neg-str",
        "list",
    ],
)
def test_the_inflow_endpoint_rejects_an_amount_it_cannot_trust(monkeypatch, bad):
    """A payload that cannot be parsed is the caller's error, not a 500.

    A 500 tells the rail the server failed and invites it to retry a payload
    that can never succeed. It also used to accept the classes that do not raise
    on parse (``nan``) and the ones that are not a credit at all (zero, negative),
    which still wrote an executed receipt.
    """
    ledger = _ledger(spendable="0")
    _store, rail, _memory = _wire(monkeypatch, ledger=ledger)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/money/inflow",
        headers={"Authorization": "Bearer test-token"},
        json={"payment_id": "pay_bad", "amount": bad},
    )

    assert response.status_code == 400, response.text
    assert "amount" in response.json()["detail"]
    assert rail.moves == []


def test_a_good_amount_still_splits(monkeypatch):
    """The guard must not have closed the door on a real credit."""
    ledger = _ledger(spendable="0", rent_required="150000")
    _store, _rail, _memory = _wire(monkeypatch, ledger=ledger)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/money/inflow",
        headers={"Authorization": "Bearer test-token"},
        json={"payment_id": "pay_ok", "amount": "420000"},
    )
    assert response.status_code == 200, response.text


def test_a_missing_amount_is_still_its_own_error(monkeypatch):
    """The original message survives: absent is not the same as unparseable."""
    ledger = _ledger(spendable="0")
    _wire(monkeypatch, ledger=ledger)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/money/inflow",
        headers={"Authorization": "Bearer test-token"},
        json={"payment_id": "pay_missing"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "amount is required"


# ---------------------------------------------------------------------------
# A decline needs the same typed refusal as everything else
# ---------------------------------------------------------------------------


def test_a_declined_tap_says_so_when_the_ledger_is_unreachable(monkeypatch):
    """A decline touches the ledger, so an outage must not become a 500.

    ``handle_confirm(yes=False)`` does not go through ``handle``, so it missed
    the typed handling; the exception escaped as a 500 on /chat and as the raw
    exception text in the stream's error frame.
    """
    _wire_unavailable_ledger(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)

    payload = _post(client, {"confirm_id": "confirm_x", "yes": False, "message": ""})

    assert payload["response"] == "I could not complete that. Try again."
    assert payload["receipt"] is None
    assert payload["confirm_id"] == ""

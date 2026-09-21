"""The money-turn response contract, pinned against a fixture.

The Go client owns the other side of this, so the shape is not allowed to drift
quietly. ``tests/fixtures/money_layers/response_contract.json`` records it, the
live endpoint is asserted against that file here, and Go can pin its
``PythonChatResponse`` struct and its own tests against the same file.

The two rules that matter:

* ``confirm_id``, ``decision`` and ``receipt`` are **always** present, and
  ``receipt`` is ``null`` rather than absent when nothing moved, so a client can
  parse the payload without a conditional, and
* ``requires_confirmation`` and ``cards`` are **never** present. The old approval
  card protocol is deleted, and a client that still looks for it must fail loudly
  rather than silently never confirm anything.
"""

from __future__ import annotations

import json
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
    / "response_contract.json"
)
CONTRACT: dict[str, Any] = json.loads(FIXTURE.read_text())

USER_ID = "u-contract"


class RecordingRail(InMemoryRail):
    def __init__(self) -> None:
        super().__init__()
        self.moves: list[dict[str, Any]] = []

    async def execute(self, instruction: TransferInstruction):  # noqa: ANN201
        self.moves.append({"amount": str(instruction.amount)})
        return await super().execute(instruction)


class StubMemory:
    async def ensure_user(self, user: Any) -> None:  # noqa: ARG002
        return None

    async def get_conversation(self, conversation_id: str) -> None:  # noqa: ARG002
        return None

    async def store_interaction(self, **kwargs: Any) -> None:
        return None

    async def get_conversation_history(self, *args: Any, **kwargs: Any) -> list:
        return []


def _wire(monkeypatch, *, spendable: str, rent_required: str = "0", judge=None):
    ledger = Ledger(
        user_id=USER_ID,
        track=Track(name="70/30", spend=Decimal("70"), save=Decimal("30")),
    )
    ledger.sleeves = {
        "spendable": money(spendable),
        "savings": money(0),
        "yield": money(0),
        "locked": money(0),
    }
    ledger.rent_first = RentFirst(
        required=money(rent_required), reserved=money(0), due_in_days=9
    )
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
    monkeypatch.setattr(chat._validator, "validate_rate_limit", _async_true)

    user = User()
    user.id = USER_ID
    user.username = "contract"
    user.email = "c@test.invalid"
    user.full_name = "Contract"
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
    return rail


async def _async_true(*args: Any, **kwargs: Any) -> bool:
    return True


def _chat(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/api/v1/chat", headers={"Authorization": "Bearer test-token"}, json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------


def test_the_fixture_records_the_forbidden_keys_as_forbidden():
    """The contract file itself, so a silent edit is caught by review."""
    assert CONTRACT["forbidden_keys"] == ["requires_confirmation", "cards"]
    assert "receipt" in CONTRACT["keys"]
    assert "confirm_id" in CONTRACT["keys"]
    assert "decision" in CONTRACT["keys"]


def test_a_money_turn_always_carries_the_three_keys(monkeypatch):
    _wire(monkeypatch, spendable="90000")
    client = TestClient(app, raise_server_exceptions=False)

    payload = _chat(client, {"message": "send 5k to Ada"})

    assert set(CONTRACT["keys"]) <= set(payload)
    assert payload["confirm_id"]
    assert payload["decision"]["next_mode"] == "ask"
    # Nothing moved, so the receipt is present and null.
    assert payload["receipt"] is None


def test_an_executed_money_turn_carries_a_receipt(monkeypatch):
    _wire(monkeypatch, spendable="90000")
    client = TestClient(app, raise_server_exceptions=False)

    issued = _chat(client, {"message": "send 5k to Ada"})
    settled = _chat(
        client, {"confirm_id": issued["confirm_id"], "yes": True, "message": ""}
    )

    assert set(CONTRACT["keys"]) <= set(settled)
    assert settled["receipt"] is not None
    assert settled["receipt"]["status"] == "executed"
    assert settled["receipt"]["idempotent_replay"] is False


def test_a_money_turn_never_has_the_old_card_protocol(monkeypatch):
    _wire(monkeypatch, spendable="90000")
    client = TestClient(app, raise_server_exceptions=False)

    for payload in (
        _chat(client, {"message": "send 5k to Ada"}),
        _chat(client, {"message": "can I afford 200k for a car"}),
        _chat(client, {"message": "lock 5k"}),
    ):
        for key in CONTRACT["forbidden_keys"]:
            assert key not in payload, key


def test_the_recorded_examples_match_the_contract(monkeypatch):
    """The examples in the fixture are real payloads, so Go can pin them.

    Every example is checked for the required keys and the forbidden ones, which
    is what makes it usable as a pin rather than as documentation.
    """
    for name, example in CONTRACT["examples"].items():
        assert set(CONTRACT["keys"]) <= set(example), name
        for key in CONTRACT["forbidden_keys"]:
            assert key not in example, (name, key)
        assert isinstance(example["decision"], dict), name
        # A decision always carries the typed reasons the reply was written from.
        assert "reasons" in example["decision"], name
        assert "next_mode" in example["decision"], name


def _meaningful(payload: dict[str, Any]) -> dict[str, Any]:
    """The part of a payload a client actually reads, with ids and clocks removed.

    Recorded examples are verbatim responses, so the comparisons here ignore the
    fields that differ on every run and pin everything else.
    """
    decision = payload.get("decision") or {}
    receipt = payload.get("receipt") or {}
    return {
        "confirm_id": bool(payload.get("confirm_id")),
        "next_mode": decision.get("next_mode"),
        "action_choice": decision.get("action_choice"),
        "reasons": decision.get("reasons"),
        "affordability": decision.get("affordability"),
        "policy_violation": decision.get("policy_violation"),
        "suggested_amount": decision.get("suggested_amount"),
        "receipt_status": receipt.get("status"),
        "receipt_reasons": receipt.get("reasons"),
        "receipt_amount": receipt.get("amount"),
        "receipt_counterparty": receipt.get("counterparty"),
        "receipt_sleeves_before": receipt.get("sleeves_before"),
        "receipt_sleeves_after": receipt.get("sleeves_after"),
    }


@pytest.mark.parametrize(
    "name,spendable,rent,judge_kwargs,message",
    [
        ("confirmation_required", "90000", "0", {"afford": 0.9}, "send 5k to Ada"),
        (
            "rejected",
            "184000",
            "120000",
            {"afford": 0.21, "violation": 0.9},
            "send 95k to Ada",
        ),
    ],
)
def test_a_recorded_example_is_reproduced_live(
    monkeypatch, name, spendable, rent, judge_kwargs, message
):
    """The pin cannot rot: each example is re-run and compared field by field."""
    _wire(
        monkeypatch,
        spendable=spendable,
        rent_required=rent,
        judge=judge_of(jev(intent="order", **judge_kwargs)),
    )
    client = TestClient(app, raise_server_exceptions=False)
    live = _chat(client, {"message": message})
    recorded = CONTRACT["examples"][name]

    assert set(CONTRACT["keys"]) <= set(live)
    assert _meaningful(live) == _meaningful(recorded)


def test_the_executed_example_is_reproduced_live(monkeypatch):
    _wire(monkeypatch, spendable="90000")
    client = TestClient(app, raise_server_exceptions=False)

    issued = _chat(client, {"message": "send 5k to Ada"})
    live = _chat(
        client, {"confirm_id": issued["confirm_id"], "yes": True, "message": ""}
    )
    recorded = CONTRACT["examples"]["executed"]

    assert _meaningful(live) == _meaningful(recorded)


def test_the_inflow_contract_matches_the_endpoint(monkeypatch):
    _wire(monkeypatch, spendable="0", rent_required="150000")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/money/inflow",
        headers={"Authorization": "Bearer test-token"},
        json={
            "payment_id": "pay_contract",
            "amount": "420000",
            "source_raw": "PAYROLL",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    recorded = CONTRACT["inflow_endpoint"]
    assert set(recorded["response_keys"]) <= set(payload)
    assert payload["receipt"]["status"] == "executed"

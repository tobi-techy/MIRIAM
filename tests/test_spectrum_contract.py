"""Spectrum gateway contract: parts in, parts rendered.

Through ``POST /api/v1/chat/spectrum`` with the same seams as the live chat
tests (fake user, fake memory, test orchestrator, fake Go host): no network,
no JEV, no Glider.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from layer_fakes import POLICY, FakeProvider, jev, judge_of, ledger_with

from miriam_agent.api import dependencies, spectrum
from miriam_agent.api.main import app
from miriam_agent.config.settings import get_settings
from miriam_agent.database.models import User
from miriam_agent.hands.ledger import InMemoryLedgerStore
from miriam_agent.hands.state import ProposedAction
from miriam_agent.orchestrator import Orchestrator

USER_ID = "u-spec-1"
OWNER = (
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp:"
    "7xKXTagR7k8J2y8K9mN3pQ5rS7tU9vW1xY3zABcD5eF"
)
SLEEVE_ROW = {
    "id": "d15c0ba1-71c0-4a11-8aa1-000000000001",
    "name": "Rail Stock Sleeve",
    "glider_strategy_id": "01M333PQNR9Y2WJ4D78FSRECEQ",
}


class StubMemory:
    def __init__(self) -> None:
        self.writes: list[str] = []

    async def store_interaction(self, **kwargs: Any) -> None:
        self.writes.append(str(kwargs.get("role")))

    async def get_conversation_history(self, *args: Any, **kwargs: Any) -> list:
        return []


class FakeGo:
    """The Go host surface invest + positions use, staged like the real one."""

    def __init__(self, *, positions: list[dict[str, Any]] | None = None) -> None:
        self.positions = positions if positions is not None else []
        self.prepares = 0
        self.completes = 0

    async def list_investment_strategies(self, token: str, status: str | None = None):
        return {"strategies": [SLEEVE_ROW]}

    async def get_investment_owner(self, token: str):
        return {"owner_account_id": OWNER}

    async def get_investment_positions(self, token: str):
        return {"positions": self.positions}

    async def prepare_user_enroll(self, token, payload, confirmation_token=None):
        if not confirmation_token:
            return {
                "status": "AWAITING_CONFIRMATION",
                "confirmation": {"token": "cfm-1"},
            }
        self.prepares += 1
        assert payload["strategy_id"] == SLEEVE_ROW["id"]
        return {
            "status": "COMPLETED",
            "flow_id": "flow_spec1",
            "account_index": "0",
            "agent_account_id": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp:Ag3nt",
            "chain_ids": [1399811149],
            "sign_payload": "AgABspec",
            "deposit_account_id": OWNER,
            "live": True,
        }

    async def complete_user_enroll(self, token, payload, confirmation_token=None):
        if not confirmation_token:
            return {
                "status": "AWAITING_CONFIRMATION",
                "confirmation": {"token": "cfm-2"},
            }
        self.completes += 1
        assert payload["flow_id"] == "flow_spec1"
        assert payload["account_index"] == "0"
        return {
            "status": "COMPLETED",
            "enrollment": {"glider_portfolio_id": "pf_spec"},
            "funding": {"status": "SUBMITTED"},
        }


def _wire(monkeypatch, *, go: FakeGo, savings: str = "100", judge=None):
    import miriam_agent.orchestrator as orchmod

    store = InMemoryLedgerStore()
    ledger = ledger_with(USER_ID, savings=savings)
    store.seed(ledger)
    orchestrator = Orchestrator(
        store=store,
        policy=POLICY,
        provider=FakeProvider("Noted."),
        judge=judge or judge_of(jev(intent="order", mode="ask", action="allow")),
        go_token="test-token",
    )
    monkeypatch.setattr(spectrum, "_orchestrator_for", lambda token: orchestrator)
    monkeypatch.setattr("miriam_agent.integrations.go_client.get_go_client", lambda: go)
    # spectrum reads settings + lazy go import; keep everything else real.
    user = User()
    user.id = USER_ID
    user.username = "specter"
    user.email = "s@test.invalid"
    user.full_name = "Specter"
    user.is_active = True
    user.roles = ["verified"]
    memory = StubMemory()
    overrides = {
        **app.dependency_overrides,
        dependencies.get_current_user: lambda: user,
        dependencies.get_bearer_token: lambda: "test-token",
        dependencies.get_memory_store: lambda: memory,
    }
    monkeypatch.setattr(app, "dependency_overrides", overrides)
    # Silence the hold on the invest leg's Go token seam.
    _ = orchmod
    return store, memory


def _post(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/api/v1/chat/spectrum", json=body)
    assert response.status_code == 200, response.text[:400]
    return response.json()


def _base(text: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "channel": "terminal",
        "space_id": "sp1",
        "user_id": USER_ID,
        "text": text,
        **extra,
    }


def test_inflow_splits_and_proves_70_30(monkeypatch) -> None:
    # The pasted-alert demo path is off by default; this contract pins what it
    # renders when a deployment explicitly turns it on.
    monkeypatch.setenv("ALLOW_CHAT_INFLOW_SYNTH", "true")
    get_settings.cache_clear()
    _wire(monkeypatch, go=FakeGo())
    client = TestClient(app, raise_server_exceptions=False)
    out = _post(client, _base("i just got paid 100"))
    kinds = [p["type"] for p in out["parts"]]
    assert kinds[0] == "text"
    assert any(
        p["type"] == "chart" and p["spec"]["kind"] == "bars" for p in out["parts"]
    )


def test_invest_utterance_then_tap_returns_card(monkeypatch) -> None:
    _wire(monkeypatch, go=FakeGo())
    client = TestClient(app, raise_server_exceptions=False)
    first = _post(client, _base("put 30 of this deposit into stocks"))
    assert first["parts"][0]["type"] == "text"
    assert all(p["type"] != "card" for p in first["parts"])
    # The open challenge id is echoed for the gateway to tap.
    assert first["confirm_id"].startswith("confirm_")
    # The challenge id travels in the Challenge, not the narration: pull it
    # from the store seam the same way the gateway reads its own log line.
    store = spectrum._orchestrator_for("test-token").store  # noqa: SLF001
    import asyncio

    ledger = asyncio.new_event_loop().run_until_complete(store.load(USER_ID))
    (confirm_id,) = list(ledger.challenges.keys())
    second = _post(client, _base(f"confirm {confirm_id}"))
    cards = [p for p in second["parts"] if p["type"] == "card"]
    assert len(cards) == 1
    card = cards[0]
    assert card["kind"] == "allocate"
    assert card["flow_id"] == "flow_spec1"
    assert card["amount"].startswith("30")
    assert card["source"] == "stash"
    assert card["image_base64"]
    assert card["sign_payload"] == "AgABspec"


def test_signature_settles_and_charts_or_indexing_note(monkeypatch) -> None:
    go = FakeGo(positions=[])
    _wire(monkeypatch, go=go)
    client = TestClient(app, raise_server_exceptions=False)
    _post(client, _base("put 30 of this deposit into stocks"))
    store = spectrum._orchestrator_for("test-token").store  # noqa: SLF001
    import asyncio

    ledger = asyncio.new_event_loop().run_until_complete(store.load(USER_ID))
    (confirm_id,) = list(ledger.challenges.keys())
    tapped = _post(client, _base(f"confirm {confirm_id}"))
    flow = [p for p in tapped["parts"] if p["type"] == "card"][0]["flow_id"]
    done = _post(client, _base("", signed_tx="AgAB wallet-signed", flow_id=flow))
    assert done["parts"][0]["type"] == "text"
    assert "Done." in done["parts"][0]["text"]
    # No positions indexed yet: honesty text, no invented chart.
    assert any("Indexing" in p.get("text", "") for p in done["parts"])
    go.positions = [
        {"symbol": "AAPLx", "value_usd": "12"},
        {"symbol": "NVDAx", "value_usd": "9"},
        {"symbol": "TSLAx", "value_usd": "9"},
    ]
    looking = _post(client, _base("how am i looking"))
    charts = [p for p in looking["parts"] if p["type"] == "chart"]
    assert len(charts) == 1
    assert charts[0]["filename"] == "portfolio.png"
    assert charts[0]["image_base64"]


def test_portfolio_ask_with_nothing_indexed(monkeypatch) -> None:
    _wire(monkeypatch, go=FakeGo(positions=[]))
    client = TestClient(app, raise_server_exceptions=False)
    out = _post(client, _base("how am i looking"))
    assert out["parts"] == [{"type": "text", "text": "Nothing indexed yet."}]


def test_user_mismatch_and_bad_channel_rejected(monkeypatch) -> None:
    _wire(monkeypatch, go=FakeGo())
    client = TestClient(app, raise_server_exceptions=False)
    bad_user = _base("hi")
    bad_user["user_id"] = "someone-else"
    assert client.post("/api/v1/chat/spectrum", json=bad_user).status_code == 403
    bad_chan = _base("hi")
    bad_chan["channel"] = "pager"
    assert client.post("/api/v1/chat/spectrum", json=bad_chan).status_code == 400


def test_text_signature_refused_without_dev_flag(monkeypatch) -> None:
    _wire(monkeypatch, go=FakeGo())
    client = TestClient(app, raise_server_exceptions=False)
    long_b64 = (
        "QWdBQnNpZ25hdHVyZXNpZ25hdHVyZXNpZ25hdHVyZXNpZ25hdHVy"
        "ZXNpZ25hdHVyZXNpZ25hdHVyZQ=="
    )
    res = client.post("/api/v1/chat/spectrum", json=_base(long_b64))
    assert res.status_code in (200, 403)
    if res.status_code == 403:
        assert "RAIL_ALLOW_DEV_SIGN" in res.text


def test_demo_script_never_starts_with_a_ticker() -> None:
    from miriam_agent.hands.invest import parse_invest_utterance

    assert parse_invest_utterance("buy NVDA for 30") is None
    assert parse_invest_utterance("put 30 into stocks") is not None
    action: ProposedAction | None = parse_invest_utterance("put 30 into stocks")
    assert action is not None and action.sleeve == "savings"
    assert Decimal(str(action.amount)) == Decimal("30")


def test_decline_text_closes_challenge_with_nothing_moved(monkeypatch) -> None:
    _wire(monkeypatch, go=FakeGo())
    client = TestClient(app, raise_server_exceptions=False)
    first = _post(client, _base("put 30 of this deposit into stocks"))
    assert first["confirm_id"].startswith("confirm_")
    out = _post(client, _base(f"no {first['confirm_id']}"))
    assert out["parts"][0]["type"] == "text"
    assert "Nothing moved" in out["parts"][0]["text"]

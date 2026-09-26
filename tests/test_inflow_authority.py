"""Inflow authority: only the rail mints ledger money.

The rules under test:

A1. ``POST /money/inflow`` requires the rail service key on top of the user
    JWT; a user token alone (or any wrong key) is refused and the ledger is
    untouched.
A2. With ``RAIL_SERVICE_KEY`` unset the endpoint refuses closed (503), not
    open.
A3. Chat text that looks like a payment alert is inert by default: without
    ``ALLOW_CHAT_INFLOW_SYNTH`` no text turn ever produces an
    ``inflow_split``.
A4. The production settings guard fires for any production spelling
    ("Production", "prod", "PRODUCTION"), not just the exact lowercase
    string, and refuses ``ALLOW_CHAT_INFLOW_SYNTH`` in production.
A5. ``decode_token`` has no secret override: a token signed with any other
    secret is rejected.
"""

from __future__ import annotations

import pathlib
import sys
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from layer_fakes import FakeProvider, jev, judge_of  # noqa: E402

from miriam_agent.api import chat, dependencies  # noqa: E402
from miriam_agent.api.main import app  # noqa: E402
from miriam_agent.config.settings import Settings, get_settings  # noqa: E402
from miriam_agent.database.models import User  # noqa: E402
from miriam_agent.hands.ledger import (  # noqa: E402
    InMemoryLedgerStore,
    Ledger,
    Track,
    money,
)
from miriam_agent.hands.limits import Policy  # noqa: E402
from miriam_agent.hands.transfer import InMemoryRail  # noqa: E402
from miriam_agent.orchestrator import Orchestrator  # noqa: E402

USER_ID = "u-inflow-auth"
SERVICE_KEY = "test-rail-service-key-0123456789abcdef"


def _ledger() -> Ledger:
    ledger = Ledger(
        user_id=USER_ID,
        track=Track(name="70/30", spend=Decimal("70"), save=Decimal("30")),
    )
    ledger.sleeves = {
        "spendable": money("50000"),
        "savings": money("0"),
        "yield": money("0"),
        "locked": money("0"),
    }
    return ledger


def _wire(monkeypatch, *, ledger: Ledger):
    store = InMemoryLedgerStore()
    store.seed(ledger)
    orchestrator = Orchestrator(
        store=store,
        policy=Policy(
            max_auto=money(2000),
            max_with_confirm=money(100000),
            reversible_under=money(2000),
        ),
        rail=InMemoryRail(),
        provider=FakeProvider("Noted."),
        judge=judge_of(jev(intent="order", afford=0.9)),
    )
    monkeypatch.setattr(chat, "_orchestrator_for", lambda token: orchestrator)
    monkeypatch.setattr(chat._validator, "validate_rate_limit", _async_true)

    user = User()
    user.id = USER_ID
    user.username = "tester"
    user.roles = ["verified"]
    memory = _StubMemory()
    overrides = {
        **app.dependency_overrides,
        dependencies.get_current_user: lambda: user,
        dependencies.get_bearer_token: lambda: "test-token",
        dependencies.get_memory_store: lambda: memory,
        dependencies.get_supermemory_memory_dep: lambda: None,
    }
    monkeypatch.setattr(app, "dependency_overrides", overrides)
    return store, memory


class _StubMemory:
    async def ensure_user(self, user: Any) -> None:  # noqa: ARG002
        return None

    async def get_conversation(self, conversation_id: str) -> None:  # noqa: ARG002
        return None

    async def store_interaction(self, **kwargs: Any) -> None:
        return None

    async def get_conversation_history(self, *args: Any, **kwargs: Any) -> list:
        return []


async def _async_true(*args: Any, **kwargs: Any) -> bool:
    return True


def _inflow(client: TestClient, *, key: str | None = SERVICE_KEY) -> Any:
    headers = {"Authorization": "Bearer test-token"}
    if key is not None:
        headers["X-Rail-Service-Key"] = key
    return client.post(
        "/api/v1/money/inflow",
        headers=headers,
        json={"payment_id": "pay_auth", "amount": "420000", "source_raw": "PAYROLL"},
    )


# ---------------------------------------------------------------------------
# A1: the service key is required on top of the user JWT
# ---------------------------------------------------------------------------


def test_inflow_without_the_service_key_is_refused(monkeypatch):
    store, _memory = _wire(monkeypatch, ledger=_ledger())
    client = TestClient(app, raise_server_exceptions=False)

    response = _inflow(client, key=None)

    assert response.status_code == 401, response.text
    reloaded = _sync_load(store)
    assert reloaded.sleeves["spendable"] == money("50000")
    assert reloaded.sleeves["savings"] == money("0")


def test_inflow_with_a_wrong_service_key_is_refused(monkeypatch):
    store, _memory = _wire(monkeypatch, ledger=_ledger())
    client = TestClient(app, raise_server_exceptions=False)

    response = _inflow(client, key="not-the-rail-service-key")

    assert response.status_code == 401, response.text
    reloaded = _sync_load(store)
    assert reloaded.sleeves["spendable"] == money("50000")


# ---------------------------------------------------------------------------
# A2: unconfigured refuses closed
# ---------------------------------------------------------------------------


def test_inflow_without_a_configured_key_refuses_closed(monkeypatch):
    _wire(monkeypatch, ledger=_ledger())
    monkeypatch.delenv("RAIL_SERVICE_KEY", raising=False)
    get_settings.cache_clear()
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = _inflow(client)
        assert response.status_code == 503, response.text
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# A3: chat text is not a payment fact
# ---------------------------------------------------------------------------


def test_a_pasted_alert_is_inert_by_default(monkeypatch):
    store, _memory = _wire(monkeypatch, ledger=_ledger())
    assert not get_settings().ALLOW_CHAT_INFLOW_SYNTH
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/chat",
        headers={"Authorization": "Bearer test-token"},
        json={"message": "Credit alert: NGN420,000.00 from ACME PAYROLL"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    receipt = payload.get("receipt")
    assert receipt is None or receipt.get("action") != "inflow_split"
    reloaded = _sync_load(store)
    assert reloaded.sleeves["spendable"] == money("50000")
    assert reloaded.sleeves["savings"] == money("0")


# ---------------------------------------------------------------------------
# A4: the production guard covers every spelling
# ---------------------------------------------------------------------------


def _production_settings(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ENVIRONMENT": "production",
        "JWT_SECRET": "j" * 40,
        "SECRET_KEY": "s" * 40,
        "ENCRYPTION_KEY": "e" * 40,
        "JWT_AUDIENCE": "miriam-api",
        "JWT_ISSUER": "rail-backend",
        "ALLOWED_ORIGINS": "https://app.example.com",
        "DATABASE_URL": "postgresql+asyncpg://miriam:real-pw-123456789@localhost/m",
        "RAIL_SERVICE_KEY": "r" * 40,
        "_env_file": None,
    }
    base.update(extra)
    return base


@pytest.mark.parametrize("spelling", ["production", "Production", "PRODUCTION", "prod"])
def test_production_guard_fires_for_every_spelling(spelling):
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(**_production_settings(ENVIRONMENT=spelling, JWT_SECRET="short"))


def test_production_guard_refuses_chat_inflow_synth():
    with pytest.raises(ValueError, match="ALLOW_CHAT_INFLOW_SYNTH"):
        Settings(**_production_settings(ALLOW_CHAT_INFLOW_SYNTH=True))


def test_production_guard_refuses_a_missing_rail_service_key():
    with pytest.raises(ValueError, match="RAIL_SERVICE_KEY"):
        Settings(**_production_settings(RAIL_SERVICE_KEY="dev-rail-key"))


def test_staging_with_a_weak_still_nonproduction_spelling_is_not_guarded():
    # Sanity: development spellings remain untouched so local runs work.
    settings = Settings(
        **_production_settings(ENVIRONMENT="staging", JWT_SECRET="short")
    )
    assert settings.JWT_SECRET == "short"


# ---------------------------------------------------------------------------
# A5: no secret override on the verify path
# ---------------------------------------------------------------------------


def test_decode_token_rejects_a_token_signed_with_another_secret():
    from datetime import UTC, datetime, timedelta

    import jwt as pyjwt

    from miriam_agent.core.exceptions import AuthenticationError

    forged = pyjwt.encode(
        {
            "sub": "u1",
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        "an-attacker-secret-not-the-configured-one",
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationError):
        _decode(forged)


def test_decode_and_create_have_no_secret_override():
    import inspect

    from miriam_agent.auth import jwt as jwt_module

    for name in ("decode_token", "create_token"):
        params = inspect.signature(getattr(jwt_module, name)).parameters
        assert "secret" not in params, f"{name} still carries a secret override"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _decode(token: str) -> dict[str, Any]:
    from miriam_agent.auth.jwt import decode_token

    return decode_token(token)


def _sync_load(store: InMemoryLedgerStore) -> Ledger:
    import asyncio

    async def _load() -> Ledger | None:
        ledger = await store.load(USER_ID)
        assert ledger is not None
        return ledger

    return asyncio.new_event_loop().run_until_complete(_load())

"""Regression coverage for the session wrapper used by Go-delegated chat.

Keep MemoryStore methods real; mock only the SQLAlchemy session and external
services so a recursive wrapper cannot hide behind a fake memory store.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from miriam_agent.database.memory import MemoryStore
from miriam_agent.database.models import User


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def session_store():
    store = MemoryStore("unused-no-database-connection")
    session = MagicMock(spec=AsyncSession)
    session.get.return_value = None
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=session)
    manager.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=manager)
    store.async_session = factory
    return store, session, manager, factory


def test_session_requires_initialization():
    async def check():
        with pytest.raises(RuntimeError, match="initialize"):
            async with MemoryStore("unused")._session():
                pytest.fail("Uninitialized store yielded a session")

    _run(check())


def test_session_uses_factory_and_closes(session_store):
    store, session, manager, factory = session_store

    async def check():
        async with store._session() as actual:
            assert actual is session

    _run(check())
    factory.assert_called_once_with()
    manager.__aenter__.assert_awaited_once_with()
    manager.__aexit__.assert_awaited_once_with(None, None, None)


def test_session_closes_and_propagates_error(session_store):
    store, _, manager, factory = session_store
    error = ValueError("test failure")

    async def check():
        with pytest.raises(ValueError) as caught:
            async with store._session():
                raise error
        assert caught.value is error

    _run(check())
    factory.assert_called_once_with()
    manager.__aexit__.assert_awaited_once()
    assert manager.__aexit__.await_args.args[:2] == (ValueError, error)


@pytest.mark.parametrize("existing", [False, True])
def test_ensure_user_uses_session(session_store, existing):
    store, session, _, factory = session_store
    user = User(id="guest-id", username="guest_test", email="guest@test.invalid")
    session.get.return_value = user if existing else None

    _run(store.ensure_user(user))

    factory.assert_called_once_with()
    session.get.assert_awaited_once_with(User, user.id)
    if existing:
        session.add.assert_not_called()
        session.commit.assert_not_awaited()
    else:
        row = session.add.call_args.args[0]
        assert (row.id, row.username, row.email) == (user.id, user.username, user.email)
        session.commit.assert_awaited_once_with()


@pytest.mark.parametrize("is_poll_vote", [False, True])
def test_guest_chat_reaches_onboarding_and_persists(
    session_store, monkeypatch, is_poll_vote
):
    from miriam_agent.api import chat, dependencies
    from miriam_agent.api.main import app
    from miriam_agent.auth import jwt
    from miriam_agent.config.settings import Settings
    from miriam_agent.onboarding.service import OnboardingTurn

    store, session, _, factory = session_store
    settings = Settings(_env_file=None, JWT_SECRET="local-regression-secret-not-live")
    monkeypatch.setattr(jwt, "get_settings", lambda: settings)
    uid = "00000000-0000-5000-8000-000000000001"
    token = jwt.create_token(
        uid,
        claims={
            "user_id": uid,
            "username": "guest_test",
            "email": "guest@test.invalid",
            "role": "guest",
            "token_type": "agent",
            "iss": "rail_service",
        },
    )
    turn = OnboardingTurn(
        took_over=True,
        response="What are you saving for?",
        conversation_id=f"onboarding:{uid}",
        stage="interview",
    )
    handle = AsyncMock(return_value=turn)
    monkeypatch.setattr(chat.OnboardingService, "handle_turn", handle)
    monkeypatch.setattr(
        chat._validator, "validate_rate_limit", AsyncMock(return_value=True)
    )
    overrides = {
        **app.dependency_overrides,
        dependencies.get_memory_store: lambda: store,
        dependencies.get_supermemory_memory_dep: lambda: None,
    }
    monkeypatch.setattr(app, "dependency_overrides", overrides)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/v1/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "message": "Save for a home",
            "conversation_id": "platform:imessage:test",
            "is_poll_vote": is_poll_vote,
            "poll_title": "Your goal?",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["response"] == turn.response
    assert payload["onboarding"]["stage"] == "interview"
    # The approval-card protocol is deleted; onboarding carries neither key.
    assert "cards" not in payload
    assert "requires_confirmation" not in payload
    handle.assert_awaited_once()
    assert handle.await_args.args[0].roles == ["guest"]
    assert handle.await_args.kwargs["is_poll_vote"] is is_poll_vote
    # ensure_user + ownership lookup + both interaction writes + history read
    # use the real wrapper.
    assert factory.call_count == 5
    assert session.commit.await_count == 3

"""Continuity: close/reinstall/number-change must not lose the thread.

Pins the three fixes that make "continue where I stopped" true:

1. ``POST /api/v1/chat/stream`` persists the streamed agent turn through the
   same finalize path as ``/chat`` (it used to stream tokens and forget them).
2. A turn with no ``conversation_id`` (fresh install) still loads history from
   the ``conv_{user_id}`` default instead of answering with ``[]``.
3. Channel handles bind to the stable JWT user; ``GET /conversations/latest``
   resumes with one call; ``POST /users/merge`` (rail-authenticated) moves
   portable history after a verified number change.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from miriam_agent.api import chat, dependencies
from miriam_agent.api.main import app
from miriam_agent.database.memory import MemoryStore
from miriam_agent.database.models import User
from miriam_agent.onboarding.service import OnboardingTurn


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mock_store(session) -> MemoryStore:
    """A MemoryStore wired to a mocked AsyncSession (no live DB)."""
    from unittest.mock import MagicMock

    assert isinstance(session, MagicMock)
    store = MemoryStore("unused-no-database-connection")
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=session)
    manager.__aexit__ = AsyncMock(return_value=False)
    store.async_session = MagicMock(return_value=manager)
    return store


def _mock_session() -> Any:
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy.ext.asyncio import AsyncSession

    session = MagicMock(spec=AsyncSession)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    return session


def _user(uid: str = "user-continuity-1") -> User:
    user = User()
    user.id = uid
    user.username = uid
    user.email = f"{uid}@test.invalid"
    user.full_name = "Continuity Tester"
    user.is_active = True
    user.roles = ["user"]
    return user


class _RecordingMemory:
    """Minimal memory double that records writes and serves canned history."""

    def __init__(self, history: list | None = None):
        self.writes: list[tuple[str, str]] = []
        self.history = history or []
        self.history_calls: list[str | None] = []

    async def ensure_user(self, user: Any) -> None:
        return None

    async def get_conversation(self, conversation_id: str) -> None:
        return None

    async def store_interaction(self, **kwargs: Any) -> str:
        self.writes.append((str(kwargs.get("role")), str(kwargs.get("content"))))
        return str(kwargs.get("conversation_id") or "conv_x")

    async def get_conversation_history(self, *args: Any, **kwargs: Any) -> list:
        self.history_calls.append(args[0] if args else None)
        return self.history


class _StreamAgent:
    """Fake agent loop: two tokens, then done with the full text."""

    async def stream_run(self, **kwargs: Any):  # noqa: ANN201
        yield {"type": "token", "content": "Hello "}
        yield {"type": "token", "content": "again"}
        yield {"type": "done", "content": "Hello again"}


def _base_overrides(user: User, memory: Any) -> dict:
    return {
        **app.dependency_overrides,
        dependencies.get_current_user: lambda: user,
        dependencies.get_bearer_token: lambda: "test-token",
        dependencies.get_memory_store: lambda: memory,
        dependencies.get_supermemory_memory_dep: lambda: None,
    }


def _stub_turn_seams(monkeypatch, *, history: list | None = None):
    monkeypatch.setattr(
        chat._validator, "validate_rate_limit", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        chat._validator, "validate_user_input", AsyncMock(return_value=(True, []))
    )
    monkeypatch.setattr(
        chat.OnboardingService,
        "handle_turn",
        AsyncMock(return_value=OnboardingTurn(took_over=False)),
    )
    monkeypatch.setattr(chat, "_ingress_decision", AsyncMock(return_value=None))
    monkeypatch.setattr(chat, "_build_agent", lambda registry: _StreamAgent())
    monkeypatch.setattr(chat, "build_tool_registry", lambda: object())


# ---------------------------------------------------------------------------
# 1. Streamed turns persist
# ---------------------------------------------------------------------------


def test_streamed_agent_turn_is_persisted(monkeypatch):
    user = _user()
    memory = _RecordingMemory()
    monkeypatch.setattr(app, "dependency_overrides", _base_overrides(user, memory))
    _stub_turn_seams(monkeypatch)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/v1/chat/stream",
        headers={"Authorization": "Bearer test-token"},
        json={"message": "hello, are you still there?"},
    )
    assert response.status_code == 200, response.text
    body = response.text
    assert '"type": "token"' in body
    assert '"type": "done"' in body
    # Same two rows the non-stream path writes: the user's turn + the reply.
    roles = [role for role, _ in memory.writes]
    assert roles == ["user", "assistant"]
    assert memory.writes[0][1] == "hello, are you still there?"
    assert memory.writes[1][1] == "Hello again"
    # The closing frame carries the conversation so the app can resume it.
    assert "conv_user-continuity-1" in body


# ---------------------------------------------------------------------------
# 2. Fresh installs load the default conversation's history
# ---------------------------------------------------------------------------


def test_agent_inputs_falls_back_to_default_conversation_history(monkeypatch):
    user = _user()
    prior = [{"role": "user", "content": "i earn 500k monthly"}]
    memory = _RecordingMemory(history=prior)
    inputs = _run(
        chat._agent_inputs(
            memory_store=memory,  # type: ignore[arg-type]
            supermemory_memory=None,
            user=user,
            token="test-token",
            message="what should i do?",
            conversation_id=None,
            registry=object(),
        )
    )
    assert memory.history_calls == ["conv_user-continuity-1"]
    assert inputs["history"] == prior


# ---------------------------------------------------------------------------
# 3a. Resume endpoint (real sqlite store)
# ---------------------------------------------------------------------------


def test_latest_conversation_resumes_where_the_user_stopped(monkeypatch):
    from datetime import datetime
    from unittest.mock import MagicMock

    from miriam_agent.api.main import app as live_app

    user = _user("user-resume-1")

    conv = MagicMock()
    conv.id = "conv_b"
    conv.title = "Conversation 2026-01-01"
    conv.created_at = datetime(2026, 1, 1, 12, 0, 0)
    conv.updated_at = datetime(2026, 1, 2, 12, 0, 0)
    msg = MagicMock()
    msg.id = "m1"
    msg.role = "user"
    msg.content = "second"
    msg.extra_data = {}
    msg.created_at = datetime(2026, 1, 2, 12, 0, 0)

    memory = MagicMock()
    memory.get_conversations = AsyncMock(return_value=[conv])
    memory.get_conversation_messages = AsyncMock(return_value=[msg])

    overrides = {
        **live_app.dependency_overrides,
        dependencies.get_current_user: lambda: user,
        dependencies.get_memory_store: lambda: memory,
    }
    old = live_app.dependency_overrides
    live_app.dependency_overrides = overrides
    try:
        client = TestClient(live_app, raise_server_exceptions=False)
        response = client.get(
            "/api/v1/conversations/latest",
            headers={"Authorization": "Bearer test-token"},
        )
    finally:
        live_app.dependency_overrides = old
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["conversation"]["id"] == "conv_b"
    assert payload["messages"][-1]["content"] == "second"
    memory.get_conversations.assert_awaited_once_with("user-resume-1", limit=1)


def test_latest_conversation_empty_for_a_new_user(monkeypatch):
    from unittest.mock import MagicMock

    from miriam_agent.api.main import app as live_app

    user = _user("user-fresh-1")
    memory = MagicMock()
    memory.get_conversations = AsyncMock(return_value=[])

    overrides = {
        **live_app.dependency_overrides,
        dependencies.get_current_user: lambda: user,
        dependencies.get_memory_store: lambda: memory,
    }
    old = live_app.dependency_overrides
    live_app.dependency_overrides = overrides
    try:
        client = TestClient(live_app, raise_server_exceptions=False)
        response = client.get(
            "/api/v1/conversations/latest",
            headers={"Authorization": "Bearer test-token"},
        )
    finally:
        live_app.dependency_overrides = old
    assert response.status_code == 200, response.text
    assert response.json() == {"conversation": None, "messages": []}


# ---------------------------------------------------------------------------
# 3b. Identity linking + verified merge (real sqlite store)
# ---------------------------------------------------------------------------


def _scalars_result(*, first: Any = None, all: Any = None) -> Any:
    from unittest.mock import MagicMock

    result = MagicMock()
    scalars = MagicMock()
    scalars.first = MagicMock(return_value=first)
    scalars.all = MagicMock(return_value=all if all is not None else [])
    result.scalars = MagicMock(return_value=scalars)
    return result


def test_handle_conflict_needs_the_verified_merge_path():
    from unittest.mock import AsyncMock

    from miriam_agent.core.exceptions import AuthorizationError
    from miriam_agent.database.models import ChannelIdentity

    owned = ChannelIdentity(
        user_id="user-old-1", channel="whatsapp", handle="+15551234567"
    )
    session = _mock_session()
    session.execute = AsyncMock(
        return_value=_scalars_result(first=owned),
    )
    store = _mock_store(session)

    with pytest.raises(AuthorizationError):
        _run(store.link_identity("user-new-1", "whatsapp", "+15551234567"))


def test_handle_link_is_idempotent_for_the_owner():
    from unittest.mock import AsyncMock

    from miriam_agent.database.models import ChannelIdentity

    owned = ChannelIdentity(
        user_id="user-old-1", channel="whatsapp", handle="+15551234567"
    )
    session = _mock_session()
    session.execute = AsyncMock(
        return_value=_scalars_result(first=owned),
    )
    store = _mock_store(session)

    # Different formatting, same handle: rebinds, does not duplicate.
    row = _run(store.link_identity("user-old-1", "WhatsApp", "+1 (555) 123-4567"))
    assert row.user_id == "user-old-1"
    session.add.assert_not_called()


def test_merge_moves_portable_history_to_the_new_id():
    from unittest.mock import AsyncMock, MagicMock

    conv = MagicMock()
    conv.user_id = "user-old-2"
    mem = MagicMock()
    mem.user_id = "user-old-2"
    ident = MagicMock()
    ident.user_id = "user-old-2"

    session = _mock_session()
    session.execute = AsyncMock(
        side_effect=[
            _scalars_result(all=[conv]),
            _scalars_result(all=[mem]),
            _scalars_result(all=[ident]),
            _scalars_result(first=None),
            _scalars_result(first=None),
        ]
    )
    store = _mock_store(session)

    counts = _run(store.merge_user_data("user-old-2", "user-new-2"))
    assert counts == {"conversations": 1, "memories": 1, "identities": 1}
    assert conv.user_id == "user-new-2"
    assert mem.user_id == "user-new-2"
    assert ident.user_id == "user-new-2"
    session.commit.assert_awaited_once()


def test_merge_rejects_identical_ids():
    session = _mock_session()
    store = _mock_store(session)
    with pytest.raises(ValueError):
        _run(store.merge_user_data("user-x", "user-x"))

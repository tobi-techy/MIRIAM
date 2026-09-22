"""Ownership enforcement on the conversation read/write path.

``MemoryStore.get_conversation_history`` used to take only a conversation id
and do no ownership check, while ``POST /api/v1/chat`` passed the
*client-supplied* ``conversation_id`` straight in. An authenticated user who
presented another user's conversation id received that user's messages --
both injected into their agent context and returned in the
``conversation_history`` field of the response.

These tests pin the fixed behaviour on every layer: the ownership lookup, the
history read, the write path, and the endpoint guard.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.core.exceptions import AuthorizationError  # noqa: E402
from miriam_agent.database.memory import MemoryStore  # noqa: E402
from miriam_agent.database.models import Conversation, User  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _msg(role, content):
    m = MagicMock()
    m.id = f"msg-{content}"
    m.role = role
    m.content = content
    m.extra_data = {}
    m.created_at = datetime(2026, 1, 1, 12, 0, 0)
    return m


def _conversation(owner: str):
    conv = Conversation(user_id=owner, title="private chat")
    conv.id = "conv_shared_id"
    return conv


def _store(conversation, messages=()) -> MemoryStore:
    """A MemoryStore whose session returns ``conversation`` and ``messages``."""
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=conversation)
    result = MagicMock()
    result.scalars = MagicMock(return_value=list(messages))
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    store = MemoryStore("sqlite+aiosqlite:///:memory:")
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=session)
    manager.__aexit__ = AsyncMock(return_value=False)
    store.async_session = MagicMock(return_value=manager)
    return store


# ---------------------------------------------------------------------------
# Ownership lookup
# ---------------------------------------------------------------------------


def test_owned_conversation_returns_none_for_another_user():
    store = _store(_conversation("user-B-victim"))
    assert _run(store.get_owned_conversation("conv_shared_id", "user-A")) is None


def test_owned_conversation_returns_it_for_the_owner():
    store = _store(_conversation("user-B-victim"))
    owned = _run(store.get_owned_conversation("conv_shared_id", "user-B-victim"))
    assert owned is not None and owned.user_id == "user-B-victim"


def test_owned_conversation_returns_none_when_absent():
    store = _store(None)
    assert _run(store.get_owned_conversation("conv_missing", "user-A")) is None


# ---------------------------------------------------------------------------
# History read
# ---------------------------------------------------------------------------


def test_history_scoped_to_owner_returns_nothing_for_another_user():
    """User A asking for user B's conversation gets nothing back."""
    store = _store(_conversation("user-B-victim"), [_msg("user", "my salary is 5m")])
    assert _run(store.get_conversation_history("conv_shared_id", "user-A")) == []


def test_history_returns_messages_to_the_owner():
    store = _store(_conversation("user-B-victim"), [_msg("user", "my salary is 5m")])
    history = _run(store.get_conversation_history("conv_shared_id", "user-B-victim"))
    assert [h["content"] for h in history] == ["my salary is 5m"]


def test_history_unscoped_call_still_works_for_server_minted_ids():
    """Onboarding mints ``onboarding:{user_id}`` itself, so it may read
    without passing an id it never received from a client."""
    store = _store(_conversation("user-B-victim"), [_msg("user", "hello")])
    history = _run(store.get_conversation_history("conv_shared_id"))
    assert [h["content"] for h in history] == ["hello"]


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


def test_store_interaction_refuses_cross_user_write():
    """Appending into someone else's conversation must fail closed."""
    store = _store(_conversation("user-B-victim"))

    with pytest.raises(AuthorizationError):
        _run(
            store.store_interaction(
                user_id="user-A",
                role="user",
                content="injected",
                conversation_id="conv_shared_id",
            )
        )


def test_store_interaction_allows_the_owner():
    store = _store(_conversation("user-A"))

    result = _run(
        store.store_interaction(
            user_id="user-A",
            role="user",
            content="mine",
            conversation_id="conv_shared_id",
        )
    )
    assert result == "conv_shared_id"


# ---------------------------------------------------------------------------
# Endpoint guard
# ---------------------------------------------------------------------------


def _user(uid: str) -> User:
    user = User()
    user.id = uid
    user.username = uid
    return user


def _guarded(conversation):
    store = MagicMock()
    store.get_conversation = AsyncMock(return_value=conversation)
    return store


def test_endpoint_guard_rejects_a_foreign_conversation_id():
    from miriam_agent.api.chat import _require_owned_conversation

    store = _guarded(_conversation("user-B-victim"))
    with pytest.raises(HTTPException) as exc:
        _run(_require_owned_conversation(store, _user("user-A"), "conv_shared_id"))
    assert exc.value.status_code == 404


def test_endpoint_guard_accepts_the_owners_conversation_id():
    from miriam_agent.api.chat import _require_owned_conversation

    resolved = _run(
        _require_owned_conversation(
            _guarded(_conversation("user-A")), _user("user-A"), "conv_shared_id"
        )
    )
    assert resolved == "conv_shared_id"


def test_endpoint_guard_allows_a_not_yet_existing_id():
    """Clients start sessions with their own id; the write path creates it."""
    from miriam_agent.api.chat import _require_owned_conversation

    resolved = _run(
        _require_owned_conversation(_guarded(None), _user("user-A"), "platform:web:1")
    )
    assert resolved == "platform:web:1"


def test_endpoint_guard_passes_through_a_missing_id():
    from miriam_agent.api.chat import _require_owned_conversation

    resolved = _run(
        _require_owned_conversation(_guarded(None), _user("user-A"), None)
    )
    assert resolved is None

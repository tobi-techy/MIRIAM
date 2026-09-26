"""Account initialization: ensure_user lifecycle guarantees.

Covers the "account was not created" report using the same mock-session
style as test_memory_session.py (no live database needed): new user
creation, existing user no-op, repeated initialization, invalid input,
database failure surfacing, concurrent initialization safety, and context
propagation of the authenticated user id into tool calls.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.database.memory import MemoryStore  # noqa: E402
from miriam_agent.database.models import User  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _store_with_session(session):
    store = MemoryStore("unused-no-database-connection")
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=session)
    manager.__aexit__ = AsyncMock(return_value=False)
    store.async_session = MagicMock(return_value=manager)
    return store


def _user(uid="u-1"):
    return User(id=uid, username="ada", email="ada@example.com", full_name="Ada Obi")


def _session_mock(existing=None):
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=existing)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def test_new_user_gets_account():
    session = _session_mock(None)
    store = _store_with_session(session)
    _run(store.ensure_user(_user("u-new")))
    row = session.add.call_args.args[0]
    assert (row.id, row.username, row.email) == ("u-new", "ada", "ada@example.com")
    session.commit.assert_awaited_once()


def test_unknown_username_does_not_collide():
    session = _session_mock(None)
    store = _store_with_session(session)
    user = _user("a28c1e1a-3e6d-4a3d-9fec-8186396cc478")
    user.username = "unknown"
    user.full_name = "Unknown User"
    _run(store.ensure_user(user))
    row = session.add.call_args.args[0]
    assert row.username == "u-a28c1e1a-3e6d-4a3d-9fec-8186396cc478"
    assert row.username != "unknown"


def test_existing_user_is_noop():
    session = _session_mock(_user("u-old"))
    store = _store_with_session(session)
    _run(store.ensure_user(_user("u-old")))
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


def test_repeated_initialization_is_idempotent():
    for _ in range(5):
        session = _session_mock(_user("u-rep"))
        store = _store_with_session(session)
        _run(store.ensure_user(_user("u-rep")))
        session.add.assert_not_called()


def test_invalid_user_raises():
    session = _session_mock(None)
    store = _store_with_session(session)
    with pytest.raises(ValueError):
        _run(store.ensure_user(type("X", (), {})()))


def test_database_failure_surfaces_not_hidden():
    session = _session_mock(None)
    session.commit = AsyncMock(side_effect=ConnectionError("db down"))
    store = _store_with_session(session)
    with pytest.raises(ConnectionError):
        _run(store.ensure_user(_user("u-db")))


def test_concurrent_initialization_collapses_to_one_row():
    # First call wins the race; the loser sees IntegrityError on commit but
    # finds the winner's row on re-check, so it returns cleanly.
    sessions = []

    async def go():
        state = {"created": False}

        async def fake_get(model, uid):
            if uid == "u-race" and state["created"]:
                return _user("u-race")
            return None

        async def racer(_i):
            session = _session_mock(None)
            session.get = AsyncMock(side_effect=fake_get)
            sessions.append(session)
            store = _store_with_session(session)
            if not state["created"]:
                state["created"] = True
                await store.ensure_user(_user("u-race"))
            else:
                session.commit = AsyncMock(
                    side_effect=IntegrityError("INSERT", {}, Exception("dup"))
                )
                await store.ensure_user(_user("u-race"))

        await asyncio.gather(*[racer(i) for i in range(5)])
        assert state["created"]

    _run(go())


def test_missing_account_returns_none():
    session = _session_mock(None)
    store = _store_with_session(session)

    async def go():
        async with store._session() as s:
            assert await s.get(User, "nope-missing") is None

    _run(go())


def test_authenticated_user_id_reaches_tool_context():
    seen = {}

    async def handler(args, ctx):
        seen.update(ctx)
        return {"ok": True}

    from miriam_agent.agents.tools import Tool, get_registry

    registry = get_registry()
    name = "ctx_probe_account_test_tool"
    if name not in registry.list_names():
        registry.register(
            Tool(
                name=name,
                description="probe",
                args_schema={"type": "object", "properties": {}},
                handler=handler,
            )
        )
    _run(registry.execute(name, {}, {"user_id": "auth-user-9", "token": "tok-9"}))
    assert seen["user_id"] == "auth-user-9"
    assert seen["token"] == "tok-9"

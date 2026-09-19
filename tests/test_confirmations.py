"""Tests for the server-side pending-confirmation ledger.

The ledger is the mechanism that makes "the user approved *this exact*
action" a server-side fact rather than a client-supplied promise. These
tests use the in-process fallback (``redis_enabled=False``) so they are
hermetic and need no Redis.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.safety.confirmations import PendingConfirmationStore  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _store(ttl: float = 1800.0):
    """A ledger plus a mutable clock so TTL expiry is deterministic."""
    now = {"t": 1000.0}
    store = PendingConfirmationStore(
        redis_enabled=False, ttl=ttl, clock=lambda: now["t"]
    )
    return store, now


def test_stage_validate_consume_roundtrip():
    store, _ = _store()
    sig = "send_money:{}"

    assert _run(store.stage("u1", sig)) is True
    assert _run(store.validate("u1", sig)) is True
    assert _run(store.consume("u1", sig)) is True
    # One-time use: both validate and consume are now false.
    assert _run(store.validate("u1", sig)) is False
    assert _run(store.consume("u1", sig)) is False


def test_validate_false_when_never_staged():
    """A forged approval (no matching proposal) must not validate."""
    store, _ = _store()
    assert _run(store.validate("u1", "send_money:{}")) is False


def test_consume_false_when_never_staged():
    store, _ = _store()
    assert _run(store.consume("u1", "send_money:{}")) is False


def test_ttl_expiry_invalidates_pending():
    store, now = _store(ttl=60.0)
    assert _run(store.stage("u1", "sig")) is True
    now["t"] += 61.0
    assert _run(store.validate("u1", "sig")) is False
    assert _run(store.consume("u1", "sig")) is False


def test_ledger_is_scoped_per_user():
    store, _ = _store()
    _run(store.stage("u1", "sig"))
    assert _run(store.validate("u2", "sig")) is False


def test_restaging_extends_the_window():
    store, now = _store(ttl=60.0)
    _run(store.stage("u1", "sig"))
    now["t"] += 50.0
    _run(store.stage("u1", "sig"))
    now["t"] += 50.0  # 100s after the first stage, 50s after the second
    assert _run(store.validate("u1", "sig")) is True

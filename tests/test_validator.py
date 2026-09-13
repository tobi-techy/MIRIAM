"""Regression tests for miriam_agent.safety.validator.InputValidator.

Covers two fixes from the production audit:

  - Rate limiting used to be an in-memory deque that forgot everything on
    restart and never saw traffic handled by a different process. It's now
    backed by Redis (a fake client stands in here so the tests stay fast
    and hermetic -- real Redis wiring is exercised by running the app).
  - `_sanitize_string` used to strip a long list of "SQL/shell keywords"
    (select, rm, cat, find, http, ...) from every string field, which
    mangled completely honest text without stopping any real attack (the
    app never builds raw SQL or shell commands from user input).
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeRedis:
    """Minimal in-memory stand-in for the handful of sorted-set commands
    validate_rate_limit uses."""

    def __init__(self):
        self.store: dict[str, dict[str, float]] = {}

    async def zremrangebyscore(self, key, min_score, max_score):
        members = self.store.get(key, {})
        for member in [m for m, s in members.items() if min_score <= s <= max_score]:
            del members[member]

    async def zcard(self, key):
        return len(self.store.get(key, {}))

    async def zadd(self, key, mapping):
        self.store.setdefault(key, {}).update(mapping)

    async def expire(self, key, seconds):
        pass


class ExplodingRedis:
    """Simulates Redis being unreachable."""

    async def zremrangebyscore(self, *a, **k):
        raise ConnectionError("redis unreachable")


# -----------------------------------------------------------------------
# Rate limiting
# -----------------------------------------------------------------------


def test_rate_limit_allows_requests_under_the_limit(monkeypatch):
    from miriam_agent.safety.validator import InputValidator

    validator = InputValidator()
    monkeypatch.setattr(validator, "_get_redis", lambda: FakeRedis())

    for _ in range(5):
        assert _run(validator.validate_rate_limit("u-1", "transaction")) is True


def test_rate_limit_blocks_once_limit_is_exceeded(monkeypatch):
    """Regression: the old in-memory limiter worked for this too, but only
    within a single process -- this proves the Redis-backed replacement
    enforces the same limit correctly."""
    from miriam_agent.safety.validator import InputValidator

    validator = InputValidator()
    fake = FakeRedis()
    monkeypatch.setattr(validator, "_get_redis", lambda: fake)

    limit, _window = InputValidator.RATE_LIMITS["transaction"]
    results = [
        _run(validator.validate_rate_limit("u-1", "transaction"))
        for _ in range(limit + 1)
    ]
    assert results[:limit] == [True] * limit
    assert results[limit] is False


def test_rate_limit_is_scoped_per_user(monkeypatch):
    from miriam_agent.safety.validator import InputValidator

    validator = InputValidator()
    fake = FakeRedis()
    monkeypatch.setattr(validator, "_get_redis", lambda: fake)

    limit, _window = InputValidator.RATE_LIMITS["transaction"]
    for _ in range(limit):
        _run(validator.validate_rate_limit("u-1", "transaction"))

    # u-1 is now at the limit, but u-2 has done nothing yet.
    assert _run(validator.validate_rate_limit("u-1", "transaction")) is False
    assert _run(validator.validate_rate_limit("u-2", "transaction")) is True


def test_rate_limit_fails_open_when_redis_is_unreachable(monkeypatch):
    """Regression guard: an infra hiccup must never lock every user out."""
    from miriam_agent.safety.validator import InputValidator

    validator = InputValidator()
    monkeypatch.setattr(validator, "_get_redis", lambda: ExplodingRedis())

    assert _run(validator.validate_rate_limit("u-1", "chat")) is True


# -----------------------------------------------------------------------
# Sanitization no longer mangles honest text
# -----------------------------------------------------------------------


def test_sanitize_string_leaves_honest_text_alone():
    """Regression: 'cat food budget this month' used to come back as
    ' food budget this month' because 'cat ' matched a fake SQL/shell
    keyword filter that had nothing to do with either SQL or shells."""
    from miriam_agent.safety.validator import InputValidator

    validator = InputValidator()
    result = _run(
        validator._sanitize_string("cat food budget this month", "message", "chat")
    )
    assert result == "cat food budget this month"


def test_sanitize_string_leaves_other_previously_mangled_words_alone():
    from miriam_agent.safety.validator import InputValidator

    validator = InputValidator()
    for text in [
        "find me a good ETF",
        "help me set up a budget",
        "check my https://example.com receipt",
    ]:
        assert _run(validator._sanitize_string(text, "message", "chat")) == text


def test_sanitize_string_still_strips_script_tags():
    """Regression guard: removing the fake SQL filter must not remove the
    genuinely useful script-tag stripping."""
    from miriam_agent.safety.validator import InputValidator

    validator = InputValidator()
    result = _run(
        validator._sanitize_string(
            "hello <script>alert(1)</script> world", "message", "chat"
        )
    )
    assert "<script>" not in result
    assert "alert(1)" not in result

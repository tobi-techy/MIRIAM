"""Time helpers shared across layers."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow_naive() -> datetime:
    """Naive UTC now — the value ``datetime.utcnow()`` used to produce.

    The database columns are ``timestamp without time zone``, so writes must
    stay naive; this exists because ``datetime.utcnow()`` is deprecated (and
    its aware sibling, ``datetime.now(UTC)``, would be rejected by asyncpg
    for those columns). Code that handles aware datetimes should use
    ``datetime.now(UTC)`` directly instead.
    """
    return datetime.now(UTC).replace(tzinfo=None)

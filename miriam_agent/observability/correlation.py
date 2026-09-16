"""Request correlation: one trace id per user request, end to end.

A single user message must be followable from the channel that carried it,
through the orchestrator and every tool call, out to the verified result and
the response -- so this module owns the id that makes that possible.

The channel adapter (the Go bridge relaying iMessage/WhatsApp, the web client,
or a Rail internal caller) may supply ``X-Miriam-Trace-Id``; when it does, we
adopt it, and when it does not, the API mints one. The value lives in a
``ContextVar`` so the layers below -- orchestrator, tool registry, safety
audit, onboarding trace -- record the same id without it being threaded
through every function signature. It is also bound into structlog's contextvars
and stamped onto the active OpenTelemetry span, so one id appears in logs,
spans, and audit rows together.

Read it with :func:`get_trace_id` (or :func:`current_trace_id` for a
never-fails string). Never mint an id anywhere else, or a single request stops
being greppable across systems.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

TRACE_HEADER = "x-miriam-trace-id"
"""Inbound/outbound header carrying the id. Lowercase for ASGI header lookups."""

MAX_TRACE_ID_LENGTH = 128
_ID_PREFIX = "tr_"

_trace_id: ContextVar[str | None] = ContextVar("miriam_trace_id", default=None)


def new_trace_id() -> str:
    """Mint a fresh, opaque trace id."""
    return f"{_ID_PREFIX}{uuid.uuid4().hex[:20]}"


def normalize_trace_id(raw: object) -> str | None:
    """Return a usable inbound id, or ``None`` if it should not be trusted.

    An id arrives from outside the process, so it is bounded in length and
    restricted to an unreserved-character alphabet: it ends up in log lines,
    audit rows, and span attributes, and must never be able to break framing
    or inject content there.
    """
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > MAX_TRACE_ID_LENGTH:
        return None
    if not all(c.isalnum() or c in "-_." for c in cleaned):
        return None
    return cleaned


def get_trace_id() -> str | None:
    """The current request's trace id, or ``None`` outside a request."""
    return _trace_id.get()


def current_trace_id() -> str:
    """Trace id safe to store in a dict or log field; ``""`` when unbound."""
    return _trace_id.get() or ""


def _bind_observers(trace_id: str) -> None:
    """Mirror the id into structlog contextvars and the active OTel span."""
    try:
        import structlog

        structlog.contextvars.bind_contextvars(trace_id=trace_id)
    except Exception:  # pragma: no cover - logging must never break a request
        pass
    try:
        from miriam_agent.observability.tracing import stamp_trace_id

        stamp_trace_id(trace_id)
    except Exception:  # pragma: no cover - tracing must never break a request
        pass


def _restore_observers(previous: str | None) -> None:
    """Put structlog's contextvars back the way they were before binding."""
    try:
        import structlog

        if previous is None:
            structlog.contextvars.unbind_contextvars("trace_id")
        else:
            structlog.contextvars.bind_contextvars(trace_id=previous)
    except Exception:  # pragma: no cover
        pass


@contextmanager
def bind_trace_id(trace_id: str | None = None) -> Iterator[str]:
    """Bind a trace id for the duration of the block, yielding the id used.

    ``None`` or a rejected inbound value mints a new id, so a request always
    has one. Nested binds save and restore the outer value instead of clearing
    it, which keeps an inner unit of work from erasing its caller's id.
    """
    resolved = normalize_trace_id(trace_id) or new_trace_id()
    try:
        import structlog

        previous: str | None = structlog.contextvars.get_contextvars().get("trace_id")
    except Exception:  # pragma: no cover
        previous = None

    token: Token[str | None] = _trace_id.set(resolved)
    _bind_observers(resolved)
    try:
        yield resolved
    finally:
        _trace_id.reset(token)
        _restore_observers(previous)

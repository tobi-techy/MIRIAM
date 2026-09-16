"""Document error mapping for Stage 2.

No new exception hierarchy: Go document failures map onto the existing
``core.exceptions`` types so callers keep one error model.
"""

from __future__ import annotations

import httpx

from miriam_agent.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    IntegrationError,
    MiriamError,
    RateLimitError,
)


def map_http_status(
    status: int, body: str, *, trace_id: str | None = None
) -> MiriamError:
    """Map a Go document-endpoint HTTP failure to a typed exception."""
    details = {"http_status": status, "trace_id": trace_id or ""}
    safe = (body or "")[:200]
    if status == 401:
        return AuthenticationError(f"Go document request unauthorized: {safe}", details)
    if status == 403:
        return AuthorizationError(f"Go document request forbidden: {safe}", details)
    if status == 404:
        # Go returns 404 for cross-user documents: surface as authz, not
        # "missing", so callers cannot distinguish чужой doc from absent.
        return AuthorizationError("document not found or not accessible", details)
    if status == 429:
        return RateLimitError(f"Go document rate limit: {safe}", details)
    if status == 503:
        return IntegrationError(f"Go document service unavailable: {safe}", details)
    return IntegrationError(f"Go document request failed ({status}): {safe}", details)


def map_transport_error(exc: httpx.HTTPError) -> IntegrationError:
    return IntegrationError(f"Go document service unreachable: {exc}")

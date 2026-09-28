"""Shared TypeSafe async client.

One process-wide ``AsyncTypeSafeClient``, created lazily inside the running
event loop (httpx clients are loop-bound, so it cannot be built at import
time). Reads ``TYPESAFE_API_KEY`` from settings; the key is never hardcoded.
"""

from __future__ import annotations

import logging
import threading

from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from miriam_agent.config.settings import get_settings

logger = logging.getLogger(__name__)

_client: AsyncTypeSafeClient | None = None
_client_lock = threading.Lock()


def enabled() -> bool:
    """Whether the judgment layer is on.

    Disabled (flag off or key missing) means the agent behaves exactly as it
    did before TypeSafe existed, and every caller logs that it skipped the gate.
    """
    settings = get_settings()
    return bool(settings.TYPESAFE_ENABLED and settings.TYPESAFE_API_KEY)


def get_async_client() -> AsyncTypeSafeClient:
    """The process-wide async client, built once and reused by all requests."""
    global _client
    with _client_lock:
        if _client is None:
            settings = get_settings()
            _client = AsyncTypeSafeClient(
                api_key=settings.TYPESAFE_API_KEY,
                model=settings.TYPESAFE_MODEL,
                timeout=settings.TYPESAFE_TIMEOUT,
                retry=RetryPolicy(
                    max_retries=settings.TYPESAFE_MAX_RETRIES,
                    respect_retry_after=True,
                    timeout=settings.TYPESAFE_TIMEOUT,
                ),
            )
        return _client


async def close_async_client() -> None:
    """Close the shared client during application shutdown."""
    global _client
    with _client_lock:
        client = _client
        _client = None
    if client is not None:
        await client.aclose()

"""Shared TypeSafe async client.

One process-wide ``AsyncTypeSafeClient``, created lazily inside the running
event loop (httpx clients are loop-bound, so it cannot be built at import
time). Reads ``TYPESAFE_API_KEY`` from settings; the key is never hardcoded.
"""

from __future__ import annotations

import logging

from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from miriam_agent.config.settings import get_settings

logger = logging.getLogger(__name__)

_client: AsyncTypeSafeClient | None = None


def enabled() -> bool:
    """Whether the judgment layer is on.

    Disabled (flag off or key missing) means the agent behaves exactly as it
    did before TypeSafe existed, and every caller logs that it skipped the gate.
    """
    settings = get_settings()
    return bool(settings.TYPESAFE_ENABLED and settings.TYPESAFE_API_KEY)


def get_async_client() -> AsyncTypeSafeClient:
    """The process-wide async client, built on first use inside the loop."""
    global _client
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

"""Server-side pending-confirmation ledger for staged money actions.

How it plugs into the agent loop:

1. Every money action the LLM proposes is *staged* here before the response
   card is returned to the user. The key is derived from the exact proposed
   action ``(user_id, signature)`` and the record expires after a TTL.
2. When the user later sends ``approved_actions`` (the confirmation), the
   agent *validates* each one against this ledger. An action that was never
   staged -- forged, edited, replayed after expiry -- is denied even though
   the payload might look identical to a legitimately proposed one.
3. After the Go side actually executes, the record is *consumed* (deleted).
   A second approval carrying the same signature can therefore never move the
   same money twice: the first confirmation is what's real, and it's gone.

The ledger therefore makes "the confirmation matches the exact proposed
action" a server-side fact instead of a client round-trip promise.

Backend: Redis (shared across the two uvicorn workers and any horizontal
replicas), with an in-process fallback for when Redis is unavailable so a
Redis outage does not take a single-instance app fully down. Multi-instance
deployments must run Redis for confirmations to be shared across replicas;
without it the fallback is per-process only.

Design notes:
- ``validate`` before execution, ``consume`` only after a successful
  execution. A failed Go attempt (network error, backend 500) leaves the
  record in place so the in-turn retry path can legitimately re-attempt.
- Fail-closed on errors: if the ledger cannot answer, the action is denied.
  An unverifiable confirmation must never be treated as confirmed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from miriam_agent.config.settings import get_settings

logger = logging.getLogger(__name__)

DEFAULT_PENDING_TTL_SECONDS = 30 * 60  # 30 minutes, same horizon as Go tokens
_KEY_PREFIX = "miriam:confirm:"
_STAGED_VALUE = "1"


def _hash_signature(signature: str) -> str:
    """Stable, URL-safe digest of the action signature used as the Redis key."""
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:40]


def _redis_key(user_id: str, sig_hash: str) -> str:
    return f"{_KEY_PREFIX}{user_id}:{sig_hash}"


class PendingConfirmationStore:
    """Redis-backed, one-time, TTL'd ledger of staged money actions.

    ``redis_enabled=False`` forces the in-process fallback and is used by
    tests so no network is touched; production leaves it on (auto-detects).
    """

    def __init__(
        self,
        redis_url: str | None = None,
        ttl: float = DEFAULT_PENDING_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        redis_enabled: bool = True,
    ):
        self.redis_url = redis_url or get_settings().REDIS_URL
        self.ttl = ttl if ttl > 0 else DEFAULT_PENDING_TTL_SECONDS
        self.redis_enabled = redis_enabled
        self._clock = clock
        self._redis: Any | None = None
        self._lock = asyncio.Lock()
        # Fallback: {redis_key: expiry_monotonic}. Only used when Redis is
        # unavailable (or redis_enabled=False for tests).
        self._memory: dict[str, float] = {}

    async def _client(self) -> Any | None:
        """Lazily resolve the Redis client. Returns None when unavailable."""
        if not self.redis_enabled:
            return None
        if self._redis is None:
            try:
                import redis.asyncio as aioredis

                client = aioredis.from_url(self.redis_url, decode_responses=True)
                await client.ping()
                self._redis = client
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Redis unavailable for confirmation ledger; using "
                    "in-process fallback: %s",
                    e,
                )
                self._redis = None
        return self._redis

    # -- write path ---------------------------------------------------------

    async def stage(self, user_id: str, signature: str) -> bool:
        """Record a proposed action so a later approval can be verified."""
        key = _redis_key(user_id, _hash_signature(signature))
        client = await self._client()
        if client is not None:
            try:
                await client.set(key, _STAGED_VALUE, ex=int(self.ttl))
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Confirmation stage failed on Redis, falling back to "
                    "in-process ledger: %s",
                    e,
                )
        async with self._lock:
            self._memory[key] = self._clock() + self.ttl
        return True

    # -- read path ----------------------------------------------------------

    async def validate(self, user_id: str, signature: str) -> bool:
        """Whether a matching pending confirmation currently exists."""
        key = _redis_key(user_id, _hash_signature(signature))
        now = self._clock()
        client = await self._client()
        if client is not None:
            try:
                exists = bool(await client.exists(key))
                if exists:
                    return True
            except Exception as e:  # noqa: BLE001
                # Can't trust Redis right now -> fail closed (deny).
                logger.warning("Confirmation validate failed on Redis: %s", e)
                return False
        async with self._lock:
            expiry = self._memory.get(key)
        if expiry is None:
            return False
        return expiry > now

    # -- consume path -------------------------------------------------------

    async def consume(self, user_id: str, signature: str) -> bool:
        """Atomically claim-and-delete a pending record after execution.

        Returns False when nothing valid was pending (already consumed,
        expired, or never staged).
        """
        key = _redis_key(user_id, _hash_signature(signature))
        now = self._clock()
        client = await self._client()
        if client is not None:
            try:
                removed = await client.getdel(key)
                if removed is not None:
                    return True
            except Exception as e:  # noqa: BLE001
                logger.warning("Confirmation consume failed on Redis: %s", e)
                return False
        async with self._lock:
            expiry = self._memory.get(key)
            if expiry is None or expiry <= now:
                self._memory.pop(key, None)
                return False
            del self._memory[key]
            return True


@lru_cache
def get_pending_confirmation_store() -> PendingConfirmationStore:
    """Shared ledger instance (one per process; Redis shares across workers)."""
    return PendingConfirmationStore()

"""Proactive-message state for Miriam's 24/7 analyst.

Miriam watches always, but reaches out rarely and never repeats herself.
This store stamps "last message per user" so the analyst does not fire the
same (or a near-same) message twice and does not message anyone more than
once per minimum interval.

Redis-backed for multi-instance deployments, with an in-process fallback so
a missing Redis server can never break the analyst. Fail-open everywhere.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from miriam_agent.config.settings import get_settings

logger = logging.getLogger(__name__)

_TTL_SECONDS = 24 * 60 * 60
_KEY_PREFIX = "miriam:proactive:analyst"


def message_hash(message: str) -> str:
    """Stable hash of a message so identical outreach is deduped."""
    return hashlib.sha256(message.strip().encode("utf-8")).hexdigest()[:16]


class ProactiveStateStore:
    """Stamps the last proactive message per user.

    ``should_stay_quiet``:
      - no record -> reach out
      - identical message stamped < 24h ago -> stay quiet (dedupe)
      - different message but priority is "high" -> reach out (risks matter)
      - different message stamped within the minimum interval -> stay quiet
      - otherwise -> reach out

    ``min_interval_hours`` defaults to `PROACTIVE_MIN_INTERVAL_HOURS` and is
    the calm-down window so she is present, not nagging.
    """

    def __init__(
        self,
        min_interval_hours: float | None = None,
        redis_url: str | None = None,
        use_settings_url: bool = True,
    ):
        settings = get_settings()
        self._min_interval: float = (
            min_interval_hours
            if min_interval_hours is not None
            else settings.PROACTIVE_MIN_INTERVAL_HOURS
        )
        # Use the configured Redis URL unless the caller explicitly opts out
        # (use_settings_url=False + redis_url=None -> in-process only).
        self._url = redis_url
        if self._url is None and use_settings_url:
            self._url = settings.REDIS_URL or None
        self._redis: Any = None
        if self._url:
            try:
                from redis import asyncio as aioredis

                self._redis = aioredis.from_url(self._url, decode_responses=True)
            except Exception as e:  # pragma: no cover - import/env issues
                logger.warning(
                    "Proactive state Redis unavailable, using in-process store: %s",
                    e,
                )
                self._redis = None
        # In-process fallback: user_id -> dict(hash, ts).
        self._local: dict[str, dict[str, Any]] = {}
        self._degraded_logged = False

    def _note_redis_failure(self, exc: Exception, operation: str) -> None:
        """The quiet-hours/dedup window lives in Redis so every worker shares
        it. Falling back to process memory means two workers can each decide the
        user is due a message; say so once instead of hiding it."""
        if self._degraded_logged:
            logger.debug("Proactive state Redis %s failed: %s", operation, exc)
            return
        self._degraded_logged = True
        logger.warning(
            "Proactive state Redis %s failed; falling back to in-process state, "
            "which is not shared across workers, so the quiet-hours window no "
            "longer holds globally. Check REDIS_URL credentials. Cause: %s",
            operation,
            exc,
        )

    # -- read / write ---------------------------------------------------

    async def _read(self, user_id: str) -> dict[str, Any] | None:
        key = _KEY_PREFIX + ":" + user_id
        try:
            if self._redis is not None:
                raw = await self._redis.get(key)
                if raw:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        return data
        except Exception as e:
            self._note_redis_failure(e, "read")
        return self._local.get(user_id)

    async def _write(self, user_id: str, record: dict[str, Any]) -> None:
        key = _KEY_PREFIX + ":" + user_id
        self._local[user_id] = record
        try:
            if self._redis is not None:
                await self._redis.set(key, json.dumps(record), ex=_TTL_SECONDS)
        except Exception as e:
            self._note_redis_failure(e, "write")

    # -- API ------------------------------------------------------------

    async def should_stay_quiet(
        self, user_id: str, priority: str, message: str
    ) -> bool:
        """Return True when this message should NOT be sent right now."""
        record = await self._read(user_id)
        if record is None:
            return False

        same = record.get("hash") == message_hash(message)
        ts = record.get("ts", 0)
        age_hours = (time.time() - float(ts)) / 3600
        if same and age_hours < 24:
            return True
        if record.get("priority") == "high" or priority == "high":
            return False
        return bool(age_hours < self._min_interval)

    async def mark(self, user_id: str, priority: str, message: str) -> None:
        """Stamp that ``message`` was the latest of ``priority`` for this user."""
        await self._write(
            user_id,
            {"hash": message_hash(message), "priority": priority, "ts": time.time()},
        )


_store: ProactiveStateStore | None = None


def get_proactive_state() -> ProactiveStateStore:
    """Process-wide store singleton."""
    global _store
    if _store is None:
        _store = ProactiveStateStore()
    return _store

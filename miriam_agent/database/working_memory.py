"""Short-term working memory backed by Redis.

Mirrors the Go backend's working-memory tier: a per-user sliding buffer
of recent conversation summaries with a TTL. The agent reads this to
"remember" what happened recently within a session, while long-term facts
live in Postgres (MemoryStore) and vectors in pgvector.

Fail-open by design: if Redis is unavailable, calls return empty/False so
the agent still works — memory is an enhancement, never a hard dependency.
"""

import asyncio
import json
import logging
from typing import Any

from miriam_agent.config.settings import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 30 * 60
MAX_ENTRIES = 20
_KEY_PREFIX = "miriam:working:"  # {prefix}{user_id}


class WorkingMemory:
    """Redis-backed sliding window of recent conversation summaries."""

    def __init__(self, redis_url: str | None = None, ttl: int = DEFAULT_TTL_SECONDS):
        self.redis_url = redis_url or get_settings().REDIS_URL
        self.ttl = ttl
        self._redis = None
        self._lock = asyncio.Lock()

    async def _client(self):
        if self._redis is None:
            import redis.asyncio as aioredis

            try:
                self._redis = aioredis.from_url(self.redis_url, decode_responses=True)
                await self._redis.ping()
            except Exception as e:
                logger.warning("Redis unavailable, working memory disabled: %s", e)
                self._redis = None
        return self._redis

    def _key(self, user_id: str) -> str:
        return f"{_KEY_PREFIX}{user_id}"

    async def _available(self) -> bool:
        client = await self._client()
        return client is not None

    async def push(self, user_id: str, entry: dict[str, Any]) -> bool:
        """Add a summary entry to the user's recent working memory."""
        if not await self._available():
            return False
        try:
            client = await self._client()
            key = self._key(user_id)
            payload = json.dumps({"memo": entry}, default=str)
            async with self._lock:
                length = await client.llen(key)
                if length >= MAX_ENTRIES:
                    await client.lpop(key)
                await client.rpush(key, payload)
                await client.expire(key, self.ttl)
            return True
        except Exception as e:
            logger.warning("Working memory push failed: %s", e)
            return False

    async def recent(self, user_id: str, limit: int = 8) -> list[dict[str, Any]]:
        """Return the most recent working-memory entries (oldest first)."""
        if not await self._available():
            return []
        try:
            client = await self._client()
            key = self._key(user_id)
            items = await client.lrange(key, -limit, -1)
            out = []
            for item in items:
                try:
                    parsed = json.loads(item)
                    out.append(parsed.get("memo", parsed))
                except Exception:
                    continue
            return out
        except Exception as e:
            logger.warning("Working memory read failed: %s", e)
            return []

    async def clear(self, user_id: str) -> bool:
        if not await self._available():
            return False
        try:
            client = await self._client()
            await client.delete(self._key(user_id))
            return True
        except Exception as e:
            logger.warning("Working memory clear failed: %s", e)
            return False

    async def record_conversation_turn(
        self,
        user_id: str,
        user_message: str,
        assistant_response: str,
        topic: str | None = None,
    ) -> bool:
        """Convenience wrapper: summarize a single turn into working memory."""
        summary = {
            "user": user_message[:200],
            "assistant": assistant_response[:200],
            "topic": topic or "general",
        }
        return await self.push(user_id, summary)


_working_memory: WorkingMemory | None = None


def get_working_memory() -> WorkingMemory:
    """Get the process-wide working memory singleton."""
    global _working_memory
    if _working_memory is None:
        _working_memory = WorkingMemory()
    return _working_memory

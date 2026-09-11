"""Embedding generation for Miriam Financial Agent.

Wraps OpenAI's embeddings API with batching, caching, and graceful
degradation (embeddings are an enhancement; the system must not crash
when they fail).
"""

import hashlib
import logging

from miriam_agent.config.settings import get_settings

logger = logging.getLogger(__name__)


class Embedder:
    """Generates text embeddings via OpenAI."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key
        self.model = model or get_settings().EMBEDDING_MODEL
        self._cache: dict[str, list[float]] = {}
        self._enabled = bool(self.api_key or get_settings().OPENAI_API_KEY)

    def _ensure_client(self):
        from openai import AsyncOpenAI

        key = self.api_key or get_settings().OPENAI_API_KEY
        return AsyncOpenAI(api_key=key)

    def _cache_key(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    async def embed(self, text: str) -> list[float] | None:
        """Embed a single text. Returns None when unavailable."""
        if not self._enabled:
            return None
        key = self._cache_key(text)
        if key in self._cache:
            return self._cache[key]
        try:
            client = self._ensure_client()
            resp = await client.embeddings.create(
                model=self.model,
                input=text,
            )
            vector = resp.data[0].embedding
            self._cache[key] = vector
            return vector
        except Exception as e:
            logger.warning("Embedding failed: %s", e)
            return None

    async def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        """Embed a batch of texts, tolerating individual failures."""
        results: list[list[float] | None] = []
        for text in texts:
            results.append(await self.embed(text))
        return results

    async def is_available(self) -> bool:
        """Heuristic check (no network call) on whether embeddings can be used."""
        return self._enabled


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    """Get the process-wide embedder singleton."""
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder

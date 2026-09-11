"""Async client for Supermemory (https://supermemory.ai/docs).

Supermemory is the long-term memory + retrieval infrastructure for the
agent. It owns the semantic layer the Go backend cannot:

- Graph memory: ingests conversation turns, extracts facts, links them
  to what it already knows (updates / extends / derives), and forgets
  stale truths automatically.
- User profiles: an always-on static + dynamic summary per container.
- SuperRAG: chunk-level search for grounding on documents.

This client implements the write path (``ingest_conversation`` /
``add_document``), the read path (``profile`` / ``search``), and explicit
memory management (``create_memories`` / ``update_memory`` /
``forget_memory``).

Design rules:
- Async httpx, same pattern as ``go_client.py``. The official Python SDK
  is synchronous, which would block FastAPI's event loop.
- Multi-tenant isolation via ``containerTag`` = sanitized user id.
- Fail-open: Supermemory is an enhancement, never a hard dependency. If
  the key is missing or the API errors, methods return empty data and log
  a warning instead of raising.
- Retries with backoff on transient errors (408, 409, 429, >=500).

Streamlined to the endpoints the agent actually uses:

- POST /v4/conversations  ingest/update a chat session (turn-aware)
- POST /v3/documents      ingest text/content (RAG + memory)
- GET  /v3/documents/{id}  document status
- POST /v4/profile        always-on user context
- POST /v4/search         semantic recall (memories | documents | hybrid)
- POST /v4/memories       create memories directly
- PATCH /v4/memories      update a memory (versioned)
- DELETE /v4/memories     forget a memory
- POST /v4/memories/forget-matching  forget everything about X
"""

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from miriam_agent.config.settings import get_settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.supermemory.ai"
_DEFAULT_HEADERS = {"Content-Type": "application/json"}

# containerTag pattern per the v4 OpenAPI: ^[a-zA-Z0-9_:-]+$ (max 100)
_CONTAINER_TAG_RE = re.compile(r"^[a-zA-Z0-9_:\-]+$")

_RETRYABLE_STATUS = {408, 409, 429}


def container_tag_for(user_id: str) -> str:
    """Sanitize an arbitrary user id into a valid Supermemory containerTag.

    Uses a stable ``user_``-prefixed hash so any upstream id (UUID, email,
    tag, whatever the Go backend issues) maps to a compliant, isolated tag.
    """
    value = user_id.strip()
    # Keep as-is when already compliant (keeps tags human-readable).
    if len(value) <= 90 and _CONTAINER_TAG_RE.match(value):
        return value
    import hashlib

    digest = hashlib.sha256(value.encode()).hexdigest()[:32]
    return f"user_{digest}"


class SupermemoryError(Exception):
    """Raised only for programming errors; API failures are absorbed."""


class SupermemoryClient:
    """HTTP client for the Supermemory REST API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 20.0,
        max_retries: int = 2,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.SUPERMEMORY_API_KEY
        self.base_url = (base_url or settings.SUPERMEMORY_BASE_URL or _BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        headers = dict(_DEFAULT_HEADERS)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
        )

    @property
    def enabled(self) -> bool:
        """True when an API key is configured."""
        return bool(self.api_key)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Write path: get content in
    # ------------------------------------------------------------------

    async def ingest_conversation(
        self,
        container_tag: str,
        conversation_id: str,
        messages: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
        dreaming: str = "dynamic",
    ) -> Optional[Dict[str, Any]]:
        """Ingest or update a chat session.

        Keep ``conversation_id`` stable across turns so Supermemory treats
        the whole session as one document and only processes what's new.

        ``messages``: [{role: "user"|"assistant"|"system"|"tool", content: str}]
        """
        if not await self._can_call(conversation_id):
            return None
        payload: Dict[str, Any] = {
            "conversationId": conversation_id,
            "messages": messages,
            "containerTag": container_tag,
            "dreaming": dreaming,
        }
        if metadata:
            payload["metadata"] = _flat_metadata(metadata)
        return await self._post("/v4/conversations", payload)

    async def add_document(
        self,
        container_tag: str,
        content: str,
        custom_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        task_type: str = "memory",
        dreaming: str = "dynamic",
    ) -> Optional[Dict[str, Any]]:
        """Send raw content (text, notes, articles) through the pipeline.

        ``task_type="memory"`` extracts facts + updates the profile +
        indexes chunks. ``task_type="superrag"`` only chunks/embeds and is
        5x cheaper, but does not affect what memory knows about the user.
        """
        if not content:
            return None
        payload: Dict[str, Any] = {
            "content": content,
            "containerTag": container_tag,
            "taskType": task_type,
            "dreaming": dreaming,
        }
        if custom_id:
            payload["customId"] = custom_id
        if metadata:
            payload["metadata"] = _flat_metadata(metadata)
        return await self._post("/v3/documents", payload)

    async def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Fetch document status: queued | processing | done | failed."""
        return await self._get(f"/v3/documents/{doc_id}")

    async def wait_until_done(
        self,
        doc_id: str,
        timeout: float = 60.0,
        interval: float = 1.5,
    ) -> Optional[Dict[str, Any]]:
        """Poll document status until done/failed or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            doc = await self.get_document(doc_id)
            if not doc:
                return None
            status = (doc.get("status") or "").lower()
            if status in ("done", "failed"):
                return doc
            await asyncio.sleep(interval)
        logger.warning("Supermemory document %s not done within %.0fs", doc_id, timeout)
        return None

    # ------------------------------------------------------------------
    # Read path: get context back out
    # ------------------------------------------------------------------

    async def profile(
        self,
        container_tag: str,
        query: Optional[str] = None,
        include: Optional[List[str]] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Fetch the always-on user profile plus optional search results.

        Returns ``{profile: {static: [], dynamic: [], buckets: {}},
        search_results: {results: [], total, timing}}`` (empty on failure).
        """
        payload: Dict[str, Any] = {"containerTag": container_tag}
        if query:
            payload["q"] = query
        if include:
            payload["include"] = include
        if filters:
            payload["filters"] = filters
        data = await self._post("/v4/profile", payload)
        if not data:
            return {"profile": {"static": [], "dynamic": [], "buckets": {}}, "search_results": None}
        profile = data.get("profile") or {}
        return {
            "profile": {
                "static": profile.get("static") or [],
                "dynamic": profile.get("dynamic") or [],
                "buckets": profile.get("buckets") or {},
            },
            "search_results": data.get("searchResults"),
        }

    async def search(
        self,
        container_tag: str,
        query: str,
        search_mode: str = "memories",
        limit: int = 5,
        include_related: bool = False,
        filters: Optional[Dict[str, Any]] = None,
        threshold: Optional[float] = None,
        rerank: bool = False,
    ) -> Dict[str, Any]:
        """Semantic recall.

        ``search_mode``: memories | documents | hybrid.
        Returns ``{results: [...], total: n}`` (empty on failure).
        """
        if not query:
            return {"results": [], "total": 0}
        payload: Dict[str, Any] = {
            "containerTag": container_tag,
            "q": query,
            "searchMode": search_mode,
            "limit": limit,
            "include": {
                "relatedMemories": include_related,
                "documents": False,
                "summaries": False,
            },
            "rerank": rerank,
        }
        if filters:
            payload["filters"] = filters
        if threshold is not None:
            payload["threshold"] = threshold
        data = await self._post("/v4/search", payload)
        if not data:
            return {"results": [], "total": 0}
        return {
            "results": data.get("results") or [],
            "total": int(data.get("total") or 0),
        }

    # ------------------------------------------------------------------
    # Memory management: explicit CRUD + forgetting
    # ------------------------------------------------------------------

    async def create_memories(
        self,
        container_tag: str,
        memories: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Write facts directly, bypassing the document pipeline.

        ``memories``: [{content, isStatic?, metadata?}]. Use when the agent
        already knows the exact fact (a preference, a correction).
        """
        if not memories:
            return None
        payload: Dict[str, Any] = {
            "containerTag": container_tag,
            "memories": list(memories),
        }
        return await self._post("/v4/memories", payload)

    async def update_memory(
        self,
        container_tag: str,
        new_content: str,
        memory_id: Optional[str] = None,
        content: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Versioned update: creates a new version, original keeps isLatest=False."""
        if memory_id is None and content is None:
            logger.warning("update_memory requires id or content")
            return None
        payload: Dict[str, Any] = {
            "containerTag": container_tag,
            "newContent": new_content,
        }
        if memory_id:
            payload["id"] = memory_id
        if content:
            payload["content"] = content
        if metadata:
            payload["metadata"] = _flat_metadata(metadata)
        return await self._patch("/v4/memories", payload)

    async def forget_memory(
        self,
        container_tag: str,
        memory_id: Optional[str] = None,
        content: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Soft-delete a single memory (excluded from search, kept for audit)."""
        if memory_id is None and content is None:
            logger.warning("forget_memory requires id or content")
            return None
        payload: Dict[str, Any] = {"containerTag": container_tag}
        if memory_id:
            payload["id"] = memory_id
        if content:
            payload["content"] = content
        if reason:
            payload["reason"] = reason
        return await self._delete("/v4/memories", payload)

    async def forget_matching(
        self,
        container_tag: str,
        query: Optional[str] = None,
        ids: Optional[List[str]] = None,
        dry_run: bool = True,
        reason: Optional[str] = None,
        max_forget: int = 100,
    ) -> Optional[Dict[str, Any]]:
        """Semantic bulk forget ('forget everything about X') with dry run."""
        if query is None and not ids:
            logger.warning("forget_matching requires query or ids")
            return None
        payload: Dict[str, Any] = {"containerTag": container_tag, "dryRun": dry_run}
        if query:
            payload["query"] = query
            payload["maxForget"] = max_forget
        if ids:
            payload["ids"] = ids
        if reason:
            payload["reason"] = reason
        return await self._post("/v4/memories/forget-matching", payload)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _can_call(self, label: str) -> bool:
        if not self.enabled:
            return False
        if not label or not label.strip():
            return False
        return True

    async def _post(self, path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return await self._request("POST", path, json=payload)

    async def _get(self, path: str) -> Optional[Dict[str, Any]]:
        return await self._request("GET", path)

    async def _patch(self, path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return await self._request("PATCH", path, json=payload)

    async def _delete(
        self, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        return await self._request("DELETE", path, json=payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        retries: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        attempts = self.max_retries if retries is None else retries
        for attempt in range(attempts + 1):
            try:
                resp = await self._client.request(method, path, json=json)
                if resp.status_code in _RETRYABLE_STATUS and attempt < attempts:
                    await asyncio.sleep(0.3 * (2 ** attempt))
                    continue
                resp.raise_for_status()
                if resp.content and resp.status_code != 204:
                    return resp.json()
                return {}
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                status = getattr(e, "response", None)
                code = status.status_code if status is not None else None
                is_transient = (
                    code in _RETRYABLE_STATUS or code is None or (code or 0) >= 500
                )
                if is_transient and attempt < attempts:
                    await asyncio.sleep(0.3 * (2 ** attempt))
                    continue
                logger.warning(
                    "Supermemory %s %s failed: %s",
                    method,
                    path,
                    e,
                    exc_info=True,
                )
                return None
        return None


_supermemory_client: Optional[SupermemoryClient] = None


def get_supermemory_client() -> SupermemoryClient:
    """Get the process-wide Supermemory client singleton."""
    global _supermemory_client
    if _supermemory_client is None:
        _supermemory_client = SupermemoryClient()
    return _supermemory_client


def _flat_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only string/number/bool values (Supermemory's metadata rule)."""
    out: Dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, (str, int, float, bool)) and value is not None:
            out[key] = value
    return out
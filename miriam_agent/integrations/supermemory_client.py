"""Async client for Supermemory (https://supermemory.ai/docs).

Supermemory is the long-term memory + retrieval infrastructure for the
agent. It owns the semantic layer the Go backend cannot:

- Graph memory: ingests conversation turns, extracts facts, links them
  to what it already knows (updates / extends / derives), and forgets
  stale truths automatically.
- User profiles: an always-on static + dynamic summary per container
  (plus topical buckets).
- SuperRAG: chunk-level search for grounding on documents.

This client implements the write path (``ingest_conversation`` /
``add_document``), the read path (``profile`` / ``search``), explicit
memory management (``create_memories`` / ``update_memory`` /
``forget_memory`` / ``forget_matching``), inferred-memory review
(``list_inferred`` / ``review_inferred``), and container lifecycle
(``update_container_settings`` / ``delete_container_tag`` for GDPR
erasure).

Doc coverage map (each section wired here):
- overview/what-is-supermemory: one store, memory + SuperRAG on one tag.
- concepts/how-it-works: stable customId -> diff billing; dynamic dreaming
  default, instant only when the next step must see the graph now (+1 op).
- concepts/graph-memory: related edges via include.relatedMemories;
  parents/children/related all surfaced by the service layer.
- concepts/super-rag: taskType memory (facts+profile) vs superrag
  (chunks only, 5x cheaper); hybrid search pulls both.
- concepts/container-tags: singular containerTag (v4 current; plural
  containerTags deprecated, still sent for /v3 compat); ^[a-zA-Z0-9_:-]+$,
  max 100.
- concepts/filtering: metadata is flat (str/num/bool); AND/OR filters,
  docId scoping, rewriteQuery.
- concepts/user-profiles + recall/user-profiles: static/dynamic/buckets;
  profile(q=...) returns searchResults in one call (one search-query meter
  instead of two).
- concepts/customization: entityContext per container grounds extraction.
- ingestion/add-memories: customId updates, filterByMetadata scoped writes.
- recall/search: hybrid recommended; memory|chunk response shape.
- recall/memory-operations: versioned update; forget by id/content;
  forget-matching with dryRun preview -> ids apply (bound deletes).
- recall/memory-review: inferred queue approve/decline/undo.
- concepts/rules: session-level docs, sequential ingest, same prefix per
  customId, entity grounding, container per boundary.
- overview/billing: diff billing needs stable customId + update path;
  402 = credits exhausted (fail-open here).

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
- POST /v4/profile        always-on user context (+ optional search)
- POST /v4/search         semantic recall (memories | documents | hybrid)
- POST /v4/memories       create memories directly
- PATCH /v4/memories      update a memory (versioned)
- DELETE /v4/memories     forget a memory
- POST /v4/memories/forget-matching  forget everything about X
- GET  /v3/container-tags/{tag}/inferred  review queue
- POST /v3/container-tags/{tag}/inferred/{id}/review  approve/decline/undo
- PATCH /v3/container-tags/{tag}  entityContext / display name
- DELETE /v3/container-tags/{tag}  GDPR erasure
"""

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from miriam_agent.config.settings import get_settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.supermemory.ai"
_DEFAULT_HEADERS = {"Content-Type": "application/json"}

# containerTag pattern per the concept page: ^[a-zA-Z0-9_:-]+$ (max 100).
# NOTE: the add-document OpenAPI snippet instead says "hyphens, underscores,
# and dots only", and scoped-key creation allows dots too — the docs disagree
# with each other. Hashing non-matching ids (conservative) always yields a
# tag every validator accepts, so keep the concept-page pattern here.
_CONTAINER_TAG_RE = re.compile(r"^[a-zA-Z0-9_:\-]+$")

# customId pattern per the add-document OpenAPI: max 100 chars, alphanumeric
# with hyphens, underscores, and dots only (no colons — unlike containerTag).
_CUSTOM_ID_RE = re.compile(r"[^a-zA-Z0-9_.\-]")

_RETRYABLE_STATUS = {408, 409, 429}


def container_tag_for(user_id: str) -> str:
    """Sanitize an arbitrary user id into a valid Supermemory containerTag.

    Uses a stable ``user_``-prefixed hash so any upstream id (UUID, email,
    tag, whatever the Go backend issues) maps to a compliant, isolated tag.

    Rules per /docs/concepts/container-tags: ``^[a-zA-Z0-9_:-]+$``,
    max 100 chars. Compliant ids pass through untouched (human-readable,
    deterministic — no lookup needed at query time).
    """
    value = user_id.strip()
    # Keep as-is when already compliant (keeps tags human-readable).
    if value and len(value) <= 100 and _CONTAINER_TAG_RE.match(value):
        return value
    import hashlib

    digest = hashlib.sha256(value.encode()).hexdigest()[:32]
    return f"user_{digest}"


def custom_id_for(value: str) -> str:
    """Sanitize an arbitrary id into a valid Supermemory customId.

    Per the add-document OpenAPI: max 100 chars, alphanumeric plus
    ``-``, ``_``, ``.`` only. Our conversation ids contain colons
    (``onboarding:{user}``), so sending them verbatim as ``customId``
    risks a 400 — map every disallowed char to ``_`` and truncate.
    Falls back to a stable hash when nothing valid remains.
    """
    cleaned = _CUSTOM_ID_RE.sub("_", (value or "").strip())[:100]
    if cleaned.strip("_."):
        return cleaned
    import hashlib

    return f"id_{hashlib.sha256(value.encode()).hexdigest()[:32]}"


_CHANNEL_RE = re.compile(r"[^a-z0-9]+")


def _sanitize_channel(channel: str) -> str:
    token = _CHANNEL_RE.sub("-", (channel or "").strip().lower()).strip("-")
    return token[:24] or "web"


def conversation_scope_for(user_id: str, channel: str = "web") -> str:
    """Stable per-person, per-channel conversation id.

    One person must accumulate one connected memory graph, not a new document
    per chat session. Keying every ingest to a stable
    ``miriam:<channel>:<container>`` scope keeps a channel's turns in a single
    document that Supermemory can diff, and gives ``dreaming: dynamic`` a
    coherent unit to link across, instead of scattering the person across one
    document per session id.

    The container tag (the isolation boundary) is still derived from the user
    id alone, so this remains per-person: two people never share a scope.
    """
    return f"miriam:{_sanitize_channel(channel)}:{container_tag_for(user_id)}"


_PLACEHOLDER_NAMES = {"unknown", "unknown user", "n/a"}


def display_name_for(*candidates: str | None) -> str | None:
    """First real name among the candidates, ignoring token placeholders.

    ``get_current_user`` fills in a missing JWT claim with ``"unknown"`` /
    ``"Unknown User"``. Lifting those into a container name would label a
    person's whole memory space "Unknown User", which is worse than leaving it
    unlabelled, so placeholders are dropped.
    """
    for candidate in candidates:
        value = (candidate or "").strip()
        if value and value.casefold() not in _PLACEHOLDER_NAMES:
            return value
    return None


def person_entity_context(user_id: str, name: str | None = None) -> str:
    """Container-level grounding for a person's memory space.

    Supermemory reads this while processing documents in the container, so it
    is what stops extraction from drifting on unanchored pronouns: without it,
    "I'm saving for a house" is a fact about nobody in particular.
    """
    who = (name or "").strip() or "this person"
    return (
        f"This space holds the long-term memory of {who} (user id {user_id}), "
        f"a person using Miriam, their personal financial assistant. Every fact "
        f"here is about {who} personally; resolve first-person statements "
        f'("I", "me", "my") to {who}.'
    )


class SupermemoryError(Exception):
    """Raised only for programming errors; API failures are absorbed."""


class SupermemoryClient:
    """HTTP client for the Supermemory REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 20.0,
        max_retries: int = 2,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.SUPERMEMORY_API_KEY
        self.base_url = (base_url or settings.SUPERMEMORY_BASE_URL or _BASE_URL).rstrip(
            "/"
        )
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
        messages: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
        dreaming: str = "dynamic",
        custom_id: str | None = None,
        entity_context: str | None = None,
    ) -> dict[str, Any] | None:
        """Ingest or update a chat session.

        Keep ``conversation_id`` stable across turns so Supermemory treats
        the whole session as one document and only processes what's new
        (its recommended dynamic-dreaming pattern; re-ingests under the
        same id diff-bill — only net-new tokens are metered).

        ``messages``: [{role: "user"|"assistant"|"system"|"tool", content: str}]

        ``custom_id`` defaults to a sanitized ``conversation_id`` — the stable
        identity the billing/dedup path keys on. ``entity_context`` grounds
        extraction ("User is X, talking to Miriam") per the rules doc.
        """
        if not await self._can_call(conversation_id):
            return None
        payload: dict[str, Any] = {
            "conversationId": conversation_id,
            # v4 current is singular containerTag (see the cURL examples);
            # the OpenAPI snippet for /v4/conversations also lists plural
            # containerTags (deprecated array form). Send both so neither
            # schema drift breaks writes.
            "containerTag": container_tag,
            "containerTags": [container_tag],
            # Stable identity for diff billing / dedup. Sanitized: raw
            # conversation ids contain colons, which customId forbids.
            "customId": custom_id_for(custom_id or conversation_id),
            "messages": messages,
            "dreaming": dreaming,
        }
        if entity_context:
            payload["entityContext"] = entity_context[:1500]
        if metadata:
            payload["metadata"] = _flat_metadata(metadata)
        return await self._post("/v4/conversations", payload)

    async def add_document(
        self,
        container_tag: str,
        content: str,
        custom_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        task_type: str = "memory",
        dreaming: str = "dynamic",
        entity_context: str | None = None,
        filter_by_metadata: dict[str, Any] | None = None,
        document_date: str | None = None,
    ) -> dict[str, Any] | None:
        """Send raw content (text, notes, articles) through the pipeline.

        ``task_type="memory"`` extracts facts + updates the profile +
        indexes chunks. ``task_type="superrag"`` only chunks/embeds and is
        5x cheaper, but does not affect what memory knows about the user —
        use it for reference material that should be searchable, not
        remembered. ``filter_by_metadata`` scopes which existing memories
        the new write builds on; ``entity_context`` grounds extraction.
        ``document_date`` (YYYY-MM-DD or ISO 8601) marks when the content is
        *from* — memory extraction resolves relative dates against it
        instead of the upload time (backfill doc).
        """
        if not content:
            return None
        payload: dict[str, Any] = {
            "content": content,
            "containerTag": container_tag,
            "taskType": task_type,
            "dreaming": dreaming,
        }
        if custom_id:
            payload["customId"] = custom_id_for(custom_id)
        if document_date:
            payload["documentDate"] = document_date
        if entity_context:
            payload["entityContext"] = entity_context[:1500]
        if filter_by_metadata:
            payload["filterByMetadata"] = filter_by_metadata
        if metadata:
            payload["metadata"] = _flat_metadata(metadata)
        return await self._post("/v3/documents", payload)

    async def batch_add_documents(
        self,
        container_tag: str,
        documents: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Backfill up to 600 documents in one call (historical imports).

        Each doc: {content, customId?, documentDate?, metadata?}. Sort
        oldest→newest and set ``documentDate`` on every doc so newer facts
        correctly supersede older ones (backfill doc).
        """
        if not documents:
            return None
        cleaned: list[dict[str, Any]] = []
        for doc in documents:
            entry: dict[str, Any] = {"content": doc.get("content", "")}
            if doc.get("customId") or doc.get("custom_id"):
                entry["customId"] = custom_id_for(
                    str(doc.get("customId") or doc.get("custom_id"))
                )
            date = doc.get("documentDate") or doc.get("document_date")
            if date:
                entry["documentDate"] = str(date)
            meta = doc.get("metadata")
            if isinstance(meta, dict):
                entry["metadata"] = _flat_metadata(meta)
            cleaned.append(entry)
        return await self._post(
            "/v3/documents/batch",
            {"containerTag": container_tag, "documents": cleaned},
        )

    async def list_documents(
        self,
        container_tags: list[str],
        limit: int = 50,
        page: int = 1,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Paginated document list (POST /v3/documents/list).

        Note the plural ``containerTags`` array here — this v3 endpoint
        predates the singular ``containerTag`` convention.
        """
        payload: dict[str, Any] = {
            "containerTags": container_tags,
            "limit": min(limit, 200),
            "page": page,
        }
        if filters:
            payload["filters"] = filters
        data = await self._post("/v3/documents/list", payload)
        if not data:
            return {"memories": [], "pagination": {}}
        return {
            "memories": data.get("memories") or [],
            "pagination": data.get("pagination") or {},
        }

    async def update_document(
        self,
        doc_id: str,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Update a document. Content changes reprocess fully (and bill the
        delta unless sent as an update under the same customId); metadata-only
        changes update in place with no reindexing."""
        payload: dict[str, Any] = {}
        if content is not None:
            payload["content"] = content
        if metadata is not None:
            payload["metadata"] = _flat_metadata(metadata)
        if not payload:
            return None
        return await self._patch(f"/v3/documents/{doc_id}", payload)

    async def delete_document(self, doc_id: str) -> dict[str, Any] | None:
        """Permanently delete one document (no recovery)."""
        return await self._delete(f"/v3/documents/{doc_id}")

    async def get_processing_documents(
        self, view: str = "active"
    ) -> dict[str, Any]:
        """Live in-flight queue (view=active|pending|all; all includes failed)."""
        data = await self._get(f"/v3/documents/processing?view={view}")
        if not data:
            return {"documents": []}
        return {"documents": data.get("documents") or []}

    async def get_document(self, doc_id: str) -> dict[str, Any] | None:
        """Fetch document status: queued | extracting | … | done | failed."""
        return await self._get(f"/v3/documents/{doc_id}")

    async def wait_until_done(
        self,
        doc_id: str,
        timeout: float = 60.0,
        interval: float = 1.5,
    ) -> dict[str, Any] | None:
        """Poll document status until done/failed or timeout.

        Requires both ``status`` and ``dreamingStatus`` to be done when the
        server reports dreaming status — with dynamic dreaming, chunks can
        be indexed (``status: done``) while memory extraction is still
        batching (backfill doc).
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            doc = await self.get_document(doc_id)
            if not doc:
                return None
            status = (doc.get("status") or "").lower()
            if status == "failed":
                return doc
            if status == "done":
                dreaming = (doc.get("dreamingStatus") or "").lower()
                if not dreaming or dreaming in ("done", "failed"):
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
        query: str | None = None,
        include: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        threshold: float | None = None,
        buckets: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch the always-on user profile plus optional search results.

        Pass ``query`` to get ``searchResults`` in the same call — one
        search-query meter instead of profile + search separately.
        ``include`` selects static/dynamic/buckets sections; ``buckets``
        restricts bucket keys. Returns ``{profile: {static: [], dynamic:
        [], buckets: {}}, search_results: {results: [], total, timing}}``
        (empty on failure).
        """
        payload: dict[str, Any] = {"containerTag": container_tag}
        if query:
            payload["q"] = query
        if include:
            payload["include"] = include
        if buckets:
            payload["buckets"] = buckets
        if filters:
            payload["filters"] = filters
        if threshold is not None:
            payload["threshold"] = threshold
        data = await self._post("/v4/profile", payload)
        if not data:
            return {
                "profile": {"static": [], "dynamic": [], "buckets": {}},
                "search_results": None,
            }
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
        filters: dict[str, Any] | None = None,
        threshold: float | None = None,
        rerank: bool = False,
        rewrite_query: bool = False,
        doc_id: str | None = None,
        include_documents: bool = False,
        include_summaries: bool = False,
        include_forgotten: bool = False,
    ) -> dict[str, Any]:
        """Semantic recall.

        ``search_mode``: memories | documents | hybrid (recommended when the
        pool mixes memory-path facts and superrag-path chunks). Hybrid and
        documents results carry ``chunk`` instead of ``memory`` — callers
        must read ``hit.memory or hit.chunk``. ``rewrite_query`` expands
        short queries (extra latency, no extra cost); ``rerank`` re-scores
        (+~100ms). Returns ``{results: [...], total: n}`` (empty on failure).
        """
        if not query:
            return {"results": [], "total": 0}
        payload: dict[str, Any] = {
            "containerTag": container_tag,
            "q": query,
            "searchMode": search_mode,
            "limit": limit,
            "include": {
                "relatedMemories": include_related,
                "documents": include_documents
                or search_mode in ("documents", "hybrid"),
                "summaries": include_summaries,
                "forgottenMemories": include_forgotten,
            },
            "rerank": rerank,
            "rewriteQuery": rewrite_query,
        }
        if doc_id:
            payload["docId"] = doc_id
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
        memories: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Write facts directly, bypassing the document pipeline.

        ``memories``: [{content, isStatic?, metadata?}]. Use when the agent
        already knows the exact fact (a preference, a correction).
        """
        if not memories:
            return None
        payload: dict[str, Any] = {
            "containerTag": container_tag,
            "memories": list(memories),
        }
        return await self._post("/v4/memories", payload)

    async def update_memory(
        self,
        container_tag: str,
        new_content: str,
        memory_id: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Versioned update: creates a new version, original keeps isLatest=False."""
        if memory_id is None and content is None:
            logger.warning("update_memory requires id or content")
            return None
        payload: dict[str, Any] = {
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
        memory_id: str | None = None,
        content: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Soft-delete a single memory (excluded from search, kept for audit)."""
        if memory_id is None and content is None:
            logger.warning("forget_memory requires id or content")
            return None
        payload: dict[str, Any] = {"containerTag": container_tag}
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
        query: str | None = None,
        ids: list[str] | None = None,
        dry_run: bool = True,
        reason: str | None = None,
        max_forget: int = 100,
        threshold: float | None = None,
    ) -> dict[str, Any] | None:
        """Semantic bulk forget ('forget everything about X') with dry run.

        Always preview first (dry_run=True -> candidates), then apply with
        the returned ``ids`` so the delete is bound to exactly the reviewed
        set instead of re-running the semantic match. ``threshold`` bounds
        the blast radius in query mode; ``max_forget`` caps it (1-500).
        """
        if query is None and not ids:
            logger.warning("forget_matching requires query or ids")
            return None
        payload: dict[str, Any] = {"containerTag": container_tag, "dryRun": dry_run}
        if query:
            payload["query"] = query
            payload["maxForget"] = max_forget
            if threshold is not None:
                payload["threshold"] = threshold
        if ids:
            payload["ids"] = ids
        if reason:
            payload["reason"] = reason
        return await self._post("/v4/memories/forget-matching", payload)

    # ------------------------------------------------------------------
    # Inferred-memory review (/docs/recall/memory-review)
    # ------------------------------------------------------------------

    async def list_inferred(self, container_tag: str) -> dict[str, Any]:
        """List unreviewed inferred (derive) memories awaiting review.

        Up to 50, strongest first. Returns ``{memories: [...], total: n}``
        (empty on failure). Inferred memories are down-weighted in search
        until approved.
        """
        data = await self._get(f"/v3/container-tags/{container_tag}/inferred")
        if not data:
            return {"memories": [], "total": 0}
        return {
            "memories": data.get("memories") or [],
            "total": int(data.get("total") or 0),
        }

    async def review_inferred(
        self, container_tag: str, memory_id: str, action: str
    ) -> dict[str, Any] | None:
        """Record a review decision: approve | decline | undo.

        approve clears isInference (ranks like a stated fact); decline
        forgets it; undo restores the unreviewed inferred state.
        """
        if action not in ("approve", "decline", "undo"):
            logger.warning("review_inferred unknown action: %s", action)
            return None
        return await self._post(
            f"/v3/container-tags/{container_tag}/inferred/{memory_id}/review",
            {"action": action},
        )

    # ------------------------------------------------------------------
    # Container lifecycle + GDPR (/docs/concepts/container-tags)
    # ------------------------------------------------------------------

    async def get_profile_buckets(
        self, container_tag: str
    ) -> list[dict[str, Any]]:
        """Effective bucket definitions for a tag (org merged with space).

        POST /v4/profile/buckets → [{key, description}]. Empty on failure.
        """
        data = await self._post("/v4/profile/buckets", {"containerTag": container_tag})
        if not data:
            return []
        return data.get("buckets") or []

    async def get_container_settings(
        self, container_tag: str
    ) -> dict[str, Any] | None:
        """Read a tag's settings (name, entityContext, space profileBuckets)."""
        return await self._get(f"/v3/container-tags/{container_tag}")

    async def update_container_settings(
        self,
        container_tag: str,
        entity_context: str | None = None,
        name: str | None = None,
        profile_buckets: list[dict[str, str]] | None = None,
    ) -> dict[str, Any] | None:
        """Update per-container settings (entityContext grounds extraction).

        Entity context persists on the tag and combines with org-level
        filter prompts. Max 1500 chars. ``profile_buckets`` (space-level,
        add-only on top of org buckets) replaces the tag's own list — pass
        the full set the space should add.
        """
        payload: dict[str, Any] = {}
        if entity_context:
            payload["entityContext"] = entity_context[:1500]
        if name:
            payload["name"] = name
        if profile_buckets is not None:
            payload["profileBuckets"] = profile_buckets
        if not payload:
            return None
        return await self._request(
            "PATCH", f"/v3/container-tags/{container_tag}", json=payload
        )

    async def merge_container_tags(
        self, source_tags: list[str], target_tag: str
    ) -> dict[str, Any] | None:
        """Merge source tags into a target (docs move, sources deleted)."""
        if not source_tags or not target_tag:
            logger.warning("merge_container_tags requires sources and target")
            return None
        return await self._post(
            "/v3/container-tags/merge",
            {"sourceTags": source_tags, "targetTag": target_tag},
        )

    async def get_merge_status(self, merge_id: str) -> dict[str, Any] | None:
        """Poll a queued container-tag merge."""
        return await self._get(f"/v3/container-tags/merge/{merge_id}")

    async def list_memories_with_history(
        self, container_tag: str, limit: int = 50
    ) -> dict[str, Any]:
        """List latest memory entries with update history (audit trail)."""
        data = await self._post(
            "/v4/memories/list",
            {"containerTag": container_tag, "limit": limit},
        )
        if not data:
            return {"memories": [], "total": 0}
        memories = data.get("memories") or data.get("results") or []
        total = data.get("total")
        return {
            "memories": memories,
            "total": int(total) if total is not None else len(memories),
        }

    async def delete_documents_bulk(
        self,
        ids: list[str] | None = None,
        container_tags: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Bulk delete documents by ids or whole container tags."""
        payload: dict[str, Any] = {}
        if ids:
            payload["ids"] = ids
        if container_tags:
            payload["containerTags"] = container_tags
        if not payload:
            logger.warning("delete_documents_bulk requires ids or container_tags")
            return None
        return await self._request("DELETE", "/v3/documents/bulk", json=payload)

    async def delete_container_tag(self, container_tag: str) -> dict[str, Any] | None:
        """Delete a container and all its documents/memories (GDPR erasure)."""
        return await self._request("DELETE", f"/v3/container-tags/{container_tag}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _can_call(self, label: str) -> bool:
        if not self.enabled:
            return False
        if not label or not label.strip():
            return False
        return True

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        return await self._request("POST", path, json=payload)

    async def _get(self, path: str) -> dict[str, Any] | None:
        return await self._request("GET", path)

    async def _patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        return await self._request("PATCH", path, json=payload)

    async def _delete(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        return await self._request("DELETE", path, json=payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        retries: int | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        attempts = self.max_retries if retries is None else retries
        for attempt in range(attempts + 1):
            try:
                resp = await self._client.request(method, path, json=json)
                if resp.status_code in _RETRYABLE_STATUS and attempt < attempts:
                    await asyncio.sleep(0.3 * (2**attempt))
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
                    await asyncio.sleep(0.3 * (2**attempt))
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


_supermemory_client: SupermemoryClient | None = None


def get_supermemory_client() -> SupermemoryClient:
    """Get the process-wide Supermemory client singleton."""
    global _supermemory_client
    if _supermemory_client is None:
        _supermemory_client = SupermemoryClient()
    return _supermemory_client


def _flat_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Keep only flat values (Supermemory's metadata rule).

    Scalars pass through; arrays of strings pass through (the add-document
    OpenAPI allows string arrays, e.g. for array_contains filters). Nested
    objects / mixed arrays are dropped — that data belongs in content, not
    metadata (rules doc).
    """
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
        elif (
            isinstance(value, list)
            and value
            and all(isinstance(v, str) for v in value)
        ):
            out[key] = value
    return out

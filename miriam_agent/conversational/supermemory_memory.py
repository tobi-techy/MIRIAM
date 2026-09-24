"""Supermemory-backed memory service for Miriam Financial Agent.

Sits between the agent loop and the Supermemory API. High-level operations
used by the chat endpoints and tools:

- ``build_memory_facts``  read path: always-on profile + query search,
  rendered as the ``memory_facts`` the system prompt consumes. Uses the
  single-call ``profile(q=...)`` pattern (profile + searchResults in one
  request = one search meter) and falls back to a separate search.
- ``ingest_turn``         write path: send each user/assistant turn under
  a stable conversation id so Supermemory keeps the session as one
  document and extracts facts into the graph (diff-billed on re-ingest).
- ``search``              explicit recall for the ``search_memory`` tool
  (memories | documents | hybrid; reads ``memory or chunk``).
- ``remember`` / ``forget`` / ``update``  explicit memory maintenance for
  user corrections ("that's not right anymore"). Forget uses dry-run
  preview -> ids apply so deletes are bound to the reviewed set.
- ``list_inferred`` / ``review_inferred``  low-confidence derive queue.
- ``erase_user_data``     GDPR/container erasure.

Everything is fail-open: without a key or on API errors the service
returns empty data so the agent still works, just without memory.
"""

import logging
from typing import Any, cast

from miriam_agent.integrations.supermemory_client import (
    SupermemoryClient,
    get_supermemory_client,
)

logger = logging.getLogger(__name__)

MAX_FACTS = 12


class SupermemoryMemory:
    """High-level memory operations backed by Supermemory."""

    def __init__(self, client: SupermemoryClient | None = None):
        self.client = client or get_supermemory_client()

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    async def build_memory_facts(
        self,
        container_tag: str,
        query: str | None = None,
        limit: int = MAX_FACTS,
        include_related: bool = True,
        search_mode: str = "memories",
        threshold: float | None = None,
        rerank: bool = False,
    ) -> list[dict[str, Any]]:
        """Assemble the memory block shown to the LLM every turn.

        Single-call pattern: ``profile(q=query)`` returns static + dynamic
        + buckets + searchResults together (one search meter). A separate
        ``search`` is only issued when the profile carried no search
        results but a query was asked.

        Handles both response shapes: memory hits (``memory``) and
        SuperRAG chunk hits (``chunk`` or ``chunks[]``) — callers must read
        ``memory or chunk`` per the search docs. Related graph edges come
        from ``context.related`` plus ``context.parents/children`` (the
        updates/extends/derives links from the quickstart shape).

        Each fact: ``{"type": str, "content": str}`` — the shape
        ``build_system_prompt`` renders as ``- [type] content``.
        """
        if not self.enabled:
            return []

        facts: list[dict[str, Any]] = []
        seen_contents: set[str] = set()

        def add(kind: str, content: str) -> None:
            content = (content or "").strip()
            if not content or content in seen_contents:
                return
            seen_contents.add(content)
            facts.append({"type": kind, "content": content})

        search_results: dict[str, Any] | None = None
        try:
            result = await self.client.profile(container_tag, query=query)
            profile: dict[str, Any] = result.get("profile", {}) or {}
            for item in profile.get("static", []):
                add("profile", item if isinstance(item, str) else str(item))
            for item in profile.get("dynamic", []):
                add("recent", item if isinstance(item, str) else str(item))
            buckets: dict[str, Any] = profile.get("buckets", {})
            for bucket_key, raw_items in buckets.items():
                if not isinstance(raw_items, list):
                    continue
                items = cast(list[Any], raw_items)
                for item in items[:2]:
                    text = item if isinstance(item, str) else str(item)
                    add(f"bucket:{bucket_key}", text)
            search_results = result.get("search_results")
        except Exception as e:
            logger.warning("Supermemory profile failed: %s", e)

        # Query-scoped recall (memory graph + related edges). Prefer the
        # searchResults that rode along with the profile call; only pay
        # for a second request when the profile carried none.
        hits: list[dict[str, Any]] = []
        if isinstance(search_results, dict):
            hits = search_results.get("results") or []
        if query and not hits:
            try:
                search_result = await self.client.search(
                    container_tag,
                    query,
                    search_mode=search_mode,
                    limit=limit,
                    include_related=include_related,
                    threshold=threshold,
                    rerank=rerank,
                )
                hits = search_result.get("results", [])
            except Exception as e:
                logger.warning("Supermemory search failed: %s", e)
        for hit in hits:
            for text, source in _hit_texts(hit):
                add(source, text)
                if len(facts) >= limit * 2:
                    break
            if include_related:
                for rel_text in _related_texts(hit)[:1]:
                    add("related", rel_text)

        return facts[:limit]

    async def search(
        self,
        container_tag: str,
        query: str,
        limit: int = 5,
        search_mode: str = "memories",
        include_related: bool = False,
        threshold: float | None = None,
        rerank: bool = False,
        rewrite_query: bool = False,
    ) -> list[dict[str, Any]]:
        """Explicit semantic search (backs the ``search_memory`` tool).

        Reads ``hit.memory or hit.chunk`` (plus ``chunks[]`` expansion for
        document hits) so hybrid / documents modes ground on SuperRAG
        content instead of dropping it.
        """
        if not self.enabled or not query:
            return []
        try:
            result = await self.client.search(
                container_tag,
                query,
                search_mode=search_mode,
                limit=limit,
                include_related=include_related,
                threshold=threshold,
                rerank=rerank,
                rewrite_query=rewrite_query,
            )
            out: list[dict[str, Any]] = []
            for hit in result.get("results", []):
                for text, source in _hit_texts(hit):
                    meta: dict[str, Any] = hit.get("metadata") or {}
                    out.append(
                        {
                            "content": text,
                            "type": source,
                            "similarity": hit.get("similarity"),
                            "metadata": meta,
                        }
                    )
            return out
        except Exception as e:
            logger.warning("Supermemory search failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def ingest_turn(
        self,
        container_tag: str,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        metadata: dict[str, Any] | None = None,
        entity_context: str | None = None,
    ) -> dict[str, Any] | None:
        """Send one user/assistant turn into the memory graph.

        Uses a stable ``conversation_id`` (also sent as ``customId`` for
        diff billing) so the whole session stays one document and
        Supermemory only processes what's new (its recommended
        dynamic-dreaming pattern).
        """
        if not self.enabled or not conversation_id:
            return None
        messages: list[dict[str, Any]] = []
        if user_message:
            messages.append({"role": "user", "content": user_message})
        if assistant_message:
            messages.append({"role": "assistant", "content": assistant_message})
        if not messages:
            return None
        try:
            return await self.client.ingest_conversation(
                container_tag=container_tag,
                conversation_id=conversation_id,
                messages=messages,
                metadata=metadata,
                custom_id=conversation_id,
                entity_context=entity_context,
            )
        except Exception as e:
            logger.warning("Supermemory ingest failed: %s", e)
            return None

    async def remember(
        self,
        container_tag: str,
        content: str,
        is_static: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Write an explicit fact directly (preference, trait, correction)."""
        if not self.enabled or not content:
            return None
        try:
            return await self.client.create_memories(
                container_tag,
                [
                    {
                        "content": content,
                        "isStatic": is_static,
                        "metadata": metadata or {},
                    }
                ],
            )
        except Exception as e:
            logger.warning("Supermemory remember failed: %s", e)
            return None

    async def forget(
        self,
        container_tag: str,
        query: str | None = None,
        memory_id: str | None = None,
        dry_run: bool = True,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Forget memories matching a query/topic (dry-run safe by default)."""
        if not self.enabled:
            return None
        try:
            if memory_id:
                return await self.client.forget_memory(
                    container_tag, memory_id=memory_id, reason=reason
                )
            return await self.client.forget_matching(
                container_tag, query=query, dry_run=dry_run, reason=reason
            )
        except Exception as e:
            logger.warning("Supermemory forget failed: %s", e)
            return None

    async def forget_exact(
        self,
        container_tag: str,
        query: str,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Bound bulk forget: dry-run preview, then apply by ids.

        Applying with a ``query`` re-runs the semantic match, so the result
        can drift from the preview if the container changed in between. This
        helper takes the ids from the preview and sends them back as
        ``ids`` on the apply — the delete is then bound to exactly the
        reviewed set (per the forget-matching docs tip).
        """
        if not self.enabled or not query:
            return None
        try:
            preview = await self.client.forget_matching(
                container_tag, query=query, dry_run=True, reason=reason
            )
            if not preview:
                return None
            candidates = preview.get("candidates") or []
            ids = [c.get("id") for c in candidates if c.get("id")]
            if not ids:
                return preview
            return await self.client.forget_matching(
                container_tag, ids=ids, dry_run=False, reason=reason
            )
        except Exception as e:
            logger.warning("Supermemory bound forget failed: %s", e)
            return None

    async def update(
        self,
        container_tag: str,
        new_content: str,
        memory_id: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Versioned update: new version, original keeps isLatest=False."""
        if not self.enabled or not new_content:
            return None
        try:
            return await self.client.update_memory(
                container_tag,
                new_content,
                memory_id=memory_id,
                content=content,
                metadata=metadata,
            )
        except Exception as e:
            logger.warning("Supermemory update failed: %s", e)
            return None

    async def list_inferred(
        self, container_tag: str
    ) -> list[dict[str, Any]]:
        """Unreviewed inferred (derive) memories awaiting review."""
        if not self.enabled:
            return []
        try:
            result = await self.client.list_inferred(container_tag)
            return result.get("memories") or []
        except Exception as e:
            logger.warning("Supermemory list_inferred failed: %s", e)
            return []

    async def get_buckets(self, container_tag: str) -> list[dict[str, Any]]:
        """Effective profile bucket definitions (org merged with space)."""
        if not self.enabled:
            return []
        try:
            return await self.client.get_profile_buckets(container_tag)
        except Exception as e:
            logger.warning("Supermemory get_buckets failed: %s", e)
            return []

    async def review_inferred(
        self, container_tag: str, memory_id: str, action: str
    ) -> dict[str, Any] | None:
        """Approve | decline | undo a single inferred memory."""
        if not self.enabled:
            return None
        try:
            return await self.client.review_inferred(
                container_tag, memory_id, action
            )
        except Exception as e:
            logger.warning("Supermemory review failed: %s", e)
            return None

    async def erase_user_data(self, container_tag: str) -> bool:
        """Delete a container and all its documents/memories (GDPR erasure)."""
        if not self.enabled:
            return False
        try:
            result = await self.client.delete_container_tag(container_tag)
            return result is not None
        except Exception as e:
            logger.warning("Supermemory erasure failed: %s", e)
            return False


def _hit_texts(hit: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract (text, source) pairs from a search hit.

    Hybrid/documents hits carry ``chunk`` (or a ``chunks[]`` list) instead
    of ``memory`` — read ``memory or chunk`` per the search docs so SuperRAG
    grounding is never dropped.
    """
    meta: dict[str, Any] = hit.get("metadata") or {}
    source = str(meta.get("source") or meta.get("kind") or "memory")
    out: list[tuple[str, str]] = []
    memory = (hit.get("memory") or "").strip()
    if memory:
        out.append((memory, source))
    chunk = hit.get("chunk")
    if isinstance(chunk, str) and chunk.strip():
        out.append((chunk.strip(), meta.get("source") or "document"))
    elif isinstance(chunk, dict):
        content = (chunk.get("content") or chunk.get("text") or "").strip()
        if content:
            out.append((content, meta.get("source") or "document"))
    chunks = hit.get("chunks")
    if isinstance(chunks, list):
        for c in chunks:
            if isinstance(c, str) and c.strip():
                out.append((c.strip(), "document"))
            elif isinstance(c, dict):
                content = (c.get("content") or c.get("text") or "").strip()
                if content:
                    out.append((content, "document"))
    return out


def _related_texts(hit: dict[str, Any]) -> list[str]:
    """One-hop related edge texts: related + parents + children.

    The graph links facts via updates/extends/derives; the API surfaces
    them as ``context.related`` and/or ``context.parents/children`` (the
    quickstart entity-chain shape). Surface all three so chain facts help
    even when the top hit alone is thin.
    """
    context: dict[str, Any] = hit.get("context") or {}
    out: list[str] = []
    for key in ("related", "parents", "children"):
        items = context.get(key) or []
        if not isinstance(items, list):
            continue
        for rel in items:
            if not isinstance(rel, dict):
                continue
            text = (rel.get("memory") or rel.get("chunk") or "").strip()
            if text:
                out.append(text)
    return out

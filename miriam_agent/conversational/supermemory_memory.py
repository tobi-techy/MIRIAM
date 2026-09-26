"""Supermemory-backed memory service for Miriam Financial Agent.

Sits between the agent loop and the Supermemory API. High-level operations
used by the chat endpoints and tools:

- ``build_memory_facts``  read path: always-on profile + query search,
  rendered as the ``memory_facts`` the system prompt consumes.
- ``ingest_turn``         write path: send each user/assistant turn under
  a stable per-person, per-channel scope so a person keeps one connected
  graph rather than a new document per session.
- ``ingest_note``         append one known fact (a name, a plan) to that
  same document instead of hand-writing a memory per fact.
- ``ensure_container``    name the person's space and ground extraction
  with ``entityContext`` so facts attach to the right person.
- ``search``              explicit recall for the ``search_memory`` tool.
- ``remember_person_fact`` / ``forget`` / ``update``  explicit memory
  maintenance for durable anchors and user corrections.

Everything is fail-open: without a key or on API errors the service
returns empty data so the agent still works, just without memory.
"""

import logging
import time
from typing import Any, cast

from miriam_agent.integrations.supermemory_client import (
    SupermemoryClient,
    get_supermemory_client,
)

logger = logging.getLogger(__name__)

MAX_FACTS = 12

# Per-process memo for container grounding. The settings call is one round trip
# and its result never changes for the life of the process, so it must not run
# on every turn. A failed attempt is cooled down rather than retried forever.
_CONTAINER_READY: set[str] = set()
_CONTAINER_RETRY_AT: dict[str, float] = {}
_CONTAINER_RETRY_COOLDOWN = 300.0


class SupermemoryMemory:
    """High-level memory operations backed by Supermemory."""

    def __init__(self, client: SupermemoryClient | None = None):
        self.client = client or get_supermemory_client()

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    async def ensure_container(
        self,
        container_tag: str,
        user_id: str,
        name: str | None = None,
    ) -> bool:
        """Give a person's container a display name and extraction grounding.

        Idempotent and best-effort, once per process per container: a failure
        only costs grounding, never a turn. ``user_id`` is passed explicitly so
        the entity context names the person rather than the hashed tag.

        Returns ``True`` when the container is grounded.
        """
        if not self.enabled or not container_tag:
            return False
        if container_tag in _CONTAINER_READY:
            return True
        now = time.monotonic()
        if _CONTAINER_RETRY_AT.get(container_tag, 0.0) > now:
            return False
        from miriam_agent.integrations.supermemory_client import person_entity_context

        result = await self.client.update_container_settings(
            container_tag,
            name=name,
            entity_context=person_entity_context(user_id, name),
        )
        if result is None:
            _CONTAINER_RETRY_AT[container_tag] = now + _CONTAINER_RETRY_COOLDOWN
            return False
        _CONTAINER_READY.add(container_tag)
        return True

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    async def build_memory_facts(
        self,
        container_tag: str,
        query: str | None = None,
        limit: int = MAX_FACTS,
        include_related: bool = True,
    ) -> list[dict[str, Any]]:
        """Assemble the memory block shown to the LLM every turn.

        Combines Supermemory's always-on profile (static + dynamic) with a
        query-scoped search, deduped and capped at ``limit`` facts.

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

        try:
            result = await self.client.profile(container_tag, query=query)
            profile: dict[str, Any] = result.get("profile", {}) or {}
            for item in profile.get("static", []):
                add("profile", item)
            for item in profile.get("dynamic", []):
                add("recent", item)
            buckets: dict[str, Any] = profile.get("buckets", {})
            for bucket_key, raw_items in buckets.items():
                if not isinstance(raw_items, list):
                    continue
                items = cast(list[Any], raw_items)
                for item in items[:2]:
                    add(f"bucket:{bucket_key}", item)
        except Exception as e:
            logger.warning("Supermemory profile failed: %s", e)

        # Query-scoped recall (memory graph + related edges).
        if query:
            try:
                search_result = await self.client.search(
                    container_tag,
                    query,
                    search_mode="memories",
                    limit=limit,
                    include_related=include_related,
                )
                for hit in search_result.get("results", []):
                    memory = (hit.get("memory") or "").strip()
                    if not memory:
                        continue
                    meta: dict[str, Any] = hit.get("metadata") or {}
                    source = meta.get("source") or meta.get("kind") or "memory"
                    add(str(source), memory)
                    # Surface one-hop related edges so chain facts can help
                    # even when the top hit alone is thin.
                    if include_related:
                        context: dict[str, Any] = hit.get("context") or {}
                        related: list[Any] = context.get("related") or []
                        for rel in related[:1]:
                            rel_text = (rel.get("memory") or "").strip()
                            if rel_text:
                                add("related", rel_text)
            except Exception as e:
                logger.warning("Supermemory search failed: %s", e)

        return facts[:limit]

    async def search(
        self,
        container_tag: str,
        query: str,
        limit: int = 5,
        search_mode: str = "memories",
        include_related: bool = False,
    ) -> list[dict[str, Any]]:
        """Explicit semantic search (backs the ``search_memory`` tool)."""
        if not self.enabled or not query:
            return []
        try:
            result = await self.client.search(
                container_tag,
                query,
                search_mode=search_mode,
                limit=limit,
                include_related=include_related,
            )
            out: list[dict[str, Any]] = []
            for hit in result.get("results", []):
                memory = (hit.get("memory") or "").strip()
                if not memory:
                    continue
                meta: dict[str, Any] = hit.get("metadata") or {}
                out.append(
                    {
                        "content": memory,
                        "type": meta.get("source", "memory"),
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
    ) -> dict[str, Any] | None:
        """Send one user/assistant turn into the memory graph.

        Uses a stable ``conversation_id`` so the whole session stays one
        document and Supermemory only processes what's new (its recommended
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
            )
        except Exception as e:
            logger.warning("Supermemory ingest failed: %s", e)
            return None

    async def ingest_note(
        self,
        container_tag: str,
        conversation_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Append one structured fact to an already-open conversation document.

        This is the right shape for facts Miriam knows exactly (a name, a plan,
        a correction): it lands in the graph without hand-writing every memory,
        and it appends to the same stable document instead of spawning a new
        source document per fact, which is what fragmented the graph before.
        """
        content = (content or "").strip()
        if not self.enabled or not conversation_id or not content:
            return None
        try:
            return await self.client.ingest_conversation(
                container_tag=container_tag,
                conversation_id=conversation_id,
                messages=[{"role": "system", "content": content}],
                metadata=metadata,
            )
        except Exception as e:
            logger.warning("Supermemory note ingest failed: %s", e)
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

    async def remember_person_fact(
        self,
        container_tag: str,
        content: str,
        *,
        kind: str = "fact",
        is_static: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Write one durable, entity-centric fact about the person.

        Reserved for anchors where ``isStatic`` matters (the profile's
        permanent traits) or where extraction cannot be trusted to catch the
        fact. Everything conversational should go through ``ingest_turn`` /
        ``ingest_note``: this path creates a lightweight source document per
        call, so using it for every turn would re-fragment the graph.
        """
        metadata: dict[str, Any] = {"kind": kind, "source": "miriam"}
        if extra:
            metadata.update(extra)
        return await self.remember(
            container_tag, content, is_static=is_static, metadata=metadata
        )

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

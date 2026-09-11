"""Supermemory-backed memory service for Miriam Financial Agent.

Sits between the agent loop and the Supermemory API. High-level operations
used by the chat endpoints and tools:

- ``build_memory_facts``  read path: always-on profile + query search,
  rendered as the ``memory_facts`` the system prompt consumes.
- ``ingest_turn``         write path: send each user/assistant turn under
  a stable conversation id so Supermemory keeps the session as one
  document and extracts facts into the graph.
- ``search``              explicit recall for the ``search_memory`` tool.
- ``remember`` / ``forget`` / ``update``  explicit memory maintenance for
  user corrections ("that's not right anymore").

Everything is fail-open: without a key or on API errors the service
returns empty data so the agent still works, just without memory.
"""

import logging
from typing import Any, Dict, List, Optional

from miriam_agent.integrations.supermemory_client import (
    SupermemoryClient,
    get_supermemory_client,
)

logger = logging.getLogger(__name__)

MAX_FACTS = 12


class SupermemoryMemory:
    """High-level memory operations backed by Supermemory."""

    def __init__(self, client: Optional[SupermemoryClient] = None):
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
        query: Optional[str] = None,
        limit: int = MAX_FACTS,
        include_related: bool = True,
    ) -> List[Dict[str, Any]]:
        """Assemble the memory block shown to the LLM every turn.

        Combines Supermemory's always-on profile (static + dynamic) with a
        query-scoped search, deduped and capped at ``limit`` facts.

        Each fact: ``{"type": str, "content": str}`` — the shape
        ``build_system_prompt`` renders as ``- [type] content``.
        """
        if not self.enabled:
            return []

        facts: List[Dict[str, Any]] = []
        seen_contents: set = set()

        def add(kind: str, content: str) -> None:
            content = (content or "").strip()
            if not content or content in seen_contents:
                return
            seen_contents.add(content)
            facts.append({"type": kind, "content": content})

        try:
            result = await self.client.profile(container_tag, query=query)
            profile = result.get("profile", {})
            for item in profile.get("static", []):
                add("profile", item)
            for item in profile.get("dynamic", []):
                add("recent", item)
            buckets = profile.get("buckets", {})
            if isinstance(buckets, dict):
                for bucket_key, items in buckets.items():
                    if not isinstance(items, list):
                        continue
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
                    meta = hit.get("metadata") or {}
                    source = meta.get("source") or meta.get("kind") or "memory"
                    add(str(source), memory)
                    # Surface one-hop related edges so chain facts can help
                    # even when the top hit alone is thin.
                    if include_related:
                        context = hit.get("context") or {}
                        for rel in (context.get("related") or [])[:1]:
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
    ) -> List[Dict[str, Any]]:
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
            out: List[Dict[str, Any]] = []
            for hit in result.get("results", []):
                memory = (hit.get("memory") or "").strip()
                if not memory:
                    continue
                out.append(
                    {
                        "content": memory,
                        "type": (hit.get("metadata") or {}).get("source", "memory"),
                        "similarity": hit.get("similarity"),
                        "metadata": hit.get("metadata"),
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
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Send one user/assistant turn into the memory graph.

        Uses a stable ``conversation_id`` so the whole session stays one
        document and Supermemory only processes what's new (its recommended
        dynamic-dreaming pattern).
        """
        if not self.enabled or not conversation_id:
            return None
        messages: List[Dict[str, Any]] = []
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

    async def remember(
        self,
        container_tag: str,
        content: str,
        is_static: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Write an explicit fact directly (preference, trait, correction)."""
        if not self.enabled or not content:
            return None
        try:
            return await self.client.create_memories(
                container_tag,
                [{"content": content, "isStatic": is_static, "metadata": metadata or {}}],
            )
        except Exception as e:
            logger.warning("Supermemory remember failed: %s", e)
            return None

    async def forget(
        self,
        container_tag: str,
        query: Optional[str] = None,
        memory_id: Optional[str] = None,
        dry_run: bool = True,
        reason: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
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
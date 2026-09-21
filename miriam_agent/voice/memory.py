"""Layer 3 - VOICE. Memory reads.

Voice is allowed to know who it is talking to. It is not allowed to learn
numbers this way: memory carries facts about the person (income shape,
counterparties, goals, corrections), never balances. A balance that arrives from
memory would be a number Voice could state without STATE ever containing it,
which is exactly what the clamp exists to prevent.

Read-only by construction: this module exposes no write path to Supermemory, and
every call fails open to an empty list so a memory outage costs personality, not
correctness.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

MAX_FACTS = 8


async def read_facts(
    user_id: str,
    query: str | None = None,
    *,
    memory: Any = None,
    limit: int = MAX_FACTS,
) -> list[dict[str, Any]]:
    """Remembered facts about this user, or an empty list.

    ``memory`` is injectable so a test can read exactly the facts it declared,
    and so the orchestrator can pass the shared instance.
    """
    if not user_id:
        return []
    try:
        if memory is None:
            from miriam_agent.conversational.supermemory_memory import (
                SupermemoryMemory,
            )

            memory = SupermemoryMemory()
        if not getattr(memory, "enabled", False):
            return []
        from miriam_agent.integrations.supermemory_client import container_tag_for

        facts = await memory.build_memory_facts(
            container_tag_for(user_id),
            query=query or "What should I know about this user?",
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 - memory must never break a turn
        logger.info("voice memory read failed, continuing without facts: %s", exc)
        return []
    return [fact for fact in (facts or []) if isinstance(fact, dict)][:limit]


__all__ = ["MAX_FACTS", "read_facts"]

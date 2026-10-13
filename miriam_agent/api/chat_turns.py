"""Turn finalization and streaming helpers for the chat endpoints.

Moved verbatim out of :mod:`miriam_agent.api.chat` (blob-ratchet split): the
agent-turn serializer, the SSE frame formatter, and the persistence/memory
helpers shared by the agent, onboarding, and money paths. ``chat`` re-imports
every name below, so its public surface is unchanged.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from miriam_agent.agents.agent_loop import AgentRunResult
from miriam_agent.database.memory import MemoryStore
from miriam_agent.database.models import User
from miriam_agent.integrations.supermemory_client import (
    container_tag_for,
    conversation_scope_for,
    display_name_for,
    person_entity_context,
)
from miriam_agent.observability.correlation import current_trace_id

logger = logging.getLogger(__name__)


def _serialize_agent_result(result: Any) -> dict[str, Any]:
    return {
        "response": result.response,
        "conversation_id": result.conversation_id,
        "messages": list(result.messages),
        "reaction": str(result.reaction or ""),
        "share": None,
        # Same id the inbound request carried (and the header echoes), so the
        # client can join this reply to the tool calls and audit rows behind it.
        "trace_id": getattr(result, "trace_id", "") or current_trace_id(),
    }


def _sse(payload: dict[str, Any]) -> str:
    """Format a dict as a single Server-Sent Events ``data:`` frame.

    Every frame carries the request's trace id, so a streamed reply can be
    joined to the tool calls and audit rows behind it without a second lookup.
    """
    frame = {"trace_id": current_trace_id(), **payload}
    return f"data: {json.dumps(frame)}\n\n"


async def _finish_onboarding_turn(
    memory_store: MemoryStore,
    supermemory_memory: Any,
    user: User,
    message: str,
    onboarding: Any,
) -> dict[str, Any]:
    """Persist and return an onboarding turn (mirrors the agent-turn path)."""
    conv_id = onboarding.conversation_id or f"onboarding:{user.id}"
    await memory_store.store_interaction(
        user_id=user.id,
        role="user",
        content=message,
        conversation_id=conv_id,
        metadata={"channel": "api", "onboarding": True},
    )
    await memory_store.store_interaction(
        user_id=user.id,
        role="assistant",
        content=onboarding.response,
        conversation_id=conv_id,
        metadata={"channel": "api", "onboarding": True},
    )
    await _ingest_to_supermemory(
        supermemory_memory,
        user.id,
        channel="onboarding",
        user_message=message,
        assistant_message=onboarding.response,
        name=_display_name(user),
    )
    payload = onboarding.to_payload(conv_id)
    payload["conversation_history"] = await memory_store.get_conversation_history(
        conv_id, user.id
    )
    payload["trace_id"] = current_trace_id()
    return payload


async def _load_financial_plan(token: str) -> dict[str, Any] | None:
    """Assemble a short plan note from live Go data (fail-open).

    Does not call the retired Go AI ``/api/v1/ai/financial-plan`` endpoint.
    """
    try:
        from miriam_agent.financial.intelligence import financial_plan_live
        from miriam_agent.integrations.go_client import get_go_client

        client = get_go_client()
        plan = await financial_plan_live(client, token)
        if plan:
            return plan
    except Exception as e:
        logger.info("Financial plan unavailable (non-blocking): %s", e)
    return None


async def _load_user_context(memory_store: MemoryStore, user: User) -> dict[str, Any]:
    """Build the context block shown to the LLM about the user."""
    context: dict[str, Any] = {
        "name": user.full_name or user.username,
        "user_id": user.id,
        "roles": list(getattr(user, "roles", None) or []),
    }
    try:
        profile = await memory_store.get_financial_profile(user.id)
        if profile:
            context["monthly_income"] = profile.monthly_income
            context["current_savings"] = profile.current_savings
            context["risk_tolerance"] = profile.risk_tolerance
            context["goals"] = profile.financial_goals
    except Exception:
        pass
    return context


async def _load_memory_facts(
    memory_store: MemoryStore,
    user_id: str,
    query: str | None = None,
    supermemory_memory: Any = None,
) -> list[dict[str, Any]]:
    """Load remembered facts about the user.

    Prefers Supermemory (always-on profile + query-scoped semantic recall),
    falling back to the local SQL memory store when Supermemory is disabled
    or unavailable.
    """
    if supermemory_memory is not None and supermemory_memory.enabled:
        try:
            container_tag = container_tag_for(user_id)
            smart_facts = await supermemory_memory.build_memory_facts(
                container_tag,
                query=query or "What should I know about this user?",
                # Web recall reads web turns: onboarding interview chatter
                # and spectrum/money receipts must not crowd it out. The
                # always-on profile (name and other static anchors) is
                # unaffected — this scopes the query-scoped search only.
                filters={"channel": "web", "source": "miriam"},
            )
            if smart_facts:
                return smart_facts
        except Exception:
            pass  # fail open to local store

    facts: list[dict[str, Any]] = []
    for kind in ("preference", "goal", "financial", "pattern", "onboarding"):
        try:
            entries = await memory_store.retrieve_memory(
                user_id=user_id, memory_type=kind, limit=3
            )
            facts.extend(
                [
                    {"type": kind, "content": e.content, "extra_data": e.extra_data}
                    for e in entries
                ]
            )
        except Exception:
            continue
    return facts[:8]


async def _ingest_to_supermemory(
    supermemory_memory: Any,
    user_id: str,
    *,
    channel: str,
    user_message: str,
    assistant_message: str,
    name: str | None = None,
) -> None:
    """Send a user/assistant turn into Supermemory's memory graph.

    The turn is keyed to a stable per-person, per-channel scope rather than the
    client's conversation id. That is the whole point: a client that starts a
    new conversation per session used to hand Supermemory a new document each
    time, which is how one person's memory ended up as several disconnected
    clusters. With a stable scope, a person's web turns are one document, their
    onboarding another, and dynamic dreaming links the memories across them.

    Fire-and-forget and fail-open: memory ingest must never break the chat
    response, so any error is swallowed here. ``entityContext`` grounds
    extraction about the right person (rules doc: "User is X, talking to
    Miriam"), so a fact like "I'm saving for a house" is filed against
    someone rather than nobody.
    """
    if supermemory_memory is None or not supermemory_memory.enabled:
        return
    try:
        container_tag = container_tag_for(user_id)
        await supermemory_memory.ingest_turn(
            container_tag=container_tag,
            conversation_id=conversation_scope_for(user_id, channel),
            user_message=user_message,
            assistant_message=assistant_message,
            metadata={
                "channel": channel,
                "source": "miriam",
                "agent": "miriam-python",
            },
            entity_context=person_entity_context(user_id, name),
        )
        # The settings call 404s until the container exists, so it runs after
        # the first turn lands. It is memoised after that.
        await supermemory_memory.ensure_container(container_tag, user_id, name=name)
    except Exception as e:
        logger.warning("Memorizing turn failed (non-blocking): %s", e)


def _display_name(user: User) -> str | None:
    """The name Supermemory should label and ground this person's space with."""
    return display_name_for(user.full_name, user.username)


async def _finalize_turn(
    *,
    memory_store: MemoryStore,
    supermemory_memory: Any,
    user: User,
    message: str,
    result: AgentRunResult,
) -> dict[str, Any]:
    """Persist and serialize a finished turn (agent run or ingress short-circuit).

    Shared by the normal path and the ingress gate so a refused/clarified turn
    is recorded exactly like any other exchange.
    """
    await memory_store.store_interaction(
        user_id=user.id,
        role="user",
        content=message,
        conversation_id=result.conversation_id,
        metadata={"channel": "api"},
    )
    await memory_store.store_interaction(
        user_id=user.id,
        role="assistant",
        content=result.response,
        conversation_id=result.conversation_id,
        metadata={"channel": "api"},
    )
    await _ingest_to_supermemory(
        supermemory_memory,
        user.id,
        channel="web",
        user_message=message,
        assistant_message=result.response,
        name=_display_name(user),
    )
    payload = _serialize_agent_result(result)
    payload["conversation_history"] = await memory_store.get_conversation_history(
        result.conversation_id, user.id
    )
    return payload


# Matches currency amounts (₦200k, $1,000.50, 200000 NGN, 1.5m) and bare
# figures. This runs only on money turns, where any standalone number is
# almost certainly an amount, balance, or id — so the final branch is
# deliberately aggressive: no number from a money turn reaches the graph.
_MONEY_NUMBER_RE = re.compile(
    r"[₦$€£]\s?[\d,]+(?:\.\d+)?[kmbKMB]?"
    r"|\b\d[\d,]*\.?\d*\s?(?:₦|NGN|naira|kobo|USD|USDT|\$)\b"
    r"|\b\d[\d,]{3,}(?:\.\d+)?\b"
    r"|\b\d+\.\d{2}\b"
    r"|\b\d+[kmbKMB]\b"
    r"|\b\d+(?:\.\d+)?\b"
)


def _scrub_money_numbers(text: str) -> str:
    """Replace money-like numbers with [amount], keeping names/intent.

    "send 200k to Femi" -> "send [amount] to Femi". Fail-open: on any
    error the caller gets a number-free fallback, never the raw text.
    """
    try:
        cleaned = _MONEY_NUMBER_RE.sub("[amount]", text or "")
        # Collapse accidental doubles from adjacent matches.
        cleaned = re.sub(r"(\[amount\]\s*){2,}", "[amount] ", cleaned)
        return cleaned.strip()
    except Exception:
        return "money turn (details withheld)"

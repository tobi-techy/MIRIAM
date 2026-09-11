"""Chat endpoints for Miriam Financial Agent.

The ``/chat`` endpoint runs the full agent loop. The ``/chat/stream``
endpoint streams tokens via SSE. Money-movement tools are never executed
here — they are returned as ``action_required`` payloads for the client
to show to the user and re-submit with a confirmation.
"""

from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer

from miriam_agent.agents.agent_loop import Agent
from miriam_agent.agents.base import AgentConfig
from miriam_agent.api.dependencies import (
    get_current_user,
    get_go_client_dep,
    get_memory_store,
    get_supermemory_memory_dep,
)
from miriam_agent.database.memory import MemoryStore
from miriam_agent.database.models import User
from miriam_agent.integrations.supermemory_client import container_tag_for
from miriam_agent.safety.policy import SafetyPolicy
from miriam_agent.safety.validator import InputValidator
from miriam_agent.tools import build_tool_registry

router = APIRouter()
security = HTTPBearer()


def _serialize_agent_result(result: Any) -> Dict[str, Any]:
    return {
        "response": result.response,
        "conversation_id": result.conversation_id,
        "requires_confirmation": result.requires_confirmation,
        "cards": [
            {
                "type": "action_confirmation",
                "tool": c["tool"],
                "arguments": c["arguments"],
                "summary": c["summary"],
            }
            for c in result.cards
        ],
    }


@router.post("/chat")
async def chat_with_agent(
    request: Dict[str, Any],
    user: User = Depends(get_current_user),
    memory_store: MemoryStore = Depends(get_memory_store),
    supermemory_memory: Any = Depends(get_supermemory_memory_dep),
) -> Dict[str, Any]:
    """Chat with the financial agent (non-streaming)."""
    message = request.get("message", "")
    conversation_id = request.get("conversation_id")
    approved_actions = request.get("approved_actions")

    if not message:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message cannot be empty",
        )

    validator = InputValidator()
    is_valid, validation_errors = await validator.validate_user_input(
        {"message": message}, "chat"
    )
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation errors: {', '.join(validation_errors)}",
        )

    registry = build_tool_registry()
    agent = Agent(
        registry=registry,
        safety_policy=SafetyPolicy(),
        config=AgentConfig(
            name="financial_agent",
            tools=registry.list_names(),
            system_prompt="Miriam Financial Agent",
        ),
    )

    # Load user context (from Go backend when reachable; local memory otherwise)
    user_context = await _load_user_context(memory_store, user)
    history = await memory_store.get_conversation_history(conversation_id) if conversation_id else []
    memory_facts = await _load_memory_facts(
        memory_store, user.id, query=message, supermemory_memory=supermemory_memory
    )

    try:
        result = await agent.run(
            user_id=user.id,
            token=_bearer_token_from_request(request),
            message=message,
            conversation_id=conversation_id,
            history=history,
            user_context=user_context,
            memory_facts=memory_facts,
            approved_actions=approved_actions,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing chat request: {str(e)}",
        )

    # Persist the exchange.
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
        metadata={
            "channel": "api",
            "requires_confirmation": result.requires_confirmation,
        },
    )

    # Feed the turn into Supermemory's memory graph (fail-open, non-blocking).
    await _ingest_to_supermemory(
        supermemory_memory,
        user.id,
        conversation_id=result.conversation_id,
        user_message=message,
        assistant_message=result.response,
    )

    payload = _serialize_agent_result(result)
    payload["conversation_history"] = await memory_store.get_conversation_history(
        result.conversation_id
    )
    return payload


@router.post("/chat/stream")
async def chat_stream(
    request: Dict[str, Any],
    user: User = Depends(get_current_user),
    memory_store: MemoryStore = Depends(get_memory_store),
    supermemory_memory: Any = Depends(get_supermemory_memory_dep),
) -> StreamingResponse:
    """Stream chat tokens via Server-Sent Events."""
    message = request.get("message", "")
    conversation_id = request.get("conversation_id")
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    registry = build_tool_registry()
    agent = Agent(
        registry=registry,
        safety_policy=SafetyPolicy(),
        config=AgentConfig(name="financial_agent", tools=registry.list_names()),
    )
    history = await memory_store.get_conversation_history(conversation_id) if conversation_id else []
    user_context = await _load_user_context(memory_store, user)
    memory_facts = await _load_memory_facts(
        memory_store, user.id, query=message, supermemory_memory=supermemory_memory
    )

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            async for event in agent.stream_run(
                user_id=user.id,
                token=_bearer_token_from_request(request),
                message=message,
                conversation_id=conversation_id,
                history=history,
                user_context=user_context,
                memory_facts=memory_facts,
            ):
                evt = event["type"]
                if evt == "token":
                    yield _sse({"type": "token", "content": event["content"]})
                elif evt == "tool_call":
                    yield _sse({"type": "tool_call", "tool_call": event["tool_call"]})
                elif evt == "action_required":
                    yield _sse(
                        {
                            "type": "action_required",
                            "tool": event["tool"],
                            "arguments": event["arguments"],
                            "summary": event["summary"],
                        }
                    )
                elif evt == "confirmation_required":
                    yield _sse({"type": "confirmation_required", "message": event["message"]})
                elif evt == "tool_result":
                    yield _sse(
                        {
                            "type": "tool_result",
                            "tool": event["tool"],
                            "status": event["status"],
                            "result": event["result"],
                        }
                    )
                elif evt == "done":
                    yield _sse({"type": "done"})
                    return
            yield _sse({"type": "done"})
        except Exception as e:
            yield _sse({"type": "error", "message": str(e)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/conversations")
async def get_user_conversations(
    user: User = Depends(get_current_user),
    memory_store: MemoryStore = Depends(get_memory_store),
):
    conversations = await memory_store.get_conversations(user.id)
    return {
        "conversations": [
            {
                "id": conv.id,
                "title": conv.title,
                "created_at": conv.created_at.isoformat(),
                "updated_at": conv.updated_at.isoformat(),
            }
            for conv in conversations
        ]
    }


@router.post("/conversations")
async def create_conversation(
    request: Dict[str, Any],
    user: User = Depends(get_current_user),
    memory_store: MemoryStore = Depends(get_memory_store),
):
    title = request.get("title", "New Conversation")
    conversation = await memory_store.create_conversation(user.id, title)
    return {"conversation": {"id": conversation.id, "title": conversation.title}}


@router.get("/conversations/{conversation_id}")
async def get_conversation_messages(
    conversation_id: str,
    user: User = Depends(get_current_user),
    memory_store: MemoryStore = Depends(get_memory_store),
):
    conversations = await memory_store.get_conversations(user.id)
    if conversation_id not in {c.id for c in conversations}:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await memory_store.get_conversation_messages(conversation_id)
    return {
        "messages": [
            {
                "id": msg.id,
                "role": msg.role,
                "content": msg.content,
                "metadata": msg.extra_data,
                "created_at": msg.created_at.isoformat(),
            }
            for msg in messages
        ]
    }


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _bearer_token_from_request(request: Dict[str, Any]) -> str:
    """Extract bearer token passed through the body for Go backend calls."""
    return request.get("token") or request.get("authorization", "").replace("Bearer ", "")


async def _load_user_context(
    memory_store: MemoryStore, user: User
) -> Dict[str, Any]:
    """Build the context block shown to the LLM about the user."""
    context: Dict[str, Any] = {
        "name": user.full_name or user.username,
        "user_id": user.id,
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
    query: Optional[str] = None,
    supermemory_memory: Any = None,
) -> List[Dict[str, Any]]:
    """Load remembered facts about the user.

    Prefers Supermemory (always-on profile + query-scoped semantic recall),
    falling back to the local SQL memory store when Supermemory is disabled
    or unavailable.
    """
    if supermemory_memory is not None and supermemory_memory.enabled:
        try:
            container_tag = container_tag_for(user_id)
            facts = await supermemory_memory.build_memory_facts(
                container_tag, query=query or "What should I know about this user?"
            )
            if facts:
                return facts
        except Exception:
            pass  # fail open to local store

    facts: List[Dict[str, Any]] = []
    for kind in ("preference", "goal", "financial", "pattern"):
        try:
            entries = await memory_store.retrieve_memory(
                user_id=user_id, memory_type=kind, limit=3
            )
            facts.extend(
                [{"type": kind, "content": e.content, "extra_data": e.extra_data} for e in entries]
            )
        except Exception:
            continue
    return facts[:8]


async def _ingest_to_supermemory(
    supermemory_memory: Any,
    user_id: str,
    conversation_id: str,
    user_message: str,
    assistant_message: str,
) -> None:
    """Send a user/assistant turn into Supermemory's memory graph.

    Fire-and-forget and fail-open: memory ingest must never break the chat
    response, so any error is swallowed here.
    """
    if supermemory_memory is None or not supermemory_memory.enabled:
        return
    if not conversation_id:
        return
    try:
        container_tag = container_tag_for(user_id)
        await supermemory_memory.ingest_turn(
            container_tag=container_tag,
            conversation_id=conversation_id,
            user_message=user_message,
            assistant_message=assistant_message,
            metadata={"channel": "api", "agent": "miriam-python"},
        )
    except Exception as e:
        logger.warning("Memorizing turn failed (non-blocking): %s", e)
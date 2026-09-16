"""Chat endpoints for Miriam Financial Agent.

The ``/chat`` endpoint runs the full agent loop. The ``/chat/stream``
endpoint streams tokens via SSE. Money-movement tools are never executed
here — they are returned as ``action_required`` payloads for the client
to show to the user and re-submit with a confirmation.
"""

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer

from miriam_agent.agents.agent_loop import Agent
from miriam_agent.agents.base import AgentConfig
from miriam_agent.api.dependencies import (
    get_audit_system,
    get_bearer_token,
    get_current_user,
    get_memory_store,
    get_supermemory_memory_dep,
)
from miriam_agent.database.memory import MemoryStore
from miriam_agent.database.models import User
from miriam_agent.integrations.supermemory_client import container_tag_for
from miriam_agent.onboarding.service import OnboardingService, OnboardingTurn
from miriam_agent.safety.policy import SafetyPolicy
from miriam_agent.safety.validator import InputValidator
from miriam_agent.tools import build_tool_registry

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBearer()

_audit_observer_installed = False
_validator = InputValidator()

# Mutation tools whose amount/recipient are worth recording in the audit
# trail. Kept as an explicit set (rather than checking `_is_mutation` on the
# result) so it's obvious at a glance which tools' arguments end up in a
# durable log -- read-only tool arguments are never captured.
_MONEY_TOOLS = {
    "send_money",
    "transfer_stash_to_spending",
    "transfer_spending_to_stash",
    "pay_bill",
    "create_strategy",
    "update_strategy",
    "enroll_strategy",
    "pause_strategy",
    "resume_strategy",
    "rebalance_strategy",
    "buy_asset",
    "sell_asset",
    "set_allocation",
}


def _money_details(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Pull the amount and counterparty out of a mutation tool's arguments.

    Previously the audit observer never looked at tool arguments at all, so
    ``AuditLog.details`` for a money movement recorded that *something* ran
    but never *what* -- making amount-based safety checks (daily limits,
    large-transfer patterns) impossible to implement against real data.
    """
    if tool_name not in _MONEY_TOOLS:
        return {}
    details: dict[str, Any] = {}
    if "amount" in args:
        details["amount"] = args.get("amount")
    elif "amount_ngn" in args:
        details["amount"] = args.get("amount_ngn")
    elif "amount_usd" in args:
        details["amount"] = args.get("amount_usd")
    if "to" in args:
        details["recipient"] = args.get("to")
    elif "recipient" in args:
        details["recipient"] = args.get("recipient")
    if "symbol" in args:
        details["symbol"] = args.get("symbol")
    if "asset_id" in args:
        details["asset_id"] = args.get("asset_id")
    if "strategy_id" in args:
        details["strategy_id"] = args.get("strategy_id")
    if "category" in args:
        details["category"] = args.get("category")
    return details


def _install_audit_observer() -> None:
    """Register an audit observer on the shared tool registry (once)."""
    global _audit_observer_installed
    if _audit_observer_installed:
        return

    import asyncio

    async def _audit_async(tool_name: str, result: dict[str, Any]) -> None:
        audit = await get_audit_system().__anext__()
        if audit is None:
            return
        user_id = result.get("_context", {}).get("user_id")
        if not user_id:
            user_id = result.get("result", {}).get("user_id")
        try:
            await audit.log_action(
                user_id=str(user_id or "unknown"),
                action=tool_name,
                resource="tool",
                details={
                    "status": result.get("status"),
                    "elapsed": result.get("elapsed"),
                    "error": result.get("error"),
                    **_money_details(tool_name, result.get("_args") or {}),
                },
                risk_level=result.get("result", {}).get("_risk_level"),
            )
        except Exception:
            logger.warning("Audit log failed (non-blocking): %s", tool_name)

    def _observe(tool_name: str, result: dict[str, Any]) -> None:
        try:
            task = asyncio.create_task(_audit_async(tool_name, result))
            task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )
        except Exception:
            pass  # no running loop / audit unavailable

    from miriam_agent.agents.tools import get_registry

    get_registry().add_observer(_observe)
    _audit_observer_installed = True


def _serialize_agent_result(result: Any) -> dict[str, Any]:
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
        "messages": list(result.messages),
        "reaction": str(result.reaction or ""),
        "share": None,
    }


@router.post("/chat")
async def chat_with_agent(
    request: dict[str, Any],
    user: User = Depends(get_current_user),
    token: str = Depends(get_bearer_token),
    memory_store: MemoryStore = Depends(get_memory_store),
    supermemory_memory: Any = Depends(get_supermemory_memory_dep),
) -> dict[str, Any]:
    """Chat with the financial agent (non-streaming)."""
    message = request.get("message", "")
    conversation_id = request.get("conversation_id")
    approved_actions = request.get("approved_actions")

    if not message:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message cannot be empty",
        )

    if not await _validator.validate_rate_limit(user.id, "chat"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please try again shortly.",
        )

    is_valid, validation_errors = await _validator.validate_user_input(
        {"message": message}, "chat"
    )
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation errors: {', '.join(validation_errors)}",
        )

    # Ensure a local user row exists (Go backend is the identity authority).
    await memory_store.ensure_user(user)

    # Conversational onboarding: while a user has an unfinished financial
    # interview, Miriam's onboarding flow owns the turn (polls + plan + consent)
    # instead of the general agent. Action intents and completed interviews pass
    # straight through. An OTP-confirmed action replay is the one case that
    # must never be handed to onboarding: the user already reviewed and proved
    # the action by email code, so it goes straight to the agent for execution.
    if not approved_actions:
        try:
            onboarding = await OnboardingService(memory_store).handle_turn(
                user,
                message=message,
                is_poll_vote=bool(request.get("is_poll_vote", False)),
                poll_title=request.get("poll_title") or "",
                document=request.get("document"),
            )
        except Exception:
            # Fail-open: onboarding must never 500 a message. If it crashes we
            # hand the turn to the general agent rather than lose the user's
            # message to a broken flow.
            logger.exception("onboarding handle_turn failed for %s", user.id)
            onboarding = OnboardingTurn(conversation_id=f"onboarding:{user.id}")
        if onboarding.took_over:
            return await _finish_onboarding_turn(
                memory_store, supermemory_memory, user, message, onboarding
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
    _install_audit_observer()

    # Load user context (from Go backend when reachable; local memory otherwise)
    user_context = await _load_user_context(memory_store, user)
    history = (
        await memory_store.get_conversation_history(conversation_id)
        if conversation_id
        else []
    )
    memory_facts = await _load_memory_facts(
        memory_store, user.id, query=message, supermemory_memory=supermemory_memory
    )
    financial_plan = await _load_financial_plan(token)

    try:
        result = await agent.run(
            user_id=user.id,
            token=token,
            message=message,
            conversation_id=conversation_id,
            history=history,
            user_context=user_context,
            memory_facts=memory_facts,
            financial_plan=financial_plan,
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
    request: dict[str, Any],
    user: User = Depends(get_current_user),
    token: str = Depends(get_bearer_token),
    memory_store: MemoryStore = Depends(get_memory_store),
    supermemory_memory: Any = Depends(get_supermemory_memory_dep),
) -> StreamingResponse:
    """Stream chat tokens via Server-Sent Events."""
    message = request.get("message", "")
    conversation_id = request.get("conversation_id")
    approved_actions = request.get("approved_actions")
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    if not await _validator.validate_rate_limit(user.id, "chat"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please try again shortly.",
        )
    is_valid, validation_errors = await _validator.validate_user_input(
        {"message": message}, "chat"
    )
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation errors: {', '.join(validation_errors)}",
        )

    await memory_store.ensure_user(user)

    # Conversational onboarding owns the turn here too, exactly as in /chat
    # (polls only render in the iMessage bridge; the web stream carries the text
    # and, when one is pending, the poll payload for the client to render).
    # OTP-confirmed action replays skip onboarding just like in /chat:
    # approved_actions were already reviewed and email-verified by the user.
    onboarding = None
    if not approved_actions:
        try:
            onboarding = await OnboardingService(memory_store).handle_turn(
                user,
                message=message,
                is_poll_vote=bool(request.get("is_poll_vote", False)),
                poll_title=request.get("poll_title") or "",
                document=request.get("document"),
            )
        except Exception:
            # Fail-open, mirroring /chat: never 500 the stream over onboarding;
            # fall through to the general agent.
            logger.exception("onboarding handle_turn failed for %s", user.id)
            onboarding = OnboardingTurn(conversation_id=f"onboarding:{user.id}")

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            if onboarding is not None and onboarding.took_over:
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
                    conversation_id=conv_id,
                    user_message=message,
                    assistant_message=onboarding.response,
                )
                if onboarding.poll:
                    yield _sse(
                        {
                            "type": "onboarding",
                            "onboarding": onboarding.to_payload(conv_id).get(
                                "onboarding", {}
                            ),
                            "poll": onboarding.poll,
                        }
                    )
                yield _sse({"type": "token", "content": onboarding.response})
                yield _sse({"type": "done"})
                return

            registry = build_tool_registry()
            agent = Agent(
                registry=registry,
                safety_policy=SafetyPolicy(),
                config=AgentConfig(name="financial_agent", tools=registry.list_names()),
            )
            _install_audit_observer()
            history = (
                await memory_store.get_conversation_history(conversation_id)
                if conversation_id
                else []
            )
            user_context = await _load_user_context(memory_store, user)
            memory_facts = await _load_memory_facts(
                memory_store,
                user.id,
                query=message,
                supermemory_memory=supermemory_memory,
            )
            financial_plan = await _load_financial_plan(token)

            async for event in agent.stream_run(
                user_id=user.id,
                token=token,
                message=message,
                conversation_id=conversation_id,
                history=history,
                user_context=user_context,
                memory_facts=memory_facts,
                financial_plan=financial_plan,
                approved_actions=approved_actions,
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
                    yield _sse(
                        {"type": "confirmation_required", "message": event["message"]}
                    )
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
    request: dict[str, Any],
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


def _sse(payload: dict[str, Any]) -> str:
    """Format a dict as a single Server-Sent Events ``data:`` frame."""
    return f"data: {json.dumps(payload)}\n\n"


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
        conversation_id=conv_id,
        user_message=message,
        assistant_message=onboarding.response,
    )
    payload = onboarding.to_payload(conv_id)
    payload["conversation_history"] = await memory_store.get_conversation_history(
        conv_id
    )
    return payload


async def _load_financial_plan(token: str) -> dict[str, Any] | None:
    """Assemble a short plan note from live Go data (fail-open).

    Does not call the retired Go AI ``/api/v1/ai/financial-plan`` endpoint.
    """
    try:
        from miriam_agent.integrations.go_client import get_go_client

        client = get_go_client()
        plan = await client.get_financial_plan(token)
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
            facts = await supermemory_memory.build_memory_facts(
                container_tag, query=query or "What should I know about this user?"
            )
            if facts:
                return facts
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

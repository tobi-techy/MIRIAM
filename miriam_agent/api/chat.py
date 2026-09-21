"""Chat endpoints for Miriam Financial Agent.

Every turn is classified before anything else runs, and the classification
decides which of two entrypoints owns it:

* **a money turn** (money arriving, a send/split/lock/unlock, a track change,
  "can I afford", or a confirmation tap) goes to
  ``orchestrator.Orchestrator`` and nowhere else. That path is
  Hands -> Judgment -> Hands -> Voice, and it is the only thing in the process
  that can move money.
* **everything else** goes to the agent loop, which can only read.

There is no third path, and the agent loop cannot reach a rail: the money tools
are not in the registry it reads from.

A money turn's response is a fixed shape: ``confirm_id``, ``decision`` and
``receipt``, always. ``approved_actions`` and ``cards`` are not part of it and
are not read or sent, because the confirmation authority is a ``confirm_id``
that Hands issued.

Confirmations arrive as ``{confirm_id, yes|no}``. A typed "yes" is a message like
any other and settles nothing.
"""

import json
import logging
from collections.abc import AsyncGenerator
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer

from miriam_agent.agents.agent_loop import Agent, AgentRunResult
from miriam_agent.agents.base import AgentConfig
from miriam_agent.api.dependencies import (
    get_audit_system,
    get_bearer_token,
    get_current_user,
    get_memory_store,
    get_supermemory_memory_dep,
)
from miriam_agent.config.settings import get_settings
from miriam_agent.database.memory import MemoryStore
from miriam_agent.database.models import User
from miriam_agent.hands.ledger import LedgerUnavailable, RedisLedgerStore, money
from miriam_agent.hands.limits import Policy
from miriam_agent.hands.state import InsufficientState
from miriam_agent.hands.transfer import GoRail, parse_amount
from miriam_agent.integrations.supermemory_client import container_tag_for
from miriam_agent.judgment.gates import build_ingress_state, ingress_gate
from miriam_agent.observability.correlation import current_trace_id
from miriam_agent.onboarding.service import OnboardingService, OnboardingTurn
from miriam_agent.orchestrator import (
    Orchestrator,
    TurnResult,
    classify_turn,
    inflow_id_for_alert,
    looks_like_inflow_alert,
)
from miriam_agent.safety.policy import SafetyPolicy
from miriam_agent.safety.validator import InputValidator
from miriam_agent.tools import build_tool_registry

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBearer()

_audit_observer_installed = False
_validator = InputValidator()

# One ledger store per process: the in-process fallback it keeps for a Redis
# outage has to be the same object across requests, or a challenge staged in one
# request would be invisible to the tap that settles it.
_ledger_store: RedisLedgerStore | None = None


def _get_ledger_store() -> RedisLedgerStore:
    global _ledger_store
    if _ledger_store is None:
        _ledger_store = RedisLedgerStore(
            single_process=get_settings().MONEY_SINGLE_PROCESS
        )
    return _ledger_store


def _orchestrator_for(token: str) -> Orchestrator:
    """The money entrypoint for one request.

    The store is process-wide (see ``_get_ledger_store``); the rail is
    per-request because it carries the caller's token, and Go remains the
    authority for identity and for money.
    """
    return Orchestrator(
        store=_get_ledger_store(),
        policy=Policy.from_settings(),
        rail=GoRail(token),
    )


def _install_audit_observer() -> None:
    """Register an audit observer on the shared tool registry (once).

    Only read-only tools reach the registry now, so the row this writes records
    what the agent *looked at*. What moved is recorded by Hands, in its own
    audit rows, next to the decision that authorised it.
    """
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
                    # Explicit (rather than only contextvar-inherited) so the
                    # audit row is joinable to the turn even if this observer
                    # is ever invoked from a fresh task.
                    "trace_id": result.get("trace_id") or "",
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
        "messages": list(result.messages),
        "reaction": str(result.reaction or ""),
        "share": None,
        # Same id the inbound request carried (and the header echoes), so the
        # client can join this reply to the tool calls and audit rows behind it.
        "trace_id": getattr(result, "trace_id", "") or current_trace_id(),
    }


def _serialize_money_result(result: TurnResult) -> dict[str, Any]:
    """The client payload for a money turn.

    A fixed key set, which is the contract a client pins against and which
    ``tests/fixtures/money_layers/response_contract.json`` records:

    * ``confirm_id`` is always present. Non-empty means a challenge is open and
      the only thing a client may send back to settle anything is that id.
    * ``decision`` is always present, carrying the typed reasons the reply was
      written from, so a client can render the verdict without re-deriving it.
    * ``receipt`` is always present, and ``null`` when nothing moved. It is what
      actually happened, not what was asked for.

    ``requires_confirmation`` and ``cards`` are never present. The old approval
    card protocol is gone; a client that still looks for it is reading a
    contract this server no longer has.
    """
    return {
        "response": result.narration or "",
        "conversation_id": None,  # filled by the caller
        "trace_id": current_trace_id(),
        "confirm_id": result.confirm_id,
        "decision": result.decision,
        "receipt": (
            result.receipt.model_dump(mode="json")
            if result.receipt is not None
            else None
        ),
    }


async def _run_money_turn(
    *,
    user: User,
    token: str,
    message: str,
    confirm_id: str,
    yes: bool,
) -> TurnResult:
    """Run one money turn through the orchestrator.

    A confirmation tap is settled by id and nothing else. Everything else is an
    utterance, except an inflow alert pasted into chat, which is split against a
    stable id derived from the alert so a re-paste cannot split twice.
    """
    orchestrator = _orchestrator_for(token)
    if confirm_id:
        return await orchestrator.handle_confirm(user.id, confirm_id, yes)
    if looks_like_inflow_alert(message):
        return await orchestrator.handle_inflow(
            user.id,
            payment_id=inflow_id_for_alert(message),
            amount=_alert_amount(message),
            source_raw=message,
        )
    return await orchestrator.handle_utterance(user.id, message)


def _alert_amount(message: str) -> Decimal:
    """The amount in an inflow alert, in the ledger's own Decimal type."""
    parsed = parse_amount(message)
    return money(parsed) if parsed is not None else money(0)


async def _finalize_money_turn(
    *,
    memory_store: MemoryStore,
    supermemory_memory: Any,
    user: User,
    message: str,
    result: TurnResult,
    conversation_id: str | None,
) -> dict[str, Any]:
    """Persist and serialize a finished money turn."""
    conv_id = conversation_id or f"conv_{user.id}"
    payload = _serialize_money_result(result)
    payload["conversation_id"] = conv_id

    await memory_store.store_interaction(
        user_id=user.id,
        role="user",
        content=message,
        conversation_id=conv_id,
        metadata={"channel": "api", "money_layer": True},
    )
    await memory_store.store_interaction(
        user_id=user.id,
        role="assistant",
        content=payload["response"],
        conversation_id=conv_id,
        metadata={
            "channel": "api",
            "money_layer": True,
            "confirm_id": result.confirm_id,
        },
    )
    await _ingest_to_supermemory(
        supermemory_memory,
        user.id,
        conversation_id=conv_id,
        user_message=message,
        assistant_message=payload["response"],
    )
    payload["conversation_history"] = await memory_store.get_conversation_history(
        conv_id, user.id
    )
    return payload


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
    # A confirmation tap. Only the id settles anything; `yes` decides whether it
    # is a confirmation or a decline. Anything else is not a confirmation, so a
    # missing `yes` declines rather than moves money.
    confirm_id = str(request.get("confirm_id") or "").strip()
    confirmed = bool(request.get("yes", False))

    # A confirmation tap carries no message, so an id is an acceptable turn on
    # its own. Everything else is a message.
    if not message and not confirm_id:
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

    # A conversation id supplied by the client is untrusted: it must belong to
    # the authenticated user. Previously any id was accepted, so presenting
    # another user's conversation returned their messages (and appended this
    # user's turns into their conversation).
    conversation_id = await _require_owned_conversation(
        memory_store, user, conversation_id
    )

    # The turn is classified before anything else runs, and a money turn goes
    # to the orchestrator. That is the only path in the process that can move
    # money, and it is deliberately reached before onboarding: "send 200k to
    # Femi" is an instruction, not an interview answer.
    if get_settings().MONEY_LAYERS_ENABLED and (
        confirm_id or classify_turn(message) == "orchestrator"
    ):
        money_result = await _run_money_turn(
            user=user,
            token=token,
            message=message,
            confirm_id=confirm_id,
            yes=confirmed,
        )
        return await _finalize_money_turn(
            memory_store=memory_store,
            supermemory_memory=supermemory_memory,
            user=user,
            message=message,
            result=money_result,
            conversation_id=conversation_id,
        )

    # Conversational onboarding: while a user has an unfinished financial
    # interview, Miriam's onboarding flow owns the turn (polls + plan + consent)
    # instead of the general agent. Action intents and completed interviews pass
    # straight through.
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
        await memory_store.get_conversation_history(conversation_id, user.id)
        if conversation_id
        else []
    )
    memory_facts = await _load_memory_facts(
        memory_store, user.id, query=message, supermemory_memory=supermemory_memory
    )
    financial_plan = await _load_financial_plan(token)

    # TypeSafe ingress gate: classify the turn and short-circuit jailbreaks,
    # PII pastes, and vague asks before the generator ever sees them.
    try:
        ingress_decision = await ingress_gate(
            build_ingress_state(
                user_id=user.id,
                message=message,
                history=history,
                user_context=user_context,
                registry=registry,
            )
        )
    except Exception:
        # A judgment-layer bug must never 500 a chat turn; fail open to the
        # generator. Network errors are already handled (fail-closed) inside
        # ingress_gate, so this only catches unexpected code paths.
        logger.exception("ingress gate failed; failing open to generator")
        ingress_decision = None
    if ingress_decision is not None and ingress_decision.short_circuits:
        return await _finalize_turn(
            memory_store=memory_store,
            supermemory_memory=supermemory_memory,
            user=user,
            message=message,
            result=AgentRunResult(
                response=ingress_decision.reply or "",
                conversation_id=conversation_id or f"conv_{user.id}",
            ),
        )

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
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing chat request: {str(e)}",
        )

    return await _finalize_turn(
        memory_store=memory_store,
        supermemory_memory=supermemory_memory,
        user=user,
        message=message,
        result=result,
    )


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
    confirm_id = str(request.get("confirm_id") or "").strip()
    confirmed = bool(request.get("yes", False))
    if not message and not confirm_id:
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

    # Same untrusted-id rule as /chat: refuse a conversation that isn't the
    # caller's before a single token is streamed.
    conversation_id = await _require_owned_conversation(
        memory_store, user, conversation_id
    )

    # A money turn streams its one reply instead of tokens. It never reaches the
    # agent loop, and it is not offered to onboarding.
    is_money_turn = get_settings().MONEY_LAYERS_ENABLED and (
        bool(confirm_id) or classify_turn(message) == "orchestrator"
    )

    # Conversational onboarding owns the turn here too, exactly as in /chat
    # (polls only render in the iMessage bridge; the web stream carries the text
    # and, when one is pending, the poll payload for the client to render).
    onboarding = None
    if not is_money_turn:
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
            if is_money_turn:
                money_result = await _run_money_turn(
                    user=user,
                    token=token,
                    message=message,
                    confirm_id=confirm_id,
                    yes=confirmed,
                )
                payload = await _finalize_money_turn(
                    memory_store=memory_store,
                    supermemory_memory=supermemory_memory,
                    user=user,
                    message=message,
                    result=money_result,
                    conversation_id=conversation_id,
                )
                yield _sse(
                    {
                        "type": "money_layer",
                        "confirm_id": payload["confirm_id"],
                        "decision": payload["decision"],
                        "receipt": payload["receipt"],
                    }
                )
                yield _sse({"type": "token", "content": payload["response"]})
                yield _sse({"type": "done"})
                return
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
                await memory_store.get_conversation_history(conversation_id, user.id)
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

            # TypeSafe ingress gate, mirroring /chat. A short-circuit streams the
            # fixed reply instead of running the generator, so a jailbreak/PII/
            # vague ask never reaches it here either.
            try:
                ingress_decision = await ingress_gate(
                    build_ingress_state(
                        user_id=user.id,
                        message=message,
                        history=history,
                        user_context=user_context,
                        registry=registry,
                    )
                )
            except Exception:
                logger.exception("ingress gate failed; failing open to generator")
                ingress_decision = None
            if ingress_decision is not None and ingress_decision.short_circuits:
                conv_id = conversation_id or f"conv_{user.id}"
                reply = ingress_decision.reply or ""
                await memory_store.store_interaction(
                    user_id=user.id,
                    role="user",
                    content=message,
                    conversation_id=conv_id,
                    metadata={"channel": "api"},
                )
                await memory_store.store_interaction(
                    user_id=user.id,
                    role="assistant",
                    content=reply,
                    conversation_id=conv_id,
                    metadata={"channel": "api"},
                )
                await _ingest_to_supermemory(
                    supermemory_memory,
                    user.id,
                    conversation_id=conv_id,
                    user_message=message,
                    assistant_message=reply,
                )
                yield _sse({"type": "token", "content": reply})
                yield _sse({"type": "done"})
                return

            async for event in agent.stream_run(
                user_id=user.id,
                token=token,
                message=message,
                conversation_id=conversation_id,
                history=history,
                user_context=user_context,
                memory_facts=memory_facts,
                financial_plan=financial_plan,
            ):
                evt = event["type"]
                if evt == "token":
                    yield _sse({"type": "token", "content": event["content"]})
                elif evt == "tool_call":
                    yield _sse({"type": "tool_call", "tool_call": event["tool_call"]})
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


@router.post("/money/inflow")
async def money_inflow(
    request: dict[str, Any],
    user: User = Depends(get_current_user),
    token: str = Depends(get_bearer_token),
) -> dict[str, Any]:
    """Tell the ledger that money arrived. Called by the rail, not by chat.

    This is the only inflow path. A payment is a fact the rail knows and a
    sentence is not, so money never enters the ledger because someone described
    it in a message: the classifier routes a *chat* alert here, this endpoint
    and the rail webhook are the same code, and ``payment_id`` is the
    idempotency key either way.

    The reply carries the receipt Hands wrote, so the caller can see the split
    rather than infer it.
    """
    payment_id = str(request.get("payment_id") or "").strip()
    amount = request.get("amount")
    source_raw = str(request.get("source_raw") or "")
    if not payment_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="payment_id is required: it is the idempotency key for the split",
        )
    if amount is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="amount is required",
        )
    # A payload that cannot be parsed is the caller's error, not ours. Without
    # this it reached ``money`` inside the try below, raised InvalidOperation and
    # came back as a 500 — which tells the rail the server failed and invites a
    # retry of a payload that can never succeed.
    try:
        parsed_amount = money(amount)
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="amount must be a number",
        )
    # A credit of nothing, or of a negative amount, is not a credit: it still
    # wrote an executed receipt and skewed the 30-day inflow figure.
    if not parsed_amount.is_finite() or parsed_amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="amount must be a positive number",
        )

    orchestrator = _orchestrator_for(token)
    try:
        result = await orchestrator.handle_inflow(
            user.id,
            payment_id=payment_id,
            amount=parsed_amount,
            source_raw=source_raw,
        )
    except InsufficientState as exc:
        # The ledger cannot answer for this user yet. Refuse rather than guess.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"insufficient_state: {', '.join(exc.missing)}",
        )
    except LedgerUnavailable as exc:
        # The rail must retry: money arriving is a fact it can deliver again,
        # and a 200 here would tell it the payment had been recorded.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error handling inflow: {str(e)}",
        )

    return {
        "payment_id": payment_id,
        "response": result.narration or "",
        "decision": result.decision,
        "receipt": result.receipt.model_dump(mode="json") if result.receipt else None,
        "trace_id": current_trace_id(),
    }


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
    if await memory_store.get_owned_conversation(conversation_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await memory_store.get_conversation_messages(conversation_id, user.id)
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


async def _require_owned_conversation(
    memory_store: MemoryStore, user: User, conversation_id: Any
) -> str | None:
    """Validate a client-supplied conversation id against its owner.

    Returns the id when it is usable by this user, ``None`` when no id was
    supplied, and raises 404 when the conversation exists and belongs to
    someone else.

    A conversation that does not exist yet is allowed through: the write path
    creates it for the caller on first use, which is the behaviour clients
    rely on when they start a session with their own id. The check is about
    *ownership*, not existence. Every read and write on the chat path goes
    through this: without it, presenting another user's conversation id
    returned their messages and appended this user's turns into their
    conversation.
    """
    if not conversation_id:
        return None
    candidate = str(conversation_id)
    existing = await memory_store.get_conversation(candidate)
    if existing is not None and existing.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    return candidate


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
        conversation_id=conv_id,
        user_message=message,
        assistant_message=onboarding.response,
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
        conversation_id=result.conversation_id,
        user_message=message,
        assistant_message=result.response,
    )
    payload = _serialize_agent_result(result)
    payload["conversation_history"] = await memory_store.get_conversation_history(
        result.conversation_id, user.id
    )
    return payload

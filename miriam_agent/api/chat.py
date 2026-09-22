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
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel

from miriam_agent.agents.agent_loop import Agent, AgentRunResult
from miriam_agent.agents.base import AgentConfig
from miriam_agent.api.dependencies import (
    get_audit_system,
    get_bearer_token,
    get_current_user,
    get_memory_store,
    get_supermemory_memory_dep,
    require_rail_service_key,
)
from miriam_agent.config.settings import get_settings
from miriam_agent.database.memory import MemoryStore
from miriam_agent.database.models import User
from miriam_agent.hands.ledger import (
    LedgerConflictError,
    LedgerUnavailable,
    RedisLedgerStore,
    money,
)
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


async def _persist_money_audit(event: Any, result: Any) -> None:
    """Write a finished turn's receipt to the Postgres audit store.

    Injected into the Orchestrator as its audit sink. The receipt is the
    compliance fact — what was attempted, moved, or refused — and until now
    it lived only in the rewritable Redis ledger blob. Fail-open: the
    orchestrator catches sink errors, so this raising never breaks a turn.
    """
    audit = await get_audit_system().__anext__()
    if audit is None:
        return
    receipt = result.receipt
    await audit.log_money_movement(
        user_id=event.user_id,
        transaction_id=receipt.id,
        amount=float(receipt.amount or 0),
        currency=receipt.currency,
        action=receipt.action,
        status=receipt.status,
        from_account=receipt.sleeve,
        to_account=receipt.counterparty,
        requires_approval=False,
        approval_id=result.confirm_id or None,
    )


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
        go_token=token,
        audit_sink=_persist_money_audit,
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

    Two turns for the same user race on the ledger's compare-and-set; the loser
    gets a typed conflict mapped to a retryable 409, not a 500 that tells the
    client the server is broken.
    """
    orchestrator = _orchestrator_for(token)
    try:
        if confirm_id:
            return await orchestrator.handle_confirm(user.id, confirm_id, yes)
        if get_settings().ALLOW_CHAT_INFLOW_SYNTH and looks_like_inflow_alert(
            message
        ):
            # Demo-only escape hatch (ALLOW_CHAT_INFLOW_SYNTH): split against a
            # stable id derived from the alert so a re-paste cannot split twice.
            # Off by default and refused in production entirely — chat text is
            # not a payment fact, so it must not mint ledger money.
            return await orchestrator.handle_inflow(
                user.id,
                payment_id=inflow_id_for_alert(message),
                amount=_alert_amount(message),
                source_raw=message,
            )
        return await orchestrator.handle_utterance(user.id, message)
    except LedgerConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that ledger is busy; try again in a moment",
        ) from exc


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


class ChatRequest(BaseModel):
    """Body for ``/chat`` and ``/chat/stream``.

    Replaces the hand-parsed ``dict[str, Any]`` the endpoints used to take:
    types are enforced at the boundary now, so ``"yes": "no"`` is False
    instead of truthy, a non-bool cannot slip through as a confirmation, and
    unknown fields are ignored rather than silently meaning nothing.
    """

    message: str = ""
    conversation_id: str | None = None
    confirm_id: str = ""
    # Only the id settles anything; ``yes`` decides confirmation vs decline.
    # A missing ``yes`` declines rather than moves money.
    yes: bool = False
    is_poll_vote: bool = False
    poll_title: str = ""
    document: dict[str, Any] | None = None


@dataclass
class _PreparedTurn:
    """The request after validation, with a decision on who owns the turn."""

    message: str
    conversation_id: str | None
    confirm_id: str
    yes: bool
    is_money_turn: bool
    onboarding: OnboardingTurn | None


async def _prepare_turn(
    *, body: ChatRequest, memory_store: MemoryStore, user: User
) -> _PreparedTurn:
    """Validate one turn and decide who handles it.

    Shared by /chat and /chat/stream: the two endpoints used to keep two
    hand-maintained copies of this pipeline and the copies had already
    drifted. One pipeline, two renderers.
    """
    message = body.message
    confirm_id = body.confirm_id.strip()

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
        memory_store, user, body.conversation_id
    )

    # The turn is classified before anything else runs, and a money turn goes
    # to the orchestrator, which is the only path in the process that can move
    # money.
    is_money_turn = get_settings().MONEY_LAYERS_ENABLED and (
        bool(confirm_id) or classify_turn(message) == "orchestrator"
    )

    # Conversational onboarding owns the turn when an interview is unfinished
    # (polls + plan + consent). Fail-open, as everywhere: onboarding must
    # never lose the user's message to a broken flow.
    onboarding: OnboardingTurn | None = None
    if not is_money_turn:
        try:
            onboarding = await OnboardingService(memory_store).handle_turn(
                user,
                message=message,
                is_poll_vote=body.is_poll_vote,
                poll_title=body.poll_title,
                document=body.document,
            )
        except Exception:
            logger.exception("onboarding handle_turn failed for %s", user.id)
            onboarding = OnboardingTurn(conversation_id=f"onboarding:{user.id}")

    return _PreparedTurn(
        message=message,
        conversation_id=conversation_id,
        confirm_id=confirm_id,
        yes=body.yes,
        is_money_turn=is_money_turn,
        onboarding=onboarding,
    )


def _build_agent(registry: Any) -> Agent:
    """The general agent, built the same way on every path."""
    return Agent(
        registry=registry,
        safety_policy=SafetyPolicy(),
        config=AgentConfig(
            name="financial_agent",
            tools=registry.list_names(),
            system_prompt="Miriam Financial Agent",
        ),
    )


async def _agent_inputs(
    *,
    memory_store: MemoryStore,
    supermemory_memory: Any,
    user: User,
    token: str,
    message: str,
    conversation_id: str | None,
    registry: Any,
) -> dict[str, Any]:
    """The context bundle both agent paths run with (history, facts, plan)."""
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
    return {
        "history": history,
        "user_context": user_context,
        "memory_facts": memory_facts,
        "financial_plan": financial_plan,
    }


async def _ingress_decision(
    *,
    user_id: str,
    message: str,
    registry: Any,
    history: list,
    user_context: Any,
    **_: Any,
) -> Any:
    """TypeSafe ingress gate, failing open.

    A judgment-layer bug must never 500 a chat turn; network errors are
    already handled (fail-closed) inside ingress_gate, so this only catches
    unexpected code paths.
    """
    try:
        return await ingress_gate(
            build_ingress_state(
                user_id=user_id,
                message=message,
                history=history,
                user_context=user_context,
                registry=registry,
            )
        )
    except Exception:
        logger.exception("ingress gate failed; failing open to generator")
        return None


@router.post("/chat")
async def chat_with_agent(
    body: ChatRequest,
    user: User = Depends(get_current_user),
    token: str = Depends(get_bearer_token),
    memory_store: MemoryStore = Depends(get_memory_store),
    supermemory_memory: Any = Depends(get_supermemory_memory_dep),
) -> dict[str, Any]:
    """Chat with the financial agent (non-streaming)."""
    turn = await _prepare_turn(body=body, memory_store=memory_store, user=user)

    # A money turn goes to the orchestrator. That is the only path in the
    # process that can move money, and it is deliberately reached before
    # onboarding: "send 200k to Femi" is an instruction, not an interview
    # answer.
    if turn.is_money_turn:
        money_result = await _run_money_turn(
            user=user,
            token=token,
            message=turn.message,
            confirm_id=turn.confirm_id,
            yes=turn.yes,
        )
        return await _finalize_money_turn(
            memory_store=memory_store,
            supermemory_memory=supermemory_memory,
            user=user,
            message=turn.message,
            result=money_result,
            conversation_id=turn.conversation_id,
        )

    # Conversational onboarding: while a user has an unfinished financial
    # interview, Miriam's onboarding flow owns the turn (polls + plan + consent)
    # instead of the general agent. Action intents and completed interviews pass
    # straight through.
    if turn.onboarding is not None and turn.onboarding.took_over:
        return await _finish_onboarding_turn(
            memory_store, supermemory_memory, user, turn.message, turn.onboarding
        )

    registry = build_tool_registry()
    agent = _build_agent(registry)
    _install_audit_observer()

    inputs = await _agent_inputs(
        memory_store=memory_store,
        supermemory_memory=supermemory_memory,
        user=user,
        token=token,
        message=turn.message,
        conversation_id=turn.conversation_id,
        registry=registry,
    )

    # TypeSafe ingress gate: classify the turn and short-circuit jailbreaks,
    # PII pastes, and vague asks before the generator ever sees them.
    ingress_decision = await _ingress_decision(
        user_id=user.id,
        message=turn.message,
        registry=registry,
        **inputs,
    )
    if ingress_decision is not None and ingress_decision.short_circuits:
        return await _finalize_turn(
            memory_store=memory_store,
            supermemory_memory=supermemory_memory,
            user=user,
            message=turn.message,
            result=AgentRunResult(
                response=ingress_decision.reply or "",
                conversation_id=turn.conversation_id or f"conv_{user.id}",
            ),
        )

    try:
        result = await agent.run(
            user_id=user.id,
            token=token,
            message=turn.message,
            conversation_id=turn.conversation_id,
            **inputs,
        )
    except HTTPException:
        raise
    except Exception:
        # Internal error text can carry stack fragments, SQL, or provider
        # details; the client gets an opaque message and the trace id that
        # links it to the server log.
        logger.exception(
            "chat request failed", extra={"trace_id": current_trace_id()}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="something went wrong processing that message",
        )

    return await _finalize_turn(
        memory_store=memory_store,
        supermemory_memory=supermemory_memory,
        user=user,
        message=turn.message,
        result=result,
    )


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    user: User = Depends(get_current_user),
    token: str = Depends(get_bearer_token),
    memory_store: MemoryStore = Depends(get_memory_store),
    supermemory_memory: Any = Depends(get_supermemory_memory_dep),
) -> StreamingResponse:
    """Stream chat tokens via Server-Sent Events.

    Validation, ownership, classification and onboarding run through the same
    ``_prepare_turn`` pipeline as /chat — a refused turn is an HTTP 4xx before
    the stream opens, never a mystery error frame mid-stream.
    """
    turn = await _prepare_turn(body=body, memory_store=memory_store, user=user)

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            # A money turn streams its one reply instead of tokens. It never
            # reaches the agent loop, and it is not offered to onboarding.
            if turn.is_money_turn:
                money_result = await _run_money_turn(
                    user=user,
                    token=token,
                    message=turn.message,
                    confirm_id=turn.confirm_id,
                    yes=turn.yes,
                )
                payload = await _finalize_money_turn(
                    memory_store=memory_store,
                    supermemory_memory=supermemory_memory,
                    user=user,
                    message=turn.message,
                    result=money_result,
                    conversation_id=turn.conversation_id,
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
            if turn.onboarding is not None and turn.onboarding.took_over:
                conv_id = turn.onboarding.conversation_id or f"onboarding:{user.id}"
                await memory_store.store_interaction(
                    user_id=user.id,
                    role="user",
                    content=turn.message,
                    conversation_id=conv_id,
                    metadata={"channel": "api", "onboarding": True},
                )
                await memory_store.store_interaction(
                    user_id=user.id,
                    role="assistant",
                    content=turn.onboarding.response,
                    conversation_id=conv_id,
                    metadata={"channel": "api", "onboarding": True},
                )
                await _ingest_to_supermemory(
                    supermemory_memory,
                    user.id,
                    conversation_id=conv_id,
                    user_message=turn.message,
                    assistant_message=turn.onboarding.response,
                )
                if turn.onboarding.poll:
                    yield _sse(
                        {
                            "type": "onboarding",
                            "onboarding": turn.onboarding.to_payload(conv_id).get(
                                "onboarding", {}
                            ),
                            "poll": turn.onboarding.poll,
                        }
                    )
                yield _sse({"type": "token", "content": turn.onboarding.response})
                yield _sse({"type": "done"})
                return

            registry = build_tool_registry()
            agent = _build_agent(registry)
            _install_audit_observer()
            inputs = await _agent_inputs(
                memory_store=memory_store,
                supermemory_memory=supermemory_memory,
                user=user,
                token=token,
                message=turn.message,
                conversation_id=turn.conversation_id,
                registry=registry,
            )

            # TypeSafe ingress gate, mirroring /chat. A short-circuit streams the
            # fixed reply instead of running the generator, so a jailbreak/PII/
            # vague ask never reaches it here either.
            ingress_decision = await _ingress_decision(
                user_id=user.id,
                message=turn.message,
                registry=registry,
                **inputs,
            )
            if ingress_decision is not None and ingress_decision.short_circuits:
                # Persisted through the same finalize as /chat, so a refused
                # turn is recorded exactly like any other exchange.
                payload = await _finalize_turn(
                    memory_store=memory_store,
                    supermemory_memory=supermemory_memory,
                    user=user,
                    message=turn.message,
                    result=AgentRunResult(
                        response=ingress_decision.reply or "",
                        conversation_id=turn.conversation_id or f"conv_{user.id}",
                    ),
                )
                yield _sse({"type": "token", "content": payload["response"]})
                yield _sse({"type": "done"})
                return

            async for event in agent.stream_run(
                user_id=user.id,
                token=token,
                message=turn.message,
                conversation_id=turn.conversation_id,
                **inputs,
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
        except HTTPException as e:
            # Typed refusals (e.g. the 409 for a lost ledger race) carry a
            # message the client can act on; anything else stays opaque.
            logger.exception(
                "chat stream failed", extra={"trace_id": current_trace_id()}
            )
            yield _sse({"type": "error", "message": str(e.detail)})
        except Exception:
            logger.exception(
                "chat stream failed", extra={"trace_id": current_trace_id()}
            )
            yield _sse({"type": "error", "message": "internal error; try again"})

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
    _rail: None = Depends(require_rail_service_key),
) -> dict[str, Any]:
    """Tell the ledger that money arrived. Called by the rail, not by chat.

    This is the only inflow path. A payment is a fact the rail knows and a
    sentence is not, so money never enters the ledger because someone described
    it in a message: the classifier routes a *chat* alert here, this endpoint
    and the rail webhook are the same code, and ``payment_id`` is the
    idempotency key either way.

    Auth is two-factor by design: the bearer JWT names the account to credit,
    and the ``X-Rail-Service-Key`` header proves the caller is the rail. A
    user token alone is refused — this endpoint mints ledger money, so it is
    never callable with ordinary user credentials.
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
    except LedgerConflictError:
        # Two deliveries for the same user raced on the compare-and-set. The
        # rail retries; it must not read the 500 that says "server broken".
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ledger busy; retry the delivery",
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
    except Exception:
        # Same rule as /chat: no internals to the caller. The rail retries on
        # 5xx, so the message must not pretend to be a payload diagnosis.
        logger.exception(
            "inflow handling failed", extra={"trace_id": current_trace_id()}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error handling inflow",
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

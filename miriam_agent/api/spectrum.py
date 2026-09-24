"""Spectrum gateway contract: ``POST /api/v1/chat/spectrum``.

The Photon Spectrum gateway (terminal today, iMessage next) speaks this
endpoint, not ``/chat``. One JSON shape in, structured parts out:

    {"channel": "terminal", "space_id": "...", "user_id": "...", "text": "..."}
    -> {"parts": [{"type": "text", ...}, {"type": "card", ...}, {"type": "chart", ...}]}

Routing, in order:

1. ``confirm <code>`` / ``no <code>`` text, or structured ``confirm_id``+``yes``,
   settles a Hands-issued challenge by id. A chat "yes" settles nothing.
2. A structured ``signed_tx``+``flow_id`` (or a text-carried signature when
   ``RAIL_ALLOW_DEV_SIGN`` is on and the channel is ``terminal``) settles the
   wallet half of an allocation.
3. Money turns go to the orchestrator (Hands -> Judgment -> Hands -> Voice),
   the only path that can move money.
4. Portfolio asks ("how am i looking") read live positions and answer with
   one sentence plus a donut. Anything else gets a short deterministic reply;
   the agent loop stays web-only this sprint.

Fail closed: a simulated backend, a missing sleeve, or empty positions never
produce a card or a chart. The card says Glider is not live instead.
"""

from __future__ import annotations

import logging
import os
import re
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from miriam_agent.api import chat as chatmod
from miriam_agent.api.dependencies import (
    get_bearer_token,
    get_current_user,
    get_memory_store,
)
from miriam_agent.charts import png_base64, sleeve_donut, split_bar
from miriam_agent.charts.cards import allocate_jpg, jpg_base64
from miriam_agent.config.settings import get_settings
from miriam_agent.database.memory import MemoryStore
from miriam_agent.database.models import User
from miriam_agent.hands.limits import Policy
from miriam_agent.hands.transfer import GoRail
from miriam_agent.integrations import go_client as go_client_mod
from miriam_agent.observability.correlation import current_trace_id
from miriam_agent.orchestrator import Event, Orchestrator, TurnResult
from miriam_agent.safety.validator import InputValidator

logger = logging.getLogger(__name__)

router = APIRouter()

# The one channel that can carry a wallet signature in text is the one that
# used to validate least. Every turn now passes the same rate limit and input
# validation as /chat.
_validator = InputValidator()

CHANNELS = ("imessage", "whatsapp", "terminal")

_CONFIRM_RE = re.compile(r"^(?:confirm|yes)\s+(\S+)\s*$", re.IGNORECASE)
_DECLINE_RE = re.compile(r"^(?:no|decline|cancel)\s+(\S+)\s*$", re.IGNORECASE)


class SpectrumRequest(BaseModel):
    """Body for ``/chat/spectrum``.

    Typed at the boundary like ChatRequest: a signature, confirm id or flow id
    arrives as a string or not at all, and ``yes`` is a real tri-state (None =
    not answered) instead of whatever truthiness the raw JSON implied.
    """

    channel: str = "terminal"
    space_id: str = ""
    user_id: str = ""
    sender_id: str = ""
    text: str = ""
    confirm_id: str = ""
    yes: bool | None = None
    signed_tx: str = ""
    flow_id: str = ""
    wallet_address: str = ""


_B64_RE = re.compile(r"^[A-Za-z0-9+/=]{100,}$")

_PORTFOLIO_ASKS = (
    "how am i looking",
    "how am i doing",
    "show my portfolio",
    "show portfolio",
    "what did that buy do",
    "my portfolio",
    "my stocks",
    "how are my stocks",
    "how is my portfolio",
)


def _orchestrator_for(token: str) -> Orchestrator:
    settings = get_settings()
    channels = [
        part.strip().lower()
        for part in (settings.GO_CONFIRM_CARD_CHANNELS or "").split(",")
        if part.strip()
    ]
    return Orchestrator(
        store=chatmod._get_ledger_store(),
        policy=Policy.from_settings(),
        rail=GoRail(token),
        go_token=token,
        audit_sink=chatmod._persist_money_audit,
        cards_enabled=settings.GO_CONFIRM_CARDS_ENABLED,
        card_channels=tuple(channels) if channels else ("imessage",),
    )


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _card_part(card: dict[str, Any], confirm_id: str) -> dict[str, Any]:
    """Map an invest card onto the gateway allocate contract."""
    base = (os.getenv("SPECTRUM_AUTHORIZE_BASE") or "").strip().rstrip("/")
    part: dict[str, Any] = {
        "type": "card",
        "kind": card.get("kind", "allocate"),
        "title": card.get("title", "ALLOCATE"),
        "subtitle": card.get("subtitle", "Glider \u00b7 Rail Stock Sleeve"),
        "primary": card.get("primary", ""),
        "amount": card.get("amount", ""),
        "source": card.get("source", "stash"),
        "cta": card.get("cta", "Approve"),
        "flow_id": card.get("flow_id", ""),
        "confirm_id": confirm_id,
        "expires_in_sec": card.get("expires_in_sec", 1800),
    }
    if card.get("sign_payload"):
        part["sign_payload"] = card["sign_payload"]
    try:
        part["image_base64"] = jpg_base64(
            allocate_jpg(
                title=str(part["title"]),
                subtitle=str(part["subtitle"]),
                primary=str(part["primary"]),
                amount=str(part["amount"]),
                source=str(part["source"]),
                cta=str(part["cta"]),
            )
        )
    except Exception:  # noqa: BLE001 - a missing JPEG never blocks the card
        logger.warning("spectrum: card JPEG render failed (non-blocking)")
    if base and card.get("flow_id"):
        part["authorize_url"] = f"{base}/authorize?flow={card['flow_id']}"
    return part


def _chart_part(png: bytes, title: str = "Sleeve") -> dict[str, Any]:
    return {
        "type": "chart",
        "spec": {"kind": "donut", "title": title, "slices": []},
        "filename": "portfolio.png",
        "mime": "image/png",
        "image_base64": png_base64(png),
    }


async def _live_positions(token: str) -> list[dict[str, Any]]:
    try:
        data = await go_client_mod.get_go_client().get_investment_positions(token)
    except Exception as exc:  # noqa: BLE001 - unreadable positions mean no chart
        logger.warning("spectrum: positions unreadable: %s", exc)
        return []
    positions = data.get("positions") if isinstance(data, dict) else None
    return positions if isinstance(positions, list) else []


@router.post("/chat/spectrum")
async def spectrum_chat(
    body: SpectrumRequest,
    user: User = Depends(get_current_user),
    token: str = Depends(get_bearer_token),
    memory_store: MemoryStore = Depends(get_memory_store),
) -> dict[str, Any]:
    """One gateway turn in, rendered parts out."""
    if not await _validator.validate_rate_limit(user.id, "chat"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please try again shortly.",
        )
    is_valid, validation_errors = await _validator.validate_user_input(
        {"message": body.text}, "chat"
    )
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation errors: {', '.join(validation_errors)}",
        )

    channel = body.channel.strip().lower()
    if channel not in CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"channel must be one of {', '.join(CHANNELS)}",
        )
    space_id = body.space_id.strip()
    body_user = body.user_id.strip()
    text = body.text
    if body_user and body_user != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user_id does not match the bearer token",
        )
    if not text and not body.confirm_id and not body.signed_tx:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Text cannot be empty"
        )

    # Bind the channel handle to the stable JWT user on every turn
    # (first-seen auto-link). A handle owned by someone else never breaks
    # the turn — it is logged and the message still persists under the
    # bearer user; the verified merge endpoint owns the move.
    sender_id = (body.sender_id or "").strip()
    if sender_id:
        try:
            await memory_store.ensure_user(user)
            await memory_store.link_identity(user.id, channel, sender_id)
        except Exception:  # noqa: BLE001 - identity never breaks the turn
            logger.warning(
                "spectrum: identity link failed for %s:%s (non-blocking)",
                channel,
                sender_id,
            )

    orchestrator = _orchestrator_for(token)
    confirm_id_out = ""
    wallet_address = body.wallet_address.strip()
    parts: list[dict[str, Any]] = []
    conv_id = f"spectrum:{channel}:{space_id or user.id}"
    # The turn result when the money layers own it; None when the turn falls
    # through to the agent loop further down.
    result: TurnResult | None

    async def _remember(
        user_text: str, reply: str, extra: dict[str, Any] | None = None
    ) -> None:
        meta: dict[str, Any] = {
            "channel": channel,
            "space_id": space_id,
            "spectrum": True,
        }
        if sender_id:
            meta["sender_id"] = sender_id
        if extra:
            meta.update(extra)
        try:
            await memory_store.store_interaction(
                user_id=user.id,
                role="user",
                content=user_text,
                conversation_id=conv_id,
                metadata=meta,
            )
            await memory_store.store_interaction(
                user_id=user.id,
                role="assistant",
                content=reply,
                conversation_id=conv_id,
                metadata=meta,
            )
        except Exception:  # noqa: BLE001 - memory never breaks the turn
            logger.warning("spectrum: memory store failed (non-blocking)")

    # -- 1. structured or text confirmation tap ---------------------------
    confirm_id = body.confirm_id.strip()
    yes: bool | None = body.yes
    m = _CONFIRM_RE.match(text.strip())
    d = _DECLINE_RE.match(text.strip())
    if m:
        confirm_id, yes = m.group(1), True
    elif d:
        confirm_id, yes = d.group(1), False
    if confirm_id:
        if yes is False:
            result = await orchestrator.handle_confirm(user.id, confirm_id, False)
            parts.append(_text_part("Declined. Nothing moved."))
            await _remember(f"decline {confirm_id}", parts[0]["text"])
            return {
                "parts": parts,
                "confirm_id": confirm_id_out,
                "trace_id": current_trace_id(),
            }
        result = await orchestrator.handle(
            Event(
                type="confirm",
                user_id=user.id,
                confirm_id=confirm_id,
                wallet_address=wallet_address,
            )
        )
        # Invest taps need the wallet address at stage 1; the confirm path
        # ignores it for other actions, so threading it here is safe.
        if result.receipt is not None and result.receipt.status == "rejected":
            parts.append(
                _text_part(
                    result.narration
                    or "That confirmation does not match anything open."
                )
            )
        elif result.card is not None:
            amount = result.card.get("amount", "")
            parts.append(
                _text_part(
                    result.narration
                    or f"Putting {amount} into the Solana stock sleeve. "
                    "Confirm below."
                )
            )
            parts.append(_card_part(result.card, result.confirm_id))
        else:
            parts.append(_text_part(result.narration or "Done."))
        reply = " | ".join(
            p.get("text", p.get("title", "")) for p in parts if isinstance(p, dict)
        )
        await _remember(f"confirm {confirm_id}", reply, {"confirm_id": confirm_id})
        confirm_id_out = result.confirm_id
        return {
            "parts": parts,
            "confirm_id": confirm_id_out,
            "trace_id": current_trace_id(),
        }

    # -- 2. wallet signature ----------------------------------------------
    signed_tx = body.signed_tx.strip()
    flow_id = body.flow_id.strip()
    if not signed_tx and channel == "terminal" and _B64_RE.match(text.strip()):
        settings = get_settings()
        if not settings.RAIL_ALLOW_DEV_SIGN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="text-carried signatures are refused (RAIL_ALLOW_DEV_SIGN=0)",
            )
        try:
            ledger = await chatmod._get_ledger_store().load(user.id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ledger unavailable; signature not accepted",
            )
        pending = list((ledger.pending_invest if ledger else {}) or {})
        if len(pending) == 1:
            signed_tx, flow_id = text.strip(), pending[0]
        elif len(pending) != 0:
            # Ambiguous: more than one open flow, so a bare pasted signature
            # cannot be bound. The wallet must resubmit with an explicit
            # flow_id; nothing is accepted here.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="flow_id is required with signed_tx",
            )
    if signed_tx:
        if not flow_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="flow_id is required with signed_tx",
            )
        result = await orchestrator.handle_wallet_signature(user.id, flow_id, signed_tx)
        ok = bool(result.invest and result.invest.get("ok"))
        if ok:
            positions = await _live_positions(token)
            png = sleeve_donut(positions)
            amount = (
                result.receipt.amount
                if result.receipt and result.receipt.amount is not None
                else "?"
            )
            parts.append(_text_part(f"Done. ${amount} in Rail Stock Sleeve."))
            if png is not None:
                parts.append(_chart_part(png))
            else:
                parts.append(_text_part("Indexing, I'll send the chart when it lands."))
        else:
            detail = ""
            if result.invest:
                detail = str(result.invest.get("detail") or "")
            parts.append(
                _text_part(
                    detail or (result.narration or "Enrollment did not complete.")
                )
            )
        await _remember("signed <tx>", parts[0].get("text", "") if parts else "")
        return {
            "parts": parts,
            "confirm_id": confirm_id_out,
            "trace_id": current_trace_id(),
        }

    # -- 3. money turns ----------------------------------------------------
    from miriam_agent.orchestrator import classify_turn

    if chatmod.get_settings().ALLOW_CHAT_INFLOW_SYNTH and (
        chatmod.looks_like_inflow_alert(text)
    ):
        # Demo-only escape hatch, mirroring the chat path: text is not a
        # payment fact, so it never mints ledger money unless the deployment
        # has explicitly turned the demo behaviour on.
        result = await orchestrator.handle_inflow(
            user.id,
            payment_id=chatmod.inflow_id_for_alert(text),
            amount=chatmod._alert_amount(text),
            source_raw=text,
        )
    elif classify_turn(text) == "orchestrator":
        if wallet_address:
            result = await orchestrator.handle(
                Event(
                    type="utterance",
                    user_id=user.id,
                    text=text,
                    wallet_address=wallet_address,
                    channel=channel,
                    thread_id=space_id,
                )
            )
        else:
            result = await orchestrator.handle_utterance(
                user.id, text, channel=channel, thread_id=space_id
            )
    else:
        result = None
    if result is not None:
        parts.append(_text_part(result.narration or "Noted."))
        # 70/30 proof: the split receipt carries before/after sleeves.
        if result.receipt is not None and result.receipt.action == "inflow_split":
            try:
                before = result.receipt.sleeves_before
                after = result.receipt.sleeves_after
                spend = float(
                    Decimal(str(after.get("spendable", 0)))
                    - Decimal(str(before.get("spendable", 0)))
                )
                invest_amt = float(
                    Decimal(str(after.get("savings", 0)))
                    - Decimal(str(before.get("savings", 0)))
                )
                png = split_bar(max(spend, 0), max(invest_amt, 0))
                if png is not None:
                    parts.append(
                        {
                            "type": "chart",
                            "spec": {"kind": "bars", "title": "70/30"},
                            "filename": "portfolio.png",
                            "mime": "image/png",
                            "image_base64": png_base64(png),
                        }
                    )
            except (ArithmeticError, TypeError, ValueError, KeyError):
                pass
        if result.confirm_id and result.card is None:
            # A non-invest challenge (transfer/stash/save-rule/...): the
            # narration already carries "confirm <id>", plus the Face ID line
            # when a live card was minted for this challenge
            # (result.card_action_id, imessage only). Nothing else to render:
            # the card lives in the iMessage transcript and Go edits it in
            # place. The invest branch above is unchanged: a first tap may
            # arrive via the settle endpoint, and the wallet-sign flow after
            # it is untouched.
            pass
        await _remember(
            text, parts[0].get("text", ""), {"confirm_id": result.confirm_id}
        )
        confirm_id_out = result.confirm_id
        return {
            "parts": parts,
            "confirm_id": confirm_id_out,
            "trace_id": current_trace_id(),
        }

    # -- 4. portfolio asks + fallback --------------------------------------
    lowered = text.casefold()
    if any(ask in lowered for ask in _PORTFOLIO_ASKS):
        positions = await _live_positions(token)
        png = sleeve_donut(positions)
        if png is None:
            reply = "Nothing indexed yet."
            parts.append(_text_part(reply))
        else:
            total = sum(
                float(str(p.get("value_usd") or p.get("valueUsd") or 0))
                for p in positions
            )
            reply = f"Your sleeve is holding at ${total:,.2f}."
            parts.append(_text_part(reply))
            parts.append(_chart_part(png))
        await _remember(text, reply)
        return {
            "parts": parts,
            "confirm_id": confirm_id_out,
            "trace_id": current_trace_id(),
        }

    reply = (
        "I can help with spending, saving, and the Solana stock sleeve. "
        "Try 'put 30 into stocks'."
    )
    parts.append(_text_part(reply))
    await _remember(text, reply)
    return {
        "parts": parts,
        "confirm_id": confirm_id_out,
        "trace_id": current_trace_id(),
    }

"""Layer 1 - HANDS. Confirm-time settlement dispatch.

Moved verbatim out of :mod:`miriam_agent.orchestrator` (blob-ratchet split):
the confirm entry point, the challenge-action dispatch, challenge expiry,
the provenance tag, and the turn types (:class:`Event`, :class:`TurnResult`)
that travel with settlement. ``Orchestrator`` mixes this in, so the runtime
surface is unchanged. Hands never imports the turn/channel layers; the one
voice-owned string used here is mirrored below (see _SERVICE_DOWN_LINE).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from miriam_agent.hands.audit import AuditLog, AuditRow, Receipt
from miriam_agent.hands.ledger import (
    Ledger,
    LedgerStore,
    LedgerUnavailable,
    money,
    new_ledger,
)
from miriam_agent.hands.limits import Policy
from miriam_agent.hands.state import HandlerState, ProposedAction, build_state
from miriam_agent.judgment.schema import Decision

logger = logging.getLogger(__name__)


# Hands may not import voice (architecture rule), so the one
# voice-owned outage string used below is mirrored here. The
# pin test test_service_down_line_matches_voice fails if the
# two ever drift apart.
_SERVICE_DOWN_LINE = "I could not complete that. Try again."


EventType = Literal["inflow", "utterance", "confirm", "wallet_signature"]


class Event(BaseModel):
    """Something that happened: money arrived, the user spoke, a tap landed,
    or a wallet signature arrived."""

    model_config = ConfigDict(extra="forbid")

    type: EventType
    user_id: str
    # utterance
    text: str = ""
    # inflow
    inflow_id: str = ""
    amount: Decimal | None = None
    source_raw: str = ""
    # confirm tap
    confirm_id: str = ""
    # Surface that carried the turn ("imessage", "terminal", ...). Only an
    # allowlisted channel may mint a Face ID card; empty means no card.
    channel: str = ""
    # Thread/space the turn arrived on, threaded into a minted card's payload
    # so Go dispatches the live card into the right transcript.
    thread_id: str = ""
    # Where a confirm tap came from ("face_id" for the Messages extension
    # settle path, "" for chat). Recorded on audit rows, never a decision.
    provenance: str = ""
    # wallet that will own the Glider portfolio (base58). Terminal tests send
    # their ephemeral key; production sends the connected wallet address.
    wallet_address: str = ""
    # wallet signature settle
    flow_id: str = ""
    signed_tx: str = ""


@dataclass
class TurnResult:
    """Everything one turn produced, for the caller and for the tests.

    ``state`` is ``None`` only when the ledger could not be read, which is the one
    turn with no STATE to show because there was nothing to read it from.
    """

    state: HandlerState | None = None
    narration: str | None = None
    decision: dict[str, Any] | None = None
    receipt: Receipt | None = None
    confirm_id: str = ""
    audit: list[AuditRow] = field(default_factory=list)
    # True when the turn is silent by design rather than because Voice failed.
    quiet: bool = False
    # Allocate card (tap time): kind/amount/strategy/flow/sign payload. The
    # sign payload is the base64 Solana transaction the wallet must sign.
    card: dict[str, Any] | None = None
    # Settle result (wallet-signature time): ok/enrollment/funding/positions.
    invest: dict[str, Any] | None = None
    # Go live-card id minted for the staged challenge (""). Empty on every
    # non-card turn; lets surfaces render the Face ID affordance.
    card_action_id: str = ""


# Provider failures that must NOT burn the approval: the challenge is
# reopened so a retap can retry instead of hitting CHALLENGE_EXPIRED.
RETRIABLE_REASONS = frozenset(
    {
        "GLIDER_UNREACHABLE",
        "GLIDER_NOT_LIVE",
        "RATE_UNAVAILABLE",
        "PAJ_INITIATE_FAILED",
        "ONRAMP_FAILED",
        "NO_GO_TOKEN",
        "AUTOMATIONS_UNREACHABLE",
    }
)


def challenge_owner_ok(challenge: Any, ledger: Ledger, event: Event) -> bool:
    """True when the tap belongs to the challenge owner.

    A confirm_id issued to another user must never execute: every settle
    leg checks this before dispatching and burns the id with a
    CHALLENGE_MISMATCH refusal when it fails.
    """
    if challenge is None:
        return False
    owner = getattr(challenge, "user_id", "") or ""
    if owner and owner != ledger.user_id:
        return False
    event_user = getattr(event, "user_id", "") or ""
    if event_user and owner and event_user != owner:
        return False
    return True


def maybe_reopen_challenge(
    ledger: Ledger, challenge: Any, receipt: Receipt | None
) -> None:
    """Reopen a consumed challenge when the failure is transient.

    Consume-first settlement would otherwise burn the approval on a blip:
    retap yields CHALLENGE_EXPIRED with no path to retry. A retriable
    provider failure flips the challenge back to pending so the next tap
    runs the leg again; declines, mismatches and executions stay terminal.
    """
    if receipt is None or receipt.status != "rejected":
        return
    reasons = set(receipt.reasons or [])
    if reasons & RETRIABLE_REASONS:
        challenge.status = "pending"
        ledger.challenges[challenge.id] = challenge


def _action_type(value: str) -> Any:
    """Narrow a stored challenge action back to the ProposedAction vocabulary."""
    return (
        value
        if value
        in (
            "transfer",
            "purchase",
            "lock",
            "unlock",
            "invest",
            "order",
            "rebalance",
            "set_allocation",
            "pause",
            "resume",
            "onramp",
            "offramp",
            "paj_otp",
        )
        else "none"
    )


class SettlementMixin:
    """Confirm entry, dispatch, expiry, turn types.

    Mixed into :class:`miriam_agent.orchestrator.Orchestrator`, which
    provides the runtime surface declared below (store, policy, clock,
    voice). Declared here so the type gate checks the mixin against the
    contract instead of `Any`.
    """

    store: LedgerStore
    policy: Policy
    clock: Callable[[], datetime]

    async def _speak(
        self, state: HandlerState, *, utterance: str = ""
    ) -> str | None: ...

    @staticmethod
    def _tag_provenance(result: TurnResult, provenance: str) -> TurnResult:
        rows = [
            row.model_copy(
                update={"detail": f"{row.detail} [via {provenance}]".strip()}
            )
            for row in result.audit
        ]
        return TurnResult(
            state=result.state,
            narration=result.narration,
            decision=result.decision,
            receipt=result.receipt,
            confirm_id=result.confirm_id,
            audit=rows,
            quiet=result.quiet,
            card=result.card,
            invest=result.invest,
            card_action_id=result.card_action_id,
        )

    async def handle_confirm(
        self, user_id: str, confirm_id: str, yes: bool, *, provenance: str = ""
    ) -> TurnResult:
        """Settle a challenge the user tapped, by its Hands-issued id.

        ``yes=False`` is a decline: the challenge is closed and nothing moves.
        There is no other way in. A chat word never reaches this method, so
        "yes" typed into a message is just a message. ``provenance`` names the
        factor when the tap arrives off-chat ("face_id"); it lands on audit
        rows, never on the decision.
        """
        if yes:
            return await self.handle(
                Event(
                    type="confirm",
                    user_id=user_id,
                    confirm_id=confirm_id,
                    provenance=provenance,
                )
            )
        # A decline goes through the ledger too, so it needs the same typed
        # refusal as the rest: without this a ledger outage answered a tap with a
        # 500 (and leaked the exception text down the SSE stream).
        try:
            return await self._decline_challenge(user_id, confirm_id)
        except LedgerUnavailable:
            logger.error("ledger unavailable for %s; refusing the decline", user_id)
            return TurnResult(narration=_SERVICE_DOWN_LINE)

    async def _handle_confirm(self, ledger: Ledger, event: Event) -> TurnResult:
        """Execute the exact action a confirm_id was issued for.

        The challenge, not the sentence, is the authorisation: the amount, the
        destination, the sleeve and the user come from what Hands stored when it
        asked. Settle only if all match, one use, and a decline burns it.
        """
        audit = AuditLog()
        now = self.clock()
        challenge = ledger.challenges.get(event.confirm_id)

        if challenge is None or not challenge.is_open(now):
            reason = "NO_SUCH_CHALLENGE" if challenge is None else "CHALLENGE_EXPIRED"
            rejected: Receipt = Receipt(
                id=f"rcpt_rejected_{event.confirm_id or 'missing'}",
                at=now,
                status="rejected",
                action="confirm",
                currency=ledger.currency,
                reasons=[reason],
                detail="that confirmation does not match anything open",
            )
            ledger.remember_receipt(rejected)
            await self.store.save(ledger)
            state = build_state(
                ledger=ledger,
                policy=self.policy,
                decision={
                    "id": "",
                    "next_mode": "ask",
                    "action_choice": "deny",
                    "reasons": [reason],
                },
                now=now,
            )
            return TurnResult(
                state=state,
                narration=await self._speak(state),
                decision=state.decision,
                receipt=rejected,
                audit=audit.rows,
            )

        # Ownership binding FIRST, before any leg dispatches: a confirm_id
        # issued to another user must never execute here. Burn it (consumed)
        # and return a mismatch refusal -- never fall through to a leg.
        if challenge.user_id and challenge.user_id != ledger.user_id:
            challenge.status = "consumed"
            ledger.challenges[challenge.id] = challenge
            mismatched_owner: Receipt = Receipt(
                id=f"rcpt_rejected_{event.confirm_id or 'missing'}",
                at=now,
                status="rejected",
                action="confirm",
                currency=ledger.currency,
                amount=challenge.amount,
                counterparty=challenge.counterparty,
                sleeve=challenge.sleeve,
                reasons=["CHALLENGE_MISMATCH"],
                detail="that confirmation was issued to a different account",
            )
            ledger.remember_receipt(mismatched_owner)
            await self.store.save(ledger)
            state = build_state(
                ledger=ledger,
                policy=self.policy,
                decision={
                    "id": "",
                    "next_mode": "ask",
                    "action_choice": "deny",
                    "reasons": ["CHALLENGE_MISMATCH"],
                },
                now=now,
            )
            return TurnResult(
                state=state,
                narration=await self._speak(state),
                decision=state.decision,
                receipt=mismatched_owner,
                audit=audit.rows,
            )
        # Cross-user tap at the event level (belt and suspenders: the ledger
        # above is loaded for event.user_id, so a challenge naming someone
        # else is already caught; this covers a spoofed event user_id).
        if event.user_id and challenge.user_id and event.user_id != challenge.user_id:
            challenge.status = "consumed"
            ledger.challenges[challenge.id] = challenge
            mismatched_event: Receipt = Receipt(
                id=f"rcpt_rejected_{event.confirm_id or 'missing'}",
                at=now,
                status="rejected",
                action="confirm",
                currency=ledger.currency,
                amount=challenge.amount,
                counterparty=challenge.counterparty,
                sleeve=challenge.sleeve,
                reasons=["CHALLENGE_MISMATCH"],
                detail="that confirmation was issued to a different account",
            )
            ledger.remember_receipt(mismatched_event)
            await self.store.save(ledger)
            state = build_state(
                ledger=ledger,
                policy=self.policy,
                decision={
                    "id": "",
                    "next_mode": "ask",
                    "action_choice": "deny",
                    "reasons": ["CHALLENGE_MISMATCH"],
                },
                now=now,
            )
            return TurnResult(
                state=state,
                narration=await self._speak(state),
                decision=state.decision,
                receipt=mismatched_event,
                audit=audit.rows,
            )

        if challenge.action == "invest":
            # The tap approves the money; the wallet signature (a separate
            # event) approves the chain write. Stage 1 runs here and returns
            # the transaction the wallet must sign.
            return await self._handle_confirm_invest(ledger, event, challenge)

        if challenge.action in ("order", "set_allocation"):
            # Orders are Rail-signed server-side: no wallet signature follows.
            # Stage + replay via _obtain_and_replay, then return the card.
            return await self._handle_confirm_order(ledger, event, challenge)

        if challenge.action in ("rebalance", "pause", "resume"):
            # Immediate actions: rebalance/pause/resume run now, no staging.
            return await self._handle_confirm_rebalance(ledger, event, challenge)

        if challenge.action == "onramp":
            # NGN -> USDC: quote, Paj initiate, await OTP, then order.
            return await self._handle_confirm_funding(ledger, event, challenge)

        if challenge.action == "offramp":
            # Funds-OUT is app-only: stage the envelope, never POST.
            return await self._handle_confirm_offramp(ledger, event, challenge)

        if challenge.action == "save_rule":
            # Save-rule change: resolve the automation at settle time and
            # update it. The percentage/amount comes from the challenge
            # binding, never from a card payload.
            return await self._handle_confirm_save_rule(ledger, event, challenge)

        action = ProposedAction(
            type=_action_type(challenge.action),
            amount=challenge.amount,
            counterparty=challenge.counterparty,
            sleeve=challenge.sleeve,
            source="user",
        )
        # The challenge already carries the user's confirmation, so the decision
        # that authorises the movement is rebuilt from it. No second JEV call is
        # made and no model is consulted.
        decision = Decision(
            id=challenge.decision_id,
            at=now,
            next_mode="act",
            action_choice="allow",
            suggested_amount=challenge.amount,
            reasons=list(challenge.reasons),
            confirm_id=challenge.id,
        )
        stored_destination = (
            challenge.destination or challenge.counterparty or challenge.sleeve
        )
        action_destination = action.counterparty or action.sleeve
        if challenge.user_id and challenge.user_id != ledger.user_id:
            bound = False
        else:
            bound = (
                money(action.amount or 0) == money(challenge.amount)
                and action_destination == stored_destination
                and money(action.amount or 0) > 0
            )
        if not bound:
            challenge.status = "consumed"
            ledger.challenges[challenge.id] = challenge
            mismatched: Receipt = Receipt(
                id=f"rcpt_rejected_{event.confirm_id or 'missing'}",
                at=now,
                status="rejected",
                action="confirm",
                currency=ledger.currency,
                amount=challenge.amount,
                counterparty=challenge.counterparty,
                sleeve=challenge.sleeve,
                reasons=["CHALLENGE_MISMATCH"],
                detail="that confirmation does not match the stored challenge",
            )
            ledger.remember_receipt(mismatched)
            await self.store.save(ledger)
            state = build_state(
                ledger=ledger,
                policy=self.policy,
                decision={
                    "id": "",
                    "next_mode": "ask",
                    "action_choice": "deny",
                    "reasons": ["CHALLENGE_MISMATCH"],
                },
                now=now,
            )
            return TurnResult(
                state=state,
                narration=await self._speak(state),
                decision=state.decision,
                receipt=mismatched,
                audit=audit.rows,
            )
        challenge.status = "consumed"
        ledger.challenges[challenge.id] = challenge

        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            decision=decision.model_dump(mode="json"),
            now=now,
        )
        outcome = await self._execute(ledger, state, decision, action)
        receipt = outcome.receipt if outcome is not None else None
        if outcome is not None:
            for row in outcome.audit:
                audit.record(row)
            ledger = outcome.ledger

        execution = self._execution_from(receipt)
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            decision=decision.model_dump(mode="json"),
            execution=execution,
            now=self.clock(),
        )
        await self._sync_card_terminal(challenge, receipt)
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
            confirm_id=challenge.id,
            audit=audit.rows,
        )

    async def expire_challenge(self, user_id: str, confirm_id: str) -> TurnResult:
        """Expire a challenge whose Go card died first (TTL hit on Go side).

        Mirrors the decline path but writes CHALLENGE_EXPIRED. Idempotent:
        an unknown or already-terminal challenge returns a terminal result
        without mutating the ledger. A ledger outage is a typed refusal.
        """
        try:
            ledger = await self.store.load(user_id) or new_ledger(user_id)
        except LedgerUnavailable:
            logger.error("ledger unavailable for %s; refusing the expire", user_id)
            return TurnResult(narration=_SERVICE_DOWN_LINE)
        now = self.clock()
        challenge = ledger.challenges.get(confirm_id)
        if challenge is None or not challenge.is_open(now):
            state = build_state(
                ledger=ledger,
                policy=self.policy,
                decision={
                    "id": "",
                    "next_mode": "ask",
                    "action_choice": "deny",
                    "reasons": ["CHALLENGE_EXPIRED"],
                },
                now=now,
            )
            return TurnResult(
                state=state,
                narration="That confirmation has expired.",
                decision=state.decision,
                receipt=None,
            )
        challenge.status = "expired"
        ledger.challenges[challenge.id] = challenge
        existing = {**ledger.sleeves}
        receipt = Receipt(
            id=f"rcpt_expired_{confirm_id or 'missing'}",
            at=now,
            status="rejected",
            action="expire",
            currency=ledger.currency,
            amount=challenge.amount,
            counterparty=challenge.counterparty,
            reasons=["CHALLENGE_EXPIRED"],
            sleeves_before={k: str(v) for k, v in existing.items()},
            sleeves_after={k: str(v) for k, v in ledger.sleeves.items()},
            detail="the confirmation expired; nothing moved",
        )
        ledger.remember_receipt(receipt)
        await self.store.save(ledger)
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            decision={
                "id": challenge.decision_id,
                "next_mode": "act",
                "action_choice": "none",
                "reasons": ["CHALLENGE_EXPIRED"],
                "confirm_id": "",
            },
            now=now,
        )
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
        )

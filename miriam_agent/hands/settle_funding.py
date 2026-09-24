"""Layer 1 - HANDS. Settle legs for NGN <-> crypto funding.

Moved verbatim out of :mod:`miriam_agent.orchestrator` (blob-ratchet split):
the onramp leg, the Paj OTP leg, the app-only offramp stager, and the
funding-fact helpers Voice speaks from. Mixed into ``Orchestrator``; the
runtime surface is unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from miriam_agent.hands.ledger import Ledger
from miriam_agent.hands.settlement import Event, TurnResult
from miriam_agent.hands.state import ProposedAction, build_state
from miriam_agent.judgment.schema import Decision

# Card keys that may ride on STATE.execution.funding. A whitelist, so a card
# can never smuggle a figure Voice did not earn from the Go result.
_FUNDING_FACT_KEYS = (
    "account_number",
    "account_name",
    "bank",
    "token_amount",
    "order_id",
    "rate",
    "amount",
    "recipient",
)


def _funding_facts(card: Any) -> dict[str, str]:
    """Speakable funding facts from a hands card, for STATE.execution.

    Voice may only state figures already in STATE, so the bank details the
    user must act on travel here rather than living only in the receipt
    detail string or the dropped TurnResult card.
    """
    if not isinstance(card, dict):
        return {}
    facts: dict[str, str] = {}
    for key in _FUNDING_FACT_KEYS:
        value = card.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            facts[key] = text
    return facts


def _open_paj_otp_challenge(ledger: Ledger, now: datetime) -> Any | None:
    """The open Paj OTP challenge awaiting the user's code, if any."""
    open_ones = [
        c
        for c in (ledger.challenges or {}).values()
        if getattr(c, "action", "") == "paj_otp" and c.is_open(now)
    ]
    if not open_ones:
        return None
    open_ones.sort(key=lambda c: c.created_at, reverse=True)
    return open_ones[0]


class FundingSettlementMixin:
    """Settle legs for NGN <-> crypto funding."""

    async def _handle_confirm_funding(
        self, ledger: Ledger, event: Event, challenge: Any
    ) -> TurnResult:
        """Settle an onramp tap: quote, Paj initiate, then await the OTP."""
        from miriam_agent.hands.audit import AuditLog as _AuditLog
        from miriam_agent.hands.funding_settle import prepare_onramp

        audit = _AuditLog()
        now = self.clock()
        challenge.status = "consumed"
        ledger.challenges[challenge.id] = challenge

        action = ProposedAction(
            type="onramp",
            amount=challenge.amount,
            counterparty=challenge.counterparty,
            sleeve=challenge.sleeve,
            source="user",
            side="buy",
        )
        decision = Decision(
            id=challenge.decision_id,
            at=now,
            next_mode="act",
            action_choice="allow",
            suggested_amount=challenge.amount,
            reasons=list(challenge.reasons),
            confirm_id=challenge.id,
        )
        meta = getattr(challenge, "meta", {}) or {}
        receipt, card, ledger = await prepare_onramp(
            store=self.store,
            ledger=ledger,
            user_id=event.user_id,
            token=self.go_token,
            amount=challenge.amount,
            symbol=challenge.counterparty,
            provider=str(meta.get("provider") or "paj"),
            decision_id=challenge.decision_id,
            confirm_id=challenge.id,
            at=now,
        )
        await self.store.save(ledger)
        confirm_out = challenge.id
        if isinstance(card, dict):
            if card.get("kind") == "paj_otp":
                confirm_out = str(card.get("otp_challenge_id") or challenge.id)
            elif card.get("kind") == "onramp_order":
                # Executed: the bank details are the message, nothing to tap.
                confirm_out = ""
        execution = self._execution_from(receipt)
        if execution is not None:
            facts = _funding_facts(card)
            if facts:
                execution = execution.model_copy(update={"funding": facts})
        decision = decision.model_copy(update={"confirm_id": confirm_out})
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            decision=decision.model_dump(mode="json"),
            execution=execution,
            now=self.clock(),
        )
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
            confirm_id=confirm_out,
            card=card,
            audit=audit.rows,
        )

    async def _handle_funding_otp(
        self, ledger: Ledger, event: Event, otp_challenge: Any, otp: str
    ) -> TurnResult:
        """Verify the Paj OTP from the user's next message, then create the order."""
        from miriam_agent.hands.audit import AuditLog as _AuditLog
        from miriam_agent.hands.funding_settle import submit_paj_otp

        audit = _AuditLog()
        now = self.clock()
        action = ProposedAction(
            type="onramp",
            amount=otp_challenge.amount,
            counterparty=otp_challenge.counterparty,
            sleeve=otp_challenge.sleeve,
            source="user",
            side="buy",
        )
        decision = Decision(
            id=otp_challenge.decision_id,
            at=now,
            next_mode="act",
            action_choice="allow",
            suggested_amount=otp_challenge.amount,
            reasons=list(otp_challenge.reasons),
            confirm_id=otp_challenge.id,
        )
        receipt, card, ledger = await submit_paj_otp(
            store=self.store,
            ledger=ledger,
            otp_challenge=otp_challenge,
            otp=otp,
            token=self.go_token,
            at=now,
        )
        await self.store.save(ledger)
        # Executed orders close the loop; a failed verify keeps the OTP
        # challenge open for retry.
        confirm_out = ""
        if receipt is None or receipt.status != "executed":
            confirm_out = otp_challenge.id
        execution = self._execution_from(receipt)
        if execution is not None:
            facts = _funding_facts(card)
            if facts:
                execution = execution.model_copy(update={"funding": facts})
        decision = decision.model_copy(update={"confirm_id": confirm_out})
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            decision=decision.model_dump(mode="json"),
            execution=execution,
            now=self.clock(),
        )
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
            confirm_id=confirm_out,
            card=card,
            audit=audit.rows,
        )

    async def _handle_confirm_offramp(
        self, ledger: Ledger, event: Event, challenge: Any
    ) -> TurnResult:
        """Stage the passcode-gated offramp envelope for the Rail app."""
        from miriam_agent.hands.audit import AuditLog as _AuditLog
        from miriam_agent.hands.funding_settle import stage_offramp

        audit = _AuditLog()
        now = self.clock()
        challenge.status = "consumed"
        ledger.challenges[challenge.id] = challenge
        action = ProposedAction(
            type="offramp",
            amount=challenge.amount,
            counterparty=challenge.counterparty,
            sleeve=challenge.sleeve,
            source="user",
            side="sell",
        )
        receipt, card, ledger = await stage_offramp(
            store=self.store,
            ledger=ledger,
            amount=challenge.amount,
            counterparty=challenge.counterparty,
            decision_id=challenge.decision_id,
            confirm_id=challenge.id,
            at=now,
        )
        await self.store.save(ledger)
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            now=now,
        )
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
            confirm_id=challenge.id,
            card=card,
            audit=audit.rows,
        )

"""Layer 1 - HANDS. Settle legs for NGN <-> crypto funding.

Moved verbatim out of :mod:`miriam_agent.orchestrator` (blob-ratchet split):
the onramp leg, the Paj OTP leg, the app-only offramp stager, and the
funding-fact helpers Voice speaks from. Mixed into ``Orchestrator``; the
runtime surface is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from miriam_agent.hands.ledger import Ledger, LedgerStore
from miriam_agent.hands.limits import Policy
from miriam_agent.hands.settlement import (
    Event,
    TurnResult,
    challenge_owner_ok,
    maybe_reopen_challenge,
)
from miriam_agent.hands.state import (
    Execution,
    HandlerState,
    ProposedAction,
    build_state,
)
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
    "category",
    "network",
    "airbills_id",
    "status",
    "amount_usdc",
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
    """Settle legs for NGN <-> crypto funding.

    Mixed into :class:`miriam_agent.orchestrator.Orchestrator`, which
    provides the runtime surface declared below. Declared here so the
    type gate checks the legs against the contract instead of `Any`.
    """

    store: LedgerStore
    policy: Policy
    go_token: str | None
    clock: Callable[[], datetime]

    async def _speak(
        self, state: HandlerState, *, utterance: str = ""
    ) -> str | None: ...
    def _execution_from(self, receipt: Any) -> Execution | None: ...

    async def _funding_mismatch(
        self,
        ledger: Ledger,
        event: Event,
        challenge: Any,
        *,
        receipt_action: str,
        proposed_type: str,
        side: str,
    ) -> TurnResult:
        """Burn a cross-user confirm_id with a mismatch refusal, never execute."""
        from miriam_agent.hands.audit import Receipt as _Receipt

        now = self.clock()
        challenge.status = "consumed"
        ledger.challenges[challenge.id] = challenge
        receipt = _Receipt(
            id=f"rcpt_rejected_{challenge.id}",
            at=now,
            status="rejected",
            action=receipt_action,
            currency=ledger.currency,
            amount=challenge.amount,
            counterparty=challenge.counterparty,
            sleeve=challenge.sleeve,
            decision_id=challenge.decision_id,
            reasons=["CHALLENGE_MISMATCH"],
            detail="that confirmation was issued to a different account",
        )
        ledger.remember_receipt(receipt)
        await self.store.save(ledger)
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=ProposedAction(
                type=proposed_type,  # type: ignore[arg-type]
                amount=challenge.amount,
                counterparty=challenge.counterparty,
                sleeve=challenge.sleeve,
                source="user",
                side=side,  # type: ignore[arg-type]
            ),
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
            receipt=receipt,
            confirm_id=challenge.id,
            audit=[],
        )

    async def _handle_confirm_funding(
        self, ledger: Ledger, event: Event, challenge: Any
    ) -> TurnResult:
        """Settle an onramp tap: quote, Paj initiate, then await the OTP."""
        from miriam_agent.hands.audit import AuditLog as _AuditLog
        from miriam_agent.hands.funding_settle import prepare_onramp

        audit = _AuditLog()
        now = self.clock()
        if not challenge_owner_ok(challenge, ledger, event):
            return await self._funding_mismatch(
                ledger,
                event,
                challenge,
                receipt_action="onramp_prepare",
                proposed_type="onramp",
                side="buy",
            )
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
        # Transient quote/initiate failures reopen the challenge so a retap
        # can retry instead of dying on CHALLENGE_EXPIRED.
        maybe_reopen_challenge(ledger, challenge, receipt)
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

    async def _handle_confirm_bill(
        self, ledger: Ledger, event: Event, challenge: Any
    ) -> TurnResult:
        """Pay an Airbills bill after the confirm tap and speak the receipt."""
        from miriam_agent.hands.audit import AuditLog as _AuditLog
        from miriam_agent.hands.audit import Receipt as _Receipt
        from miriam_agent.hands.audit import sleeves_snapshot
        from miriam_agent.integrations.go_client import get_go_client

        audit = _AuditLog()
        now = self.clock()
        meta = getattr(challenge, "meta", {}) or {}
        category = str(meta.get("category") or "airtime")
        recipient = str(meta.get("recipient") or challenge.counterparty or "")
        action = ProposedAction(
            type="bill",
            amount=challenge.amount,
            counterparty=recipient,
            sleeve=challenge.sleeve,
            source="user",
            raw=f"{category} {recipient}",
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

        def _rejected(reasons: list[str], detail: str) -> Receipt:
            before = sleeves_snapshot(ledger.sleeves)
            receipt = _Receipt(
                id=f"rcpt_bill_{challenge.id[-8:]}",
                at=now,
                status="rejected",
                action="bill_pay",
                currency=ledger.currency,
                amount=challenge.amount,
                counterparty=recipient,
                sleeve=challenge.sleeve,
                decision_id=challenge.decision_id,
                idempotency_key=f"bill:{challenge.id}",
                reasons=reasons,
                sleeves_before=before,
                sleeves_after=before,
                detail=detail,
                confirm_id=challenge.id,
            )
            ledger.remember_receipt(receipt)
            return receipt

        if not self.go_token:
            receipt = _rejected(
                ["NO_GO_TOKEN"], "no Go host token on this path; the bill was not paid"
            )
        else:
            client = get_go_client()
            network_name = ""
            network_id = ""
            if category in ("airtime", "data"):
                try:
                    detected = await client.detect_network(self.go_token, recipient)
                except Exception as exc:  # noqa: BLE001
                    detected = {"_tool_error": str(exc)[:200]}
                if isinstance(detected, dict) and not detected.get("_tool_error"):
                    network_id = str(
                        detected.get("network_id") or detected.get("networkId") or ""
                    )
                    network_name = str(
                        detected.get("network") or detected.get("name") or ""
                    )
            payload = {
                "category": category,
                "recipient": recipient,
                "amount_ngn": float(challenge.amount),
            }
            if network_id:
                payload["network_id"] = network_id
            try:
                paid = await client.pay_bill(
                    self.go_token,
                    payload,
                    idempotency_key=f"bill:{challenge.id}",
                )
            except Exception as exc:  # noqa: BLE001
                paid = {"_tool_error": str(exc)[:200]}
            if not isinstance(paid, dict) or paid.get("_tool_error"):
                receipt = _rejected(
                    ["BILL_PAY_FAILED"],
                    str((paid or {}).get("_tool_error") or "Airbills did not accept the bill"),
                )
                card = None
            else:
                before = sleeves_snapshot(ledger.sleeves)
                airbills_id = str(paid.get("airbills_id") or paid.get("order_id") or "")
                receipt = _Receipt(
                    id=f"rcpt_bill_{challenge.id[-8:]}",
                    at=now,
                    status="executed",
                    action="bill_pay",
                    currency="NGN",
                    amount=challenge.amount,
                    counterparty=recipient,
                    sleeve=challenge.sleeve,
                    decision_id=challenge.decision_id,
                    idempotency_key=f"bill:{challenge.id}",
                    rail_reference=airbills_id,
                    confirm_id=challenge.id,
                    sleeves_before=before,
                    sleeves_after=before,
                    detail=(
                        f"Airbills {category} {challenge.amount} NGN for {recipient}"
                        f" on {network_name or network_id}. Reference {airbills_id}."
                    ),
                )
                ledger.remember_receipt(receipt)
                card = {
                    "kind": "bill_pay",
                    "category": category,
                    "recipient": recipient,
                    "amount": f"{challenge.amount:g} NGN",
                    "network": network_name or network_id,
                    "airbills_id": airbills_id,
                    "status": str(paid.get("status") or ""),
                    "amount_usdc": str(paid.get("amount_usdc") or ""),
                }
        if "card" not in locals():
            card = None
        if receipt.status == "executed":
            challenge.status = "consumed"
        else:
            challenge.status = "pending"
        ledger.challenges[challenge.id] = challenge
        await self.store.save(ledger)
        execution = self._execution_from(receipt)
        facts = _funding_facts(card)
        if execution is not None and facts:
            execution = execution.model_copy(update={"funding": facts})
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
            confirm_id="",
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
        if not challenge_owner_ok(challenge, ledger, event):
            return await self._funding_mismatch(
                ledger,
                event,
                challenge,
                receipt_action="offramp_stage",
                proposed_type="offramp",
                side="sell",
            )
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

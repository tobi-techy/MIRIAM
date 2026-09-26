"""Layer 1 - HANDS. Transfers, rails, rollback, and the intent parse.

Deterministic code only. No LLM, no JEV.

This is the only module that moves money out of the user's sleeves, and it will
only do so when a typed decision says to. The order inside
:func:`execute_transfer` is deliberate:

1. idempotency first, so a retry replays the original receipt,
2. the decision is re-checked here, because Hands does not trust the caller,
3. limits are re-checked here, because Hands does not trust the caller either,
4. the rail is called, and only if it reports success is the ledger debited.

A rail failure changes nothing and produces a ``rejected`` receipt. A ledger
commit failure *after* a successful rail call is compensated with
:meth:`Rail.reverse`, so the ledger and the rail do not drift apart.

The sentence-to-action parser lives in :mod:`miriam_agent.hands.nl` (regex,
not a model) and is re-exported here. Handing a model a ``send_money`` tool
is exactly the design this package exists to prevent.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from miriam_agent.hands.audit import AuditRow, Receipt, sleeves_snapshot
from miriam_agent.hands.ledger import Ledger, LedgerStore, Movement, money
from miriam_agent.hands.limits import Policy, evaluate_limits
from miriam_agent.hands.nl import parse_amount as parse_amount
from miriam_agent.hands.nl import parse_transfer_utterance as parse_transfer_utterance
from miriam_agent.hands.state import (
    HandlerState,
    ProposedAction,
    require_complete,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransferInstruction:
    """One movement, described for a rail. Immutable on purpose.

    ``confirm_id``/``receipt_id`` ride along so Go can bind the settlement to
    the challenge that authorised it. Sent as X-Miriam-Confirm-Id /
    X-Miriam-Receipt-Id where the client supports extra headers, and always
    persisted on the receipt.
    """

    user_id: str
    amount: Decimal
    currency: str
    counterparty: str
    sleeve: str
    idempotency_key: str
    purpose: str = "transfer"
    confirm_id: str = ""
    receipt_id: str = ""


@dataclass(frozen=True)
class RailOutcome:
    """What a rail said about one instruction."""

    ok: bool
    reference: str = ""
    error: str = ""


class Rail(Protocol):
    """The thing that actually moves money. Go is the real one."""

    async def execute(self, instruction: TransferInstruction) -> RailOutcome: ...

    async def reverse(
        self, instruction: TransferInstruction, reference: str
    ) -> RailOutcome: ...


class InMemoryRail:
    """A rail that records what it was asked to do and succeeds.

    ``fail_with`` makes every call fail, which is how the yield tests exercise
    the "partner is down" path without a network.
    """

    def __init__(self, *, fail_with: str | None = None) -> None:
        self.fail_with = fail_with
        self.calls: list[TransferInstruction] = []
        self.reversals: list[str] = []

    async def execute(self, instruction: TransferInstruction) -> RailOutcome:
        self.calls.append(instruction)
        if self.fail_with:
            return RailOutcome(ok=False, error=self.fail_with)
        return RailOutcome(ok=True, reference=f"mem_{uuid.uuid4().hex[:8]}")

    async def reverse(
        self, instruction: TransferInstruction, reference: str
    ) -> RailOutcome:
        self.reversals.append(reference)
        return RailOutcome(ok=True, reference=f"rev_{reference}")


class GoRail:
    """The real rail: the Go backend. Python never keeps the ledger's money.

    Only the two verbs the Go API actually exposes are wired: a send to a
    counterparty, and a move between the user's own spending and stash wallets.
    A yield instruction is refused rather than guessed, so Hands parks the money
    under policy instead of pretending a partner posted it.
    """

    def __init__(self, token: str) -> None:
        self.token = token

    async def execute(self, instruction: TransferInstruction) -> RailOutcome:
        from miriam_agent.integrations.go_client import get_go_client

        client = get_go_client()
        try:
            if instruction.purpose == "yield":
                return RailOutcome(
                    ok=False, error="no yield rail is configured on this deployment"
                )
            if instruction.counterparty:
                result = await client.send_money(
                    self.token,
                    recipient=instruction.counterparty,
                    amount=float(instruction.amount),
                    idempotency_key=instruction.idempotency_key,
                    confirm_id=instruction.confirm_id or None,
                    receipt_id=instruction.receipt_id or None,
                )
            else:
                result = await client.transfer_to_stash(
                    self.token,
                    amount=float(instruction.amount),
                    idempotency_key=instruction.idempotency_key,
                    confirm_id=instruction.confirm_id or None,
                    receipt_id=instruction.receipt_id or None,
                )
        except Exception as exc:  # noqa: BLE001 - a rail error is a business result
            logger.warning("go rail failed: %s", exc)
            return RailOutcome(ok=False, error=str(exc))
        return RailOutcome(ok=True, reference=str(result.get("id") or "go"))

    async def reverse(
        self, instruction: TransferInstruction, reference: str
    ) -> RailOutcome:
        return RailOutcome(
            ok=False,
            error="the go rail cannot reverse a settled transfer; reconcile manually",
        )


@dataclass
class TransferOutcome:
    """The receipt and the (possibly unchanged) ledger."""

    receipt: Receipt
    ledger: Ledger
    audit: list[AuditRow] = field(default_factory=list)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _audit(
    *,
    ledger: Ledger,
    receipt: Receipt,
    decision_id: str,
    trigger: str,
    detail: str = "",
) -> AuditRow:
    return AuditRow(
        id=_id("audit"),
        at=receipt.at,
        user_id=ledger.user_id,
        action=receipt.action,
        trigger=trigger,  # type: ignore[arg-type]
        amount=receipt.amount,
        currency=receipt.currency,
        sleeve=receipt.sleeve,
        counterparty=receipt.counterparty,
        sleeves_before=receipt.sleeves_before,
        sleeves_after=receipt.sleeves_after,
        decision_id=decision_id,
        receipt_id=receipt.id,
        idempotency_key=receipt.idempotency_key,
        detail=detail or receipt.detail,
    )


def _rejected(
    *,
    ledger: Ledger,
    action: str,
    reasons: list[str],
    amount: Decimal | None,
    counterparty: str,
    decision_id: str,
    idempotency_key: str,
    at: datetime,
    detail: str,
) -> Receipt:
    """A receipt for something that did not happen. Nothing moved."""
    snapshot = sleeves_snapshot(ledger.sleeves)
    return Receipt(
        id=_id("rcpt"),
        at=at,
        status="rejected",
        action=action,
        currency=ledger.currency,
        amount=amount,
        counterparty=counterparty,
        sleeve="spendable",
        decision_id=decision_id,
        idempotency_key=idempotency_key,
        reasons=reasons,
        sleeves_before=snapshot,
        sleeves_after=snapshot,
        detail=detail,
    )


def _decision_verdict(decision: dict | None) -> tuple[bool, list[str]]:
    """Whether a typed decision authorises a movement.

    Absent, negative, or not-yet-acted decisions all read as "no". This is a
    second lock on the same door that Judgment opens: if the decision field is
    missing or malformed, nothing moves.
    """
    if not decision:
        return False, ["NO_DECISION"]
    if decision.get("next_mode") != "act":
        return False, ["NOT_ACT_MODE"]
    choice = decision.get("action_choice")
    if choice not in ("allow", "allow_smaller"):
        return False, ["DECISION_DENIED"]
    return True, []


async def execute_transfer(
    *,
    store: LedgerStore,
    ledger: Ledger,
    state: HandlerState,
    policy: Policy,
    rail: Rail,
    at: datetime | None = None,
) -> TransferOutcome:
    """Execute the transfer STATE proposes, if policy and the decision allow.

    Hands enforces the decision; it does not make it. When anything is off the
    result is a ``rejected`` receipt and an unchanged ledger.
    """
    timestamp = at if at is not None else _utcnow()
    action = state.proposed_action
    decision = state.decision
    decision_id = str((decision or {}).get("id", ""))

    if action is None or action.amount is None:
        return TransferOutcome(
            receipt=_rejected(
                ledger=ledger,
                action="transfer",
                reasons=["NO_PROPOSED_ACTION"],
                amount=None,
                counterparty="",
                decision_id=decision_id,
                idempotency_key="",
                at=timestamp,
                detail="nothing structured was proposed",
            ),
            ledger=ledger,
        )

    idempotency_key = f"{decision_id}:{action.signature()}"
    prior = ledger.receipt_for(idempotency_key)
    if prior is not None:
        return TransferOutcome(
            receipt=prior.model_copy(update={"idempotent_replay": True}),
            ledger=ledger,
        )

    require_complete(state)

    allowed, verdict_reasons = _decision_verdict(decision)
    amount = money(action.amount)
    if allowed and decision is not None:
        choice = decision.get("action_choice")
        suggested = decision.get("suggested_amount")
        if choice == "allow_smaller" and suggested is not None:
            amount = money(min(amount, Decimal(str(suggested))))
    if not allowed:
        receipt = _rejected(
            ledger=ledger,
            action="transfer",
            reasons=verdict_reasons,
            amount=amount,
            counterparty=action.counterparty,
            decision_id=decision_id,
            idempotency_key=idempotency_key,
            at=timestamp,
            detail="the decision did not authorise a movement",
        )
        ledger.remember_receipt(receipt)
        await store.save(ledger)
        return TransferOutcome(
            receipt=receipt,
            ledger=ledger,
            audit=[
                _audit(
                    ledger=ledger,
                    receipt=receipt,
                    decision_id=decision_id,
                    trigger="system",
                )
            ],
        )

    report = evaluate_limits(
        policy=policy,
        amount=amount,
        sleeve=action.sleeve,
        spendable=ledger.balance("spendable"),
        rent_required=ledger.rent_first.required,
        rent_reserved=ledger.rent_first.reserved,
        ledger=ledger,
        at=timestamp,
    )
    if not report.allowed:
        receipt = _rejected(
            ledger=ledger,
            action="transfer",
            reasons=report.reasons,
            amount=amount,
            counterparty=action.counterparty,
            decision_id=decision_id,
            idempotency_key=idempotency_key,
            at=timestamp,
            detail="a limit refused this movement; nothing moved",
        )
        ledger.remember_receipt(receipt)
        await store.save(ledger)
        return TransferOutcome(
            receipt=receipt,
            ledger=ledger,
            audit=[
                _audit(
                    ledger=ledger,
                    receipt=receipt,
                    decision_id=decision_id,
                    trigger="system",
                )
            ],
        )

    instruction = TransferInstruction(
        user_id=ledger.user_id,
        amount=amount,
        currency=ledger.currency,
        counterparty=action.counterparty,
        sleeve=action.sleeve,
        idempotency_key=idempotency_key,
        purpose="transfer",
        confirm_id=decision_id,
    )
    outcome = await rail.execute(instruction)
    if not outcome.ok:
        receipt = _rejected(
            ledger=ledger,
            action="transfer",
            reasons=["RAIL_FAILED"],
            amount=amount,
            counterparty=action.counterparty,
            decision_id=decision_id,
            idempotency_key=idempotency_key,
            at=timestamp,
            detail=f"the rail refused it: {outcome.error}",
        )
        ledger.remember_receipt(receipt)
        await store.save(ledger)
        return TransferOutcome(
            receipt=receipt,
            ledger=ledger,
            audit=[
                _audit(
                    ledger=ledger,
                    receipt=receipt,
                    decision_id=decision_id,
                    trigger="confirm",
                )
            ],
        )

    before = sleeves_snapshot(ledger.sleeves)
    # The pre-debit ledger, for the rollback path below. Without it a failed
    # commit has nothing correct to persist: the rail is reversed, so the debit
    # must not survive, and retrying the save on the mutated ledger would either
    # fight the CAS or, if it won, record a debit the rail already took back.
    pre_rail = ledger.model_copy(deep=True)
    try:
        ledger.debit(action.sleeve, amount)
        ledger.record_movement(
            Movement(
                kind="outflow",
                amount=amount,
                sleeve=action.sleeve,
                counterparty=action.counterparty,
                category="transfer",
                ref=outcome.reference,
                at=timestamp,
            )
        )
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="executed",
            action="transfer",
            currency=ledger.currency,
            amount=amount,
            counterparty=action.counterparty,
            sleeve=action.sleeve,
            decision_id=decision_id,
            idempotency_key=idempotency_key,
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            rail_reference=outcome.reference,
            confirm_id=instruction.confirm_id,
            detail=f"moved {amount} {ledger.currency} to {action.counterparty}",
        )
        ledger.remember_receipt(receipt)
        await store.save(ledger)
    except Exception as exc:  # noqa: BLE001 - compensate rather than drift
        logger.error("ledger commit failed after a successful rail call: %s", exc)
        reversal = await rail.reverse(instruction, outcome.reference)
        # Rebuild from the pre-rail state: the rail has been reversed, so the
        # debit must not be in what gets persisted, and the ROLLBACK receipt has
        # to be, or the incident is invisible and a later turn re-sends against a
        # ledger that never heard about it.
        rolled_back = pre_rail
        if reversal.ok:
            reversal_fact = "the rail move was reversed"
        else:
            # The Go rail cannot reverse a settled transfer. The receipt must
            # say so plainly: the move is live on the rail and reconciliation
            # is owed against the reference, not "asked for" and forgotten.
            reversal_fact = (
                f"the rail could NOT reverse it ({reversal.error}); the move is "
                f"live on the rail and reconciliation is owed against "
                f"reference {outcome.reference}"
            )
        receipt = _rejected(
            ledger=rolled_back,
            action="transfer",
            reasons=["ROLLBACK"],
            amount=amount,
            counterparty=action.counterparty,
            decision_id=decision_id,
            idempotency_key=idempotency_key,
            at=timestamp,
            detail=(
                f"the ledger could not record the movement ({exc}); " f"{reversal_fact}"
            ),
        )
        # Keep the rail reference on the receipt, not only in a log line: this is
        # the object the caller returns, audits and can reconcile from.
        receipt.rail_reference = outcome.reference
        rolled_back.remember_receipt(receipt)
        try:
            # Guarded: this is a second write to a store that just failed, so a
            # failure here is expected rather than exceptional.
            await store.save(rolled_back)
        except Exception as save_exc:  # noqa: BLE001 - reconciliation, not a bug
            logger.error(
                "could not persist the rollback for %s (rail reference %s): %s. "
                "The rail was asked to reverse it; reconcile from the reference.",
                ledger.user_id,
                outcome.reference,
                save_exc,
            )
        return TransferOutcome(
            receipt=receipt,
            ledger=rolled_back,
            audit=[
                _audit(
                    ledger=rolled_back,
                    receipt=receipt,
                    decision_id=decision_id,
                    trigger="system",
                )
            ],
        )

    return TransferOutcome(
        receipt=receipt,
        ledger=ledger,
        audit=[
            _audit(
                ledger=ledger,
                receipt=receipt,
                decision_id=decision_id,
                trigger="confirm",
            )
        ],
    )


async def route_yield(
    *,
    store: LedgerStore,
    ledger: Ledger,
    amount: Decimal,
    policy: Policy,
    yield_rail: Rail | None = None,
    decision_id: str = "",
    at: datetime | None = None,
) -> TransferOutcome:
    """Route money to the yield sleeve, or park it honestly if the partner is down.

    When the yield rail is unavailable the money is not credited to ``yield``.
    It moves to the policy's fallback sleeve and the receipt says ``parked``,
    because a receipt claiming a yield that never posted is a lie the user will
    act on.
    """
    timestamp = at if at is not None else _utcnow()
    amount = money(amount)
    idempotency_key = f"yield:{ledger.user_id}:{amount}:{timestamp.date().isoformat()}"
    prior = ledger.receipt_for(idempotency_key)
    if prior is not None:
        return TransferOutcome(
            receipt=prior.model_copy(update={"idempotent_replay": True}),
            ledger=ledger,
        )

    before = sleeves_snapshot(ledger.sleeves)
    instruction = TransferInstruction(
        user_id=ledger.user_id,
        amount=amount,
        currency=ledger.currency,
        counterparty="yield_partner",
        sleeve="yield",
        idempotency_key=idempotency_key,
        purpose="yield",
    )
    outcome = (
        await yield_rail.execute(instruction)
        if yield_rail is not None
        else RailOutcome(ok=False, error="no yield rail is configured")
    )

    if outcome.ok:
        ledger.move_internal("spendable", "yield", amount)
        status = "executed"
        detail = f"routed {amount} {ledger.currency} to the yield sleeve"
        reasons: list[str] = []
        reference = outcome.reference
    else:
        fallback = policy.yield_fallback
        ledger.move_internal("spendable", fallback, amount)
        status = "parked"
        detail = (
            f"the yield rail was down ({outcome.error}); parked {amount} "
            f"{ledger.currency} in {fallback}. No yield was posted."
        )
        reasons = ["YIELD_RAIL_DOWN"]
        reference = ""

    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status=status,  # type: ignore[arg-type]
        action="yield_route",
        currency=ledger.currency,
        amount=amount,
        counterparty="yield_partner",
        sleeve="yield" if outcome.ok else policy.yield_fallback,
        decision_id=decision_id,
        idempotency_key=idempotency_key,
        reasons=reasons,
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        rail_reference=reference,
        detail=detail,
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    return TransferOutcome(
        receipt=receipt,
        ledger=ledger,
        audit=[
            _audit(
                ledger=ledger,
                receipt=receipt,
                decision_id=decision_id,
                trigger="system",
            )
        ],
    )


async def record_refusal(
    *,
    store: LedgerStore,
    ledger: Ledger,
    action: ProposedAction,
    reasons: list[str],
    decision_id: str,
    at: datetime | None = None,
) -> TransferOutcome:
    """Write a ``rejected`` receipt for an action a decision refused.

    Nothing moved, and saying so is a result the user is owed. Refusals are also
    the only place a limit breach becomes visible in the trail without Hands
    having been asked to execute at all.
    """
    timestamp = at if at is not None else _utcnow()
    idempotency_key = f"refused:{decision_id}:{action.signature()}"
    receipt = _rejected(
        ledger=ledger,
        action=action.type,
        reasons=list(reasons),
        amount=action.amount,
        counterparty=action.counterparty,
        decision_id=decision_id,
        idempotency_key=idempotency_key,
        at=timestamp,
        detail="the decision refused this; nothing moved",
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    return TransferOutcome(
        receipt=receipt,
        ledger=ledger,
        audit=[
            _audit(
                ledger=ledger,
                receipt=receipt,
                decision_id=decision_id,
                trigger="system",
            )
        ],
    )


async def handle_debit(
    *,
    store: LedgerStore,
    ledger: Ledger,
    payment_id: str,
    amount: Decimal,
    reason: str = "",
    at: datetime | None = None,
) -> TransferOutcome:
    """Apply a Go-sent debit or reversal against the spendable sleeve.

    Uses the original credit's idempotency key (``payment_id``), so a reversal
    that arrives after a retried credit replays rather than double-applies. This
    is deliberately not a second inflow with a negative amount: sign-flipping an
    inflow would credit income on one ordering and debit on another. When
    spendable is short the debit fails closed with an audited rejected receipt
    and invents nothing in another sleeve.
    """
    timestamp = at if at is not None else _utcnow()
    amount = money(amount)
    prior = ledger.receipt_for(payment_id)
    if prior is not None:
        return TransferOutcome(
            receipt=prior.model_copy(update={"idempotent_replay": True}),
            ledger=ledger,
        )
    before = sleeves_snapshot(ledger.sleeves)
    reasons: list[str] = []
    if amount <= 0:
        reasons.append("NON_POSITIVE_AMOUNT")
    elif amount > ledger.balance("spendable"):
        reasons.append("INSUFFICIENT_SPENDABLE")
    if reasons:
        receipt = _rejected(
            ledger=ledger,
            action="debit",
            reasons=reasons,
            amount=amount,
            counterparty=reason,
            decision_id="",
            idempotency_key=payment_id,
            at=timestamp,
            detail=f"go debit refused: {';'.join(reasons)}; nothing moved",
        )
        receipt.sleeves_before = before
        receipt.sleeves_after = before
        ledger.remember_receipt(receipt)
        await store.save(ledger)
        return TransferOutcome(
            receipt=receipt,
            ledger=ledger,
            audit=[
                _audit(
                    ledger=ledger,
                    receipt=receipt,
                    decision_id="",
                    trigger="event",
                    detail=f"go debit {payment_id} refused: {reason}",
                )
            ],
        )
    ledger.debit("spendable", amount)
    ledger.record_movement(
        Movement(
            kind="outflow",
            amount=amount,
            sleeve="spendable",
            counterparty=reason,
            category="debit",
            ref=payment_id,
            at=timestamp,
        )
    )
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="executed",
        action="debit",
        currency=ledger.currency,
        amount=amount,
        counterparty=reason,
        sleeve="spendable",
        decision_id="",
        idempotency_key=payment_id,
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        detail=f"go debit {payment_id} applied: {amount} {ledger.currency}",
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    return TransferOutcome(
        receipt=receipt,
        ledger=ledger,
        audit=[
            _audit(
                ledger=ledger,
                receipt=receipt,
                decision_id="",
                trigger="event",
                detail=f"go debit {payment_id}: {reason}",
            )
        ],
    )


handle_reversal = handle_debit


async def move_between_sleeves(
    *,
    store: LedgerStore,
    ledger: Ledger,
    from_sleeve: str,
    to_sleeve: str,
    amount: Decimal,
    policy: Policy,
    action: str,
    decision_id: str = "",
    at: datetime | None = None,
) -> TransferOutcome:
    """Lock or unlock money between the user's own sleeves.

    No rail is involved, because nothing leaves the user. The two guards that
    matter are that nothing may leave a policy-locked sleeve, and that money
    already reserved for rent cannot be unlocked back into spendable.
    """
    timestamp = at if at is not None else _utcnow()
    amount = money(amount)
    idempotency_key = (
        f"{action}:{decision_id}:{amount}" if decision_id else f"{action}:{_id('k')}"
    )
    prior = ledger.receipt_for(idempotency_key)
    if prior is not None:
        return TransferOutcome(
            receipt=prior.model_copy(update={"idempotent_replay": True}),
            ledger=ledger,
        )

    reasons: list[str] = []
    if amount <= 0:
        reasons.append("NON_POSITIVE_AMOUNT")
    if policy.is_locked(from_sleeve):
        reasons.append("LOCKED_SLEEVE")
    if from_sleeve == "locked":
        unlockable = ledger.balance("locked") - ledger.rent_first.reserved
        if amount > unlockable:
            # The rent reserve is not the user's to release.
            reasons.append("RENT_RESERVED")
    if amount > ledger.balance(from_sleeve):
        reasons.append("OVER_BALANCE")
    if amount > policy.max_with_confirm:
        reasons.append("OVER_LIMIT")

    if reasons:
        receipt = _rejected(
            ledger=ledger,
            action=action,
            reasons=reasons,
            amount=amount,
            counterparty="",
            decision_id=decision_id,
            idempotency_key=idempotency_key,
            at=timestamp,
            detail="a precondition refused this move; nothing moved",
        )
        ledger.remember_receipt(receipt)
        await store.save(ledger)
        return TransferOutcome(
            receipt=receipt,
            ledger=ledger,
            audit=[
                _audit(
                    ledger=ledger,
                    receipt=receipt,
                    decision_id=decision_id,
                    trigger="system",
                )
            ],
        )

    before = sleeves_snapshot(ledger.sleeves)
    ledger.move_internal(from_sleeve, to_sleeve, amount)
    if from_sleeve == "locked" and to_sleeve == "spendable":
        released = min(ledger.rent_first.reserved, amount)
        ledger.rent_first = ledger.rent_first.model_copy(
            update={"reserved": ledger.rent_first.reserved - released}
        )
    ledger.record_movement(
        Movement(
            kind="unlock" if from_sleeve == "locked" else "internal",
            amount=amount,
            sleeve=from_sleeve,
            to_sleeve=to_sleeve,
            ref=action,
            at=timestamp,
        )
    )
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="executed",
        action=action,
        currency=ledger.currency,
        amount=amount,
        sleeve=to_sleeve,
        decision_id=decision_id,
        idempotency_key=idempotency_key,
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        detail=f"moved {amount} {ledger.currency} from {from_sleeve} to {to_sleeve}",
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    return TransferOutcome(
        receipt=receipt,
        ledger=ledger,
        audit=[
            _audit(
                ledger=ledger,
                receipt=receipt,
                decision_id=decision_id,
                trigger="confirm",
            )
        ],
    )


# The deterministic parse lives in hands/nl.py (blob ratchet split);
# parse_amount / parse_transfer_utterance are re-exported through the
# import block above so every existing import path keeps working.

__all__ = [
    "GoRail",
    "InMemoryRail",
    "Rail",
    "RailOutcome",
    "TransferInstruction",
    "TransferOutcome",
    "execute_transfer",
    "handle_debit",
    "handle_reversal",
    "move_between_sleeves",
    "parse_amount",
    "parse_transfer_utterance",
    "record_refusal",
    "route_yield",
]

"""Audit-receipt and journal-mark helpers for :mod:`miriam_agent.hands.transfer`.

Split out of ``transfer.py`` (blob-ratchet) as a pure move: the id/clock
utilities, the audit-row and rejected-receipt builders, the decision-verdict
check, and the fail-soft journal mark. ``transfer.py`` re-imports every name,
so existing call sites and import paths are unchanged.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from miriam_agent.hands.audit import AuditRow, Receipt, sleeves_snapshot
from miriam_agent.hands.execution_journal import ExecutionJournal
from miriam_agent.hands.ledger import Ledger

logger = logging.getLogger(__name__)


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


async def _safe_mark(
    journal: ExecutionJournal, to: str, key: str, rail_reference: str = ""
) -> None:
    """Advance the journal lifecycle without ever breaking the money path.

    The journal is the arbiter before the rail call (fail-closed there), but
    after the rail has fired the ledger receipt is the record and a journal
    write failure is logged, never raised: raising here would convert a
    settled movement into an exception the caller might retry.
    """
    try:
        if to == "dispatched":
            await journal.mark_dispatched(key=key, rail_reference=rail_reference)
        elif to == "confirmed":
            await journal.mark_confirmed(key=key, rail_reference=rail_reference)
        else:
            await journal.mark_failed(key=key)
    except Exception as exc:  # noqa: BLE001 - post-rail marks never raise
        logger.error("execution journal mark %s failed for %s: %s", to, key, exc)

"""Layer 1 - HANDS. Inflow detection split and rent-first.

Deterministic code only. This is the module that must keep working when the LLM
is dead, so nothing here imports a model, a JEV client, or a provider.

The split is arithmetic, not judgement:

    spendable = amount * track.spend
    savings   = amount * track.save
    yield     = amount * track.yield

with the rounding remainder assigned to spendable so the parts always reconcile
to the inflow exactly. Then rent-first runs: if the spendable sleeve would end
up short of rent that is still owed, the shortfall is pulled out of spendable
and reserved before the user can touch it.

Splitting is idempotent on the inflow id. The same inflow seen twice produces
one split and two identical receipts, not two splits.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from miriam_agent.hands.audit import AuditRow, Receipt, sleeves_snapshot
from miriam_agent.hands.ledger import (
    Ledger,
    LedgerStore,
    Movement,
    PendingInflow,
    Track,
    money,
)


class SplitOutcome(BaseModel):
    """The committed ledger plus what the split actually did."""

    model_config = ConfigDict(extra="forbid")

    receipt: Receipt
    parts: dict[str, str] = Field(default_factory=dict)
    reserved_for_rent: str = "0"
    idempotent_replay: bool = False


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def split_parts(track: Track, amount: Decimal) -> dict[str, Decimal]:
    """The inflow split, in whole cents, reconciling to ``amount`` exactly.

    The remainder goes to spendable rather than being dropped, so
    ``sum(parts.values()) == amount`` holds for every amount and every track.
    """
    amount = money(amount)
    savings = money(amount * track.save / Decimal("100"))
    yield_ = money(amount * track.yield_ / Decimal("100"))
    spendable = money(amount - savings - yield_)
    return {"spendable": spendable, "savings": savings, "yield": yield_}


def rent_reserve_target(ledger: Ledger, spendable_after: Decimal) -> Decimal:
    """How much of the spendable sleeve must be frozen for rent right now."""
    owed = ledger.rent_first.gap
    if owed <= 0:
        return Decimal("0")
    return money(min(owed, max(Decimal("0"), spendable_after)))


async def split_inflow(
    *,
    store: LedgerStore,
    ledger: Ledger,
    inflow_id: str,
    amount: Decimal,
    source_raw: str = "",
    classified_as: str | None = None,
    at: datetime | None = None,
) -> SplitOutcome:
    """Split one inflow across the sleeves and protect rent. Commits the ledger.

    ``inflow_id`` is the idempotency key. A repeat of the same id returns the
    original receipt and changes nothing at all.
    """
    existing = ledger.receipt_for(inflow_id)
    if existing is not None:
        return SplitOutcome(
            receipt=existing.model_copy(update={"idempotent_replay": True}),
            parts={},
            reserved_for_rent="0",
            idempotent_replay=True,
        )

    before = sleeves_snapshot(ledger.sleeves)
    timestamp = at if at is not None else _utcnow()
    parts = split_parts(ledger.track, amount)

    for sleeve, value in parts.items():
        if value > 0:
            ledger.credit(sleeve, value)
    ledger.record_movement(
        Movement(
            kind="inflow",
            amount=money(amount),
            sleeve="spendable",
            counterparty=source_raw,
            category="inflow",
            ref=inflow_id,
            at=timestamp,
        )
    )

    reserved = rent_reserve_target(ledger, ledger.balance("spendable"))
    if reserved > 0:
        ledger.move_internal("spendable", "locked", reserved)
        ledger.rent_first = ledger.rent_first.model_copy(
            update={"reserved": ledger.rent_first.reserved + reserved}
        )
        ledger.record_movement(
            Movement(
                kind="reserve",
                amount=reserved,
                sleeve="spendable",
                to_sleeve="locked",
                ref="rent_first",
                at=timestamp,
            )
        )

    ledger.pending_inflow = PendingInflow(
        id=inflow_id,
        amount=money(amount),
        source_raw=source_raw,
        classified_as=classified_as,
    )

    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="executed",
        action="inflow_split",
        currency=ledger.currency,
        amount=money(amount),
        counterparty=source_raw,
        sleeve="spendable",
        idempotency_key=inflow_id,
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        detail=(
            f"split {money(amount)} {ledger.currency} on the "
            f"{ledger.track.name} track; {reserved} reserved for rent"
        ),
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)

    return SplitOutcome(
        receipt=receipt,
        parts={name: str(value) for name, value in parts.items()},
        reserved_for_rent=str(reserved),
    )


def audit_row_for_split(
    *, user_id: str, receipt: Receipt, decision_id: str, trigger: str = "event"
) -> AuditRow:
    """The audit row for a committed split, built from its receipt."""
    return AuditRow(
        id=_id("audit"),
        at=receipt.at,
        user_id=user_id,
        action="inflow_split",
        trigger=trigger,  # type: ignore[arg-type]
        amount=receipt.amount,
        currency=receipt.currency,
        sleeve="spendable",
        counterparty=receipt.counterparty,
        sleeves_before=receipt.sleeves_before,
        sleeves_after=receipt.sleeves_after,
        decision_id=decision_id,
        receipt_id=receipt.id,
        idempotency_key=receipt.idempotency_key,
        detail=receipt.detail,
    )


__all__ = [
    "SplitOutcome",
    "audit_row_for_split",
    "rent_reserve_target",
    "split_inflow",
    "split_parts",
]

"""Layer 1 - HANDS. Audit rows and receipts.

Deterministic code only. No LLM, no JEV, no prose.

Every mutation in this package writes an audit row, and every mutation returns a
:class:`Receipt`. The receipt is the record of what actually moved; it is what
Voice narrates and it is what the tests assert on. Storing the narration as the
source of truth for a movement would be the bug this module exists to prevent.

A rejected action still produces a receipt. "Nothing happened" is a result the
user is owed, and it must be as traceable as a movement.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# What a receipt says happened. ``rejected`` always means no balance changed:
# a limit breach, a locked sleeve, or a rail failure all land here rather than
# being silently dropped.
ReceiptStatus = Literal[
    "executed",  # the rail moved it and the ledger reflects it
    "parked",  # the intended rail was down; policy routed it somewhere safe
    "queued",  # waiting on a confirm_id, nothing moved yet
    "noop",  # nothing to do, deliberately
    "rejected",  # refused, nothing moved
]

# Who or what asked for the movement. Recorded so an LLM-originated movement
# would be visible in the trail if one ever appeared.
Trigger = Literal["user", "event", "confirm", "system"]


class AuditRow(BaseModel):
    """One immutable line of the money journal."""

    model_config = ConfigDict(extra="forbid")

    id: str
    at: datetime
    user_id: str
    action: str
    trigger: Trigger = "system"
    amount: Decimal | None = None
    currency: str = ""
    sleeve: str = ""
    counterparty: str = ""
    sleeves_before: dict[str, str] = Field(default_factory=dict)
    sleeves_after: dict[str, str] = Field(default_factory=dict)
    decision_id: str = ""
    receipt_id: str = ""
    idempotency_key: str = ""
    detail: str = ""


class Receipt(BaseModel):
    """What one attempted mutation actually did.

    ``sleeves_before`` and ``sleeves_after`` are both recorded, so a reader can
    verify the movement from the receipt alone without replaying the ledger.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    at: datetime
    status: ReceiptStatus
    action: str
    currency: str
    amount: Decimal | None = None
    counterparty: str = ""
    sleeve: str = ""
    decision_id: str = ""
    idempotency_key: str = ""
    reasons: list[str] = Field(default_factory=list)
    sleeves_before: dict[str, str] = Field(default_factory=dict)
    sleeves_after: dict[str, str] = Field(default_factory=dict)
    rail_reference: str = ""
    detail: str = ""
    # True only when this receipt is being replayed for a repeated
    # idempotency key. The caller learns the original result, not a second one.
    idempotent_replay: bool = False


class AuditLog:
    """Append-only journal. Rows are never edited or removed.

    The journal is in-process plus whatever the store persists alongside the
    ledger; the ledger itself keeps the receipts, so a restart loses no record.
    """

    def __init__(self, rows: list[AuditRow] | None = None) -> None:
        self._rows: list[AuditRow] = list(rows or [])

    def record(self, row: AuditRow) -> AuditRow:
        self._rows.append(row)
        return row

    @property
    def rows(self) -> list[AuditRow]:
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


def sleeves_snapshot(sleeves: dict[str, Decimal]) -> dict[str, str]:
    """A stable, JSON-safe view of balances for an audit row or receipt."""
    return {name: str(amount) for name, amount in sorted(sleeves.items())}


__all__ = [
    "AuditLog",
    "AuditRow",
    "Receipt",
    "ReceiptStatus",
    "Trigger",
    "sleeves_snapshot",
]

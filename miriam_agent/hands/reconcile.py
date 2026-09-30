"""Layer 1 - HANDS. Reconciliation: detect drift between the three money facts.

Three systems each hold part of the truth about a movement:

* the journal (``execution_journal``) -- which keys were claimed, and whether
  the rail was called (``dispatched``) and settled (``confirmed``/``failed``),
* the ledger (``hands/ledger.py``) -- balances, receipts, and the ``processed``
  key map. The single source of truth for money,
* the rail (Go, via ``get_transactions``) -- what actually moved remotely.

This module compares them and reports drift as data. It never moves money,
never edits the ledger, never retries a rail call: every finding is a fact
for an operator (or a future auto-healer with its own authority) to act on.
A reconciler that "fixes" drift by writing is a second writer of a balance,
which is the bug this package exists to prevent.

Findings:

* ``JOURNAL_DISPATCHED_NO_RECEIPT`` -- the journal says the rail was called
  but the ledger has no receipt for the key. Crash between rail and save, or
  a lost ledger write. Reconcile against ``rail_reference`` on Go before any
  retry.
* ``RECEIPT_NO_JOURNAL`` -- the ledger has an executed receipt for a key the
  journal never saw. Movements that predate the journal, or a journal outage
  that was bypassed. Informational for old rows; alarming for new ones.
* ``JOURNAL_CONFIRMED_NO_PROCESSED`` -- journal says confirmed but the
  ledger's ``processed`` map lacks the key. The replay guard would miss on a
  retry: re-record, do not re-fire.
* ``RAIL_UNKNOWN_REFERENCE`` -- the rail has no record of a reference the
  journal/ledger claims is live. The money may not have moved: investigate
  before telling the user it did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DriftFinding:
    """One detected inconsistency between the money facts."""

    kind: str
    idempotency_key: str
    detail: str = ""
    rail_reference: str = ""


@dataclass
class ReconciliationReport:
    """The outcome of one reconciliation pass."""

    at: datetime
    checked: int = 0
    findings: list[DriftFinding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings

    def by_kind(self, kind: str) -> list[DriftFinding]:
        return [f for f in self.findings if f.kind == kind]


class RailHistory(Protocol):
    """The rail side of the comparison: what moved remotely."""

    async def references(self) -> set[str]: ...


async def reconcile(
    *,
    journal_rows: list[dict[str, Any]],
    ledger_receipts: list[Any],
    ledger_processed: dict[str, str],
    rail: RailHistory | None = None,
    at: datetime | None = None,
) -> ReconciliationReport:
    """Compare journal, ledger, and rail facts; report drift, change nothing."""
    from datetime import UTC

    now = at if at is not None else datetime.now(UTC)
    report = ReconciliationReport(at=now)

    receipts_by_key: dict[str, Any] = {}
    for receipt in ledger_receipts:
        key = getattr(receipt, "idempotency_key", "") or ""
        if key and key not in receipts_by_key:
            receipts_by_key[key] = receipt

    rail_refs: set[str] | None = None
    if rail is not None:
        try:
            rail_refs = await rail.references()
        except Exception as exc:  # noqa: BLE001 - rail outage degrades, not fails
            logger.warning("reconciliation: rail history unavailable: %s", exc)
            rail_refs = None

    for row in journal_rows:
        key = str(row.get("idempotency_key", ""))
        if not key:
            continue
        report.checked += 1
        status = str(row.get("status", ""))
        rail_ref = str(row.get("rail_reference", "") or "")
        receipt = receipts_by_key.get(key)

        if status == "dispatched" and receipt is None:
            report.findings.append(
                DriftFinding(
                    kind="JOURNAL_DISPATCHED_NO_RECEIPT",
                    idempotency_key=key,
                    rail_reference=rail_ref,
                    detail=(
                        "the journal says the rail was called but the ledger "
                        "has no receipt; reconcile against the rail reference "
                        "before any retry"
                    ),
                )
            )
        if status == "confirmed" and key not in ledger_processed:
            report.findings.append(
                DriftFinding(
                    kind="JOURNAL_CONFIRMED_NO_PROCESSED",
                    idempotency_key=key,
                    rail_reference=rail_ref,
                    detail=(
                        "the journal says settled but the ledger's replay guard "
                        "lacks the key; re-record the key, do not re-fire"
                    ),
                )
            )
        if (
            rail_refs is not None
            and rail_ref
            and status in ("dispatched", "confirmed")
            and rail_ref not in rail_refs
        ):
            report.findings.append(
                DriftFinding(
                    kind="RAIL_UNKNOWN_REFERENCE",
                    idempotency_key=key,
                    rail_reference=rail_ref,
                    detail=(
                        "the rail has no record of this reference; the money "
                        "may not have moved"
                    ),
                )
            )

    journal_keys = {str(r.get("idempotency_key", "")) for r in journal_rows}
    for key, receipt in receipts_by_key.items():
        if getattr(receipt, "status", "") != "executed":
            continue
        if key not in journal_keys:
            report.findings.append(
                DriftFinding(
                    kind="RECEIPT_NO_JOURNAL",
                    idempotency_key=key,
                    rail_reference=str(getattr(receipt, "rail_reference", "") or ""),
                    detail=(
                        "the ledger shows an executed movement the journal "
                        "never saw (predates the journal, or a bypassed outage)"
                    ),
                )
            )

    _ = Decimal  # re-export guard: amounts compare as strings across stores
    return report


__all__ = [
    "DriftFinding",
    "RailHistory",
    "ReconciliationReport",
    "reconcile",
]

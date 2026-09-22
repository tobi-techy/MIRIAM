"""Layer 1 - HANDS. Deterministic money. No LLM, no JEV side effects.

What lives here:

* :mod:`~miriam_agent.hands.ledger` - balances by sleeve, and the only three
  functions in the process that write one.
* :mod:`~miriam_agent.hands.split` - the inflow split and rent-first reserve.
* :mod:`~miriam_agent.hands.transfer` - rails, transfers, yield routing, and the
  deterministic parse of a user's sentence into a structured action.
* :mod:`~miriam_agent.hands.limits` - policy ceilings and the affordable cap.
* :mod:`~miriam_agent.hands.state` - builds STATE, the only object Voice sees.
* :mod:`~miriam_agent.hands.audit` - audit rows and receipts.

The boundaries this package enforces:

* All money movement lives in this package. Nothing outside it writes a balance.
* Voice has zero write methods here. Judgment has zero write methods here.
* A movement needs a typed decision and a passing limit check, in that order,
  and this package re-checks both rather than trusting the caller.
* A missing required field yields ``insufficient_state``. Nobody guesses.
"""

from __future__ import annotations

from miriam_agent.hands.audit import (
    AuditLog,
    AuditRow,
    Receipt,
    ReceiptStatus,
    sleeves_snapshot,
)
from miriam_agent.hands.ledger import (
    SLEEVES,
    Bill,
    Challenge,
    InMemoryLedgerStore,
    Last30d,
    Ledger,
    LedgerConflictError,
    LedgerError,
    LedgerStore,
    LedgerUnavailable,
    Movement,
    PendingInflow,
    PendingInvest,
    RedisLedgerStore,
    RentFirst,
    Track,
    money,
    new_ledger,
)
from miriam_agent.hands.limits import (
    DAILY_CAP_DEFAULT,
    Policy,
    affordable_cap,
    check_amount,
    daily_usage,
    evaluate_limits,
    free_after_obligations,
    needs_confirm,
    reserved_outbound,
    settled_outbound_today,
)
from miriam_agent.hands.split import SplitOutcome, split_inflow, split_parts
from miriam_agent.hands.state import (
    Execution,
    HandlerState,
    InsufficientState,
    ProposedAction,
    build_state,
    require_complete,
    with_decision,
    with_execution,
)
from miriam_agent.hands.transfer import (
    GoRail,
    InMemoryRail,
    Rail,
    RailOutcome,
    TransferInstruction,
    TransferOutcome,
    execute_transfer,
    handle_debit,
    handle_reversal,
    move_between_sleeves,
    parse_amount,
    parse_transfer_utterance,
    route_yield,
)

__all__ = [
    "SLEEVES",
    "DAILY_CAP_DEFAULT",
    "AuditLog",
    "AuditRow",
    "Bill",
    "Challenge",
    "Execution",
    "GoRail",
    "HandlerState",
    "InMemoryLedgerStore",
    "InMemoryRail",
    "InsufficientState",
    "Last30d",
    "Ledger",
    "LedgerConflictError",
    "LedgerError",
    "LedgerStore",
    "LedgerUnavailable",
    "Movement",
    "PendingInflow",
    "PendingInvest",
    "Policy",
    "ProposedAction",
    "Rail",
    "RailOutcome",
    "Receipt",
    "ReceiptStatus",
    "RedisLedgerStore",
    "RentFirst",
    "SplitOutcome",
    "Track",
    "TransferInstruction",
    "TransferOutcome",
    "affordable_cap",
    "build_state",
    "check_amount",
    "daily_usage",
    "evaluate_limits",
    "execute_transfer",
    "free_after_obligations",
    "handle_debit",
    "handle_reversal",
    "money",
    "move_between_sleeves",
    "needs_confirm",
    "new_ledger",
    "parse_amount",
    "parse_transfer_utterance",
    "require_complete",
    "reserved_outbound",
    "route_yield",
    "settled_outbound_today",
    "sleeves_snapshot",
    "split_inflow",
    "split_parts",
    "with_decision",
    "with_execution",
]

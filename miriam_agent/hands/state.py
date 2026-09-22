"""Layer 1 - HANDS. Builds STATE.

Deterministic code only. No LLM, no JEV.

STATE is the single object the other two layers see. Hands builds it from the
ledger and the policy; Judgment writes exactly one field (``decision``) and
Hands writes one more (``execution``). Voice reads it and writes nothing.

Because Voice is only allowed to say numbers that are in STATE, STATE is also
the vocabulary the Voice clamp checks against. That is why it is built from the
ledger rather than assembled by whoever happens to be talking.

A required field that is missing does not become a guess. It sets
``status="insufficient_state"`` and lists what was missing, and Judgment's hard
rules turn that into an ask. Nobody invents the number.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from miriam_agent.hands.audit import Receipt
from miriam_agent.hands.ledger import (
    SLEEVES,
    Bill,
    Ledger,
    PendingInflow,
    RentFirst,
    Track,
    money,
)
from miriam_agent.hands.limits import Policy

ActionType = Literal[
    "transfer", "purchase", "lock", "unlock", "yield", "internal_move", "none"
]

# How many recent receipts travel in STATE. Enough for the user to be told what
# just happened, not enough to bloat the payload.
RECEIPT_LIMIT = 5

REQUIRED_FIELDS: tuple[str, ...] = (
    "user_id",
    "currency",
    "sleeves",
    "track",
    "policy",
)


class ProposedAction(BaseModel):
    """A structured action, parsed by code. Never a chat sentence.

    ``source`` records where it came from. Only ``user`` actions are ever
    executed: prose produced by a model cannot become a movement, which is the
    difference between Miriam and a chatbot with a wallet.
    """

    model_config = ConfigDict(extra="forbid")

    type: ActionType = "none"
    amount: Decimal | None = None
    counterparty: str = ""
    sleeve: str = "spendable"
    source: Literal["user", "voice", "event", "system"] = "user"
    raw: str = ""

    def signature(self) -> str:
        """A stable identity for this action, used for idempotency keys."""
        return f"{self.type}:{self.counterparty}:{self.amount}:{self.sleeve}"


class Execution(BaseModel):
    """What Hands actually did, filled after a commit."""

    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    status: str
    action: str
    amount: Decimal | None = None
    counterparty: str = ""
    sleeve: str = ""
    rail_reference: str = ""
    idempotent_replay: bool = False
    at: datetime


class HandlerState(BaseModel):
    """The object Voice is allowed to read, and the only one it can read."""

    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    user_id: str
    currency: str
    sleeves: dict[str, Decimal]
    track: Track
    bills_upcoming: list[Bill] = Field(default_factory=list)
    rent_first: RentFirst = Field(default_factory=RentFirst)
    last_30d: dict[str, Any] = Field(default_factory=dict)
    pending_inflow: PendingInflow | None = None
    proposed_action: ProposedAction | None = None
    policy: dict[str, Any] = Field(default_factory=dict)
    last_receipts: list[Receipt] = Field(default_factory=list)
    # Written by Judgment. Never by Voice, never by Hands.
    decision: dict[str, Any] | None = None
    # Written by Hands after a commit.
    execution: Execution | None = None
    status: Literal["ok", "insufficient_state"] = "ok"
    missing: list[str] = Field(default_factory=list)

    @property
    def spendable(self) -> Decimal:
        return self.sleeves.get("spendable", Decimal("0"))

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dump. This is what a caller ships to a model."""
        return self.model_dump(mode="json")


class InsufficientState(Exception):
    """Raised when Hands is asked to execute on a STATE that is incomplete."""

    def __init__(self, missing: list[str]):
        super().__init__("insufficient_state: missing " + ", ".join(missing))
        self.missing = missing


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _missing(ledger: Ledger, policy: Policy | None) -> list[str]:
    """Which required fields the ledger cannot answer."""
    missing: list[str] = []
    if not ledger.user_id:
        missing.append("user_id")
    if not ledger.currency:
        missing.append("currency")
    for name in SLEEVES:
        if name not in ledger.sleeves:
            missing.append(f"sleeves.{name}")
    if ledger.track is None:
        missing.append("track")
    if policy is None:
        missing.append("policy")
    return missing


def build_state(
    *,
    ledger: Ledger,
    policy: Policy | None,
    proposed_action: ProposedAction | None = None,
    decision: dict[str, Any] | None = None,
    execution: Execution | None = None,
    now: datetime | None = None,
) -> HandlerState:
    """Assemble STATE from the ledger. Missing requirements are reported, not filled.

    This never raises for a thin ledger. A missing field is a fact about the
    state, and the layers above are required to handle it by asking.
    """
    missing = _missing(ledger, policy)
    at = now or _utcnow()
    state = HandlerState(
        as_of=at,
        user_id=ledger.user_id,
        currency=ledger.currency,
        sleeves={
            name: money(ledger.sleeves.get(name, Decimal("0"))) for name in SLEEVES
        },
        track=ledger.track,
        bills_upcoming=list(ledger.bills_upcoming),
        rent_first=ledger.rent_first,
        last_30d=ledger.window(at=at).model_dump(mode="json"),
        pending_inflow=ledger.pending_inflow,
        proposed_action=proposed_action,
        policy=policy.to_state() if policy is not None else {},
        last_receipts=ledger.receipts[-RECEIPT_LIMIT:],
        decision=decision,
        execution=execution,
        status="insufficient_state" if missing else "ok",
        missing=missing,
    )
    return state


def require_complete(state: HandlerState) -> None:
    """Raise unless STATE has everything a movement needs. Used before executing."""
    if state.status != "ok" or state.missing:
        raise InsufficientState(list(state.missing))


def with_decision(state: HandlerState, decision: dict[str, Any]) -> HandlerState:
    """Attach a decision without touching anything else in STATE."""
    return state.model_copy(update={"decision": decision})


def with_execution(state: HandlerState, execution: Execution) -> HandlerState:
    """Attach an execution without touching anything else in STATE."""
    return state.model_copy(update={"execution": execution})


__all__ = [
    "REQUIRED_FIELDS",
    "Execution",
    "HandlerState",
    "InsufficientState",
    "ProposedAction",
    "build_state",
    "require_complete",
    "with_decision",
    "with_execution",
]

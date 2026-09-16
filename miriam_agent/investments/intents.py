"""Investment intent lifecycle (spec §17-§18).

"invest 20k" is not an instruction, it is an *intent*. Nothing moves until the
user has seen an exact summary and confirmed it, and the policy engine has
approved that exact action. This module owns that lifecycle:

    DRAFT -> AWAITING_CONFIRMATION -> EXECUTING -> SETTLED
                                             |-> FAILED
                                             |-> CANCELLED

Safety invariants (all enforced here, none left to the caller):

  - The LLM can *create* an intent and *summarize* it. It cannot execute one.
  - Execution requires ``confirmed=True`` on the intent whose ``action_hash``
    matches the payload the user actually saw.
  - Every intent carries an idempotency key, so a double-tap, a retry or a
    replayed confirmation settles one order, not two.
  - Provider events are absorbed idempotently; out-of-order events are ignored
    (a FAILED arriving after SETTLED changes nothing).
  - Every transition is appended to an audit trail that lives on the intent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from miriam_agent.core.exceptions import ValidationError
from miriam_agent.financial.eligibility import (
    Decision,
    evaluate_investment_action,
)
from miriam_agent.investments.provider import InvestmentProvider

logger = logging.getLogger(__name__)

DRAFT = "DRAFT"
AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
EXECUTING = "EXECUTING"
SETTLED = "SETTLED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

# Terminal states. A terminal intent is finished: no confirmation, no retry and
# no late provider event can move it again.
TERMINAL = frozenset({SETTLED, FAILED, CANCELLED})

# How many times a failing order may be re-driven before the user is told to
# start again. Bounded so a flapping provider cannot spin.
_MAX_PROVIDER_RETRIES = 3


def action_hash(payload: dict[str, Any]) -> str:
    """A stable hash of the exact action the user is confirming.

    The summary a user reads and the order the provider receives must be the
    same action -- this hash is the binding between them, so a payload that
    changed between "here is what I will do" and "yes, do it" is refused.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class InvestmentIntent(BaseModel):
    """One proposed investment, and everything that happened to it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    idempotency_key: str
    amount: float
    currency: str = "USD"
    symbol: str | None = None
    status: str = DRAFT
    action_hash: str = ""
    confirmation_token: str | None = None
    provider_ref: str | None = None
    decision: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    events: list[dict[str, Any]] = Field(default_factory=list)
    provider_retries: int = 0
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def audit(self, event: str, **details: Any) -> None:
        """Append one audit entry. The trail is append-only by construction."""
        self.events.append(
            {"event": event, "at": time.time(), **details}
        )
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class IntentStore:
    """Process-local intent store.

    The durable record of an executed order lives with the provider (Glider's
    executions and audit log) and in ``audit_logs``. This store only holds the
    *conversation's* open intents, so a restart losing an unconfirmed intent
    costs the user a re-confirmation -- never money, never a silent second
    order.
    """

    def __init__(self) -> None:
        self._intents: dict[str, InvestmentIntent] = {}

    def save(self, intent: InvestmentIntent) -> None:
        self._intents[intent.id] = intent

    def get(self, intent_id: str) -> InvestmentIntent | None:
        return self._intents.get(intent_id)

    def find_by_idempotency_key(
        self, user_id: str, idempotency_key: str
    ) -> InvestmentIntent | None:
        for intent in self._intents.values():
            if intent.user_id == user_id and intent.idempotency_key == idempotency_key:
                return intent
        return None

    def clear(self) -> None:
        self._intents.clear()


class InvestmentIntentService:
    """The gate between a conversational "invest 20k" and the market."""

    def __init__(
        self,
        provider: InvestmentProvider,
        store: IntentStore | None = None,
    ) -> None:
        self._provider = provider
        self._store = store or IntentStore()

    # -- steps 1-8: resolve, check, summarize (never execute) ---------------

    async def create_intent(
        self,
        *,
        user_id: str,
        token: str,
        amount: float,
        symbol: str | None = None,
        limits: dict[str, Any] | None = None,
        kyc_verified: bool | None = None,
        jurisdiction: str | None = None,
        supported_jurisdictions: tuple[str, ...] | None = None,
        idempotency_key: str | None = None,
    ) -> InvestmentIntent:
        """Resolve the user, balance and policy; stage the intent for
        confirmation. Returns the intent carrying the user-facing ``summary``
        and the ``action_hash`` the confirmation must match.

        Raises :class:`~miriam_agent.core.exceptions.ValidationError` when
        policy refuses the action -- the caller relays that as a refusal, not
        as an order that failed.
        """
        key = idempotency_key or action_hash(
            {
                "user_id": user_id,
                "amount": amount,
                "symbol": symbol,
                "at": int(time.time() // 60),
            }
        )
        existing = self._store.find_by_idempotency_key(user_id, key)
        if existing is not None:
            # A double-tap or a retried request returns the same intent: one
            # order, one confirmation.
            return existing

        balance = await self._provider.get_balance(token)
        available = self._available_from(balance)

        asset = None
        if symbol:
            try:
                asset = await self._provider.get_asset(token, symbol, symbol=symbol)
            except Exception:
                asset = {"symbol": symbol, "tradable": False}

        decision = evaluate_investment_action(
            amount,
            limits,
            kyc_verified=kyc_verified,
            jurisdiction=jurisdiction,
            supported_jurisdictions=supported_jurisdictions,
            asset=asset,
            available_balance=available,
            idempotency_key=key,
        )

        payload = {
            "user_id": user_id,
            "amount": amount,
            "currency": "USD",
            "symbol": symbol,
        }
        intent = InvestmentIntent(
            id=f"int_{key[:16]}",
            user_id=user_id,
            idempotency_key=key,
            amount=amount,
            symbol=symbol,
            action_hash=action_hash(payload),
            decision=decision.to_dict(),
        )

        if not decision.allowed:
            intent.status = CANCELLED
            intent.audit(
                "policy_refused", reasons=decision.reasons, checks=decision.checks
            )
            self._store.save(intent)
            raise ValidationError(
                "investment action refused by policy: " + "; ".join(decision.reasons)
            )

        intent.status = AWAITING_CONFIRMATION
        intent.summary = self._summarize(intent, available)
        intent.audit(
            "staged_for_confirmation",
            amount=amount,
            symbol=symbol,
            available_balance=available,
            checks=decision.checks,
        )
        self._store.save(intent)
        return intent
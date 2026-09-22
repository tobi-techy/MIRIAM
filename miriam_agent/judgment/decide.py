"""Layer 2 - JUDGMENT. Writes exactly one field: STATE.decision.

The flow for one turn:

1. ask JEV the seven questions about STATE (:mod:`~miriam_agent.judgment.jev_client`),
2. run the hard overrides in code (:mod:`~miriam_agent.judgment.rules`),
3. return a :class:`~miriam_agent.judgment.schema.Decision`.

Nothing here writes a balance, calls a rail, or produces user-facing prose. The
``judge`` callable is injectable so a test can drive exact answers without a
network, and so a flaky JEV can be simulated to prove the fail-closed path.

The affordable cap is read from Hands, not computed here. Judgment decides
*whether* a smaller amount is acceptable; the number itself comes from the
ledger.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from miriam_agent.hands.ledger import Ledger
from miriam_agent.hands.limits import Policy, affordable_cap
from miriam_agent.hands.state import HandlerState
from miriam_agent.judgment.jev_client import JudgeUnavailable, evaluate_money
from miriam_agent.judgment.rules import apply_rules
from miriam_agent.judgment.schema import (
    INFLOW_CLASSES,
    INTENT_TYPES,
    Decision,
    InflowClass,
    IntentType,
    MoneyJudgment,
)

logger = logging.getLogger(__name__)

Judge = Callable[[HandlerState, str], Awaitable[MoneyJudgment | None]]


async def _default_judge(state: HandlerState, utterance: str) -> MoneyJudgment | None:
    """Ask JEV, converting an unavailable judge into ``None`` for the rules."""
    try:
        return await evaluate_money(state, utterance)
    except JudgeUnavailable as exc:
        logger.warning("JEV unavailable, failing closed: %s", exc)
        return None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def cap_for(*, state: HandlerState, ledger: Ledger, policy: Policy) -> Decimal:
    """The largest safe movement right now, computed by Hands."""
    proposed = state.proposed_action
    if proposed is not None and proposed.type == "invest":
        # Invest draws on the stash, so the smaller offer is capped by it.
        return ledger.balance("savings")
    return affordable_cap(
        policy=policy,
        spendable=ledger.balance("spendable"),
        rent_required=ledger.rent_first.required,
        rent_reserved=ledger.rent_first.reserved,
    )


def _inflow_class(value: str) -> InflowClass:
    """Narrow a JEV label onto the inflow vocabulary, defaulting to unknown.

    A label the catalog does not define is not a classification, so it does not
    get to become one.
    """
    return cast(InflowClass, value) if value in INFLOW_CLASSES else "unknown"


def _intent_type(value: str) -> IntentType:
    """Narrow a JEV label onto the intent vocabulary, defaulting to status."""
    return cast(IntentType, value) if value in INTENT_TYPES else "status"


async def decide(
    *,
    state: HandlerState,
    ledger: Ledger,
    policy: Policy,
    utterance: str = "",
    judge: Judge | None = None,
    cap: Decimal | None = None,
    at: datetime | None = None,
) -> Decision:
    """Produce the typed decision for one turn."""
    timestamp = at if at is not None else _utcnow()
    try:
        judgment = await (judge or _default_judge)(state, utterance)
    except Exception as exc:  # noqa: BLE001 - any judge failure must fail closed
        # A judge that raises is a judge that did not answer. The rules layer
        # turns ``None`` into an ask, so this is the one place a transport bug, a
        # timeout, or a bad injection all become the same safe outcome.
        logger.warning("judge raised, failing closed: %s", exc)
        judgment = None

    if cap is None:
        cap = cap_for(state=state, ledger=ledger, policy=policy)

    outcome = apply_rules(
        state=state,
        judgment=judgment,
        ledger=ledger,
        policy=policy,
        cap=cap,
    )

    return Decision(
        id=f"dec_{uuid.uuid4().hex[:12]}",
        at=timestamp,
        inflow_class=_inflow_class(judgment.inflow_class.choice if judgment else ""),
        inflow_conf=round(judgment.inflow_class.confidence, 3) if judgment else 0.0,
        intent_type=_intent_type(judgment.intent_type.choice if judgment else ""),
        intent_conf=round(judgment.intent_type.confidence, 3) if judgment else 0.0,
        affordability=round(judgment.affordability.score, 3) if judgment else 0.0,
        policy_violation=bool(judgment and judgment.policy_violation.noul >= 0.6),
        reversibility=bool(judgment and judgment.reversibility.noul >= 0.6),
        next_mode=outcome.next_mode,
        action_choice=outcome.action_choice,
        suggested_amount=outcome.suggested_amount,
        reasons=outcome.reasons,
        degraded=outcome.degraded,
    )


__all__ = ["Judge", "cap_for", "decide"]

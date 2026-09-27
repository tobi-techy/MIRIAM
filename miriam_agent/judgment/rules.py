"""Layer 2 - JUDGMENT. Deterministic money rules.

JEV answers only the two semantic questions code cannot compute: what kind of
inflow arrived, and what the user's turn asks for. This module turns those
answers plus the exact ledger and policy state into the one typed decision Hands
reads.

There is deliberately no model-supplied authorization label here. Affordability,
policy violations, reversibility, next mode, action choice, and the suggested
amount are all computed in code. A model cannot widen a limit, invent a smaller
amount, or turn a no into a yes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from miriam_agent.hands.ledger import Ledger
from miriam_agent.hands.limits import Policy, free_after_obligations
from miriam_agent.hands.state import HandlerState, ProposedAction
from miriam_agent.judgment.answers import choice_margin
from miriam_agent.judgment.schema import (
    THRESHOLDS,
    ActionChoice,
    MoneyJudgment,
    NextMode,
)

# Actions that only move money between sleeves the user owns. They are treated
# as reversible even when they are above the normal reversible band.
_INTERNAL_ACTIONS = frozenset({"internal_move", "lock", "unlock"})


class Reason(StrEnum):
    """The enum codes that ride on a decision. Copy may quote them, nothing else."""

    RENT_SHORT = "RENT_SHORT"
    OVER_LIMIT = "OVER_LIMIT"
    OVER_AUTO = "OVER_AUTO"
    OVER_BALANCE = "OVER_BALANCE"
    LOCKED_SLEEVE = "LOCKED_SLEEVE"
    NON_POSITIVE_AMOUNT = "NON_POSITIVE_AMOUNT"
    MISSING_AMOUNT = "MISSING_AMOUNT"
    UNKNOWN_INFLOW = "UNKNOWN_INFLOW"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    INSUFFICIENT_STATE = "INSUFFICIENT_STATE"
    JEV_UNAVAILABLE = "JEV_UNAVAILABLE"
    ADVICE_ONLY = "ADVICE_ONLY"
    CONFIRM_WITHOUT_ID = "CONFIRM_WITHOUT_ID"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    USER_CANCELLED = "USER_CANCELLED"
    NEEDS_AN_ANSWER = "NEEDS_AN_ANSWER"
    REVERSIBLE = "REVERSIBLE"
    AFFORDABLE = "AFFORDABLE"
    MATCHES_POLICY = "MATCHES_POLICY"


@dataclass
class RuleOutcome:
    """What the rules decided, before it is wrapped in a Decision."""

    next_mode: NextMode
    action_choice: ActionChoice
    reasons: list[str] = field(default_factory=list)
    suggested_amount: Decimal | None = None
    degraded: bool = False
    # These are computed facts, not model votes. They remain on the decision
    # contract for clients that display the reasoning behind the verdict.
    affordability: float = 0.0
    policy_violation: bool = False
    reversibility: bool = False

    def add(self, *reasons: Reason) -> None:
        for reason in reasons:
            if reason.value not in self.reasons:
                self.reasons.append(reason.value)


def _free_for(proposed: ProposedAction, ledger: Ledger) -> Decimal:
    """Spendable-or-stash money the action is actually allowed to draw on."""
    if proposed.type == "invest":
        return ledger.balance("savings")
    return free_after_obligations(
        ledger.balance("spendable"),
        ledger.rent_first.required,
        ledger.rent_first.reserved,
    )


def _affordability(amount: Decimal | None, free: Decimal) -> float:
    """A deterministic 0..1 view of how comfortably the amount fits.

    ``1.0`` means the amount fits inside the available money. A smaller value
    means only that fraction is safe. This replaces the old model-scored
    affordability question; the actual authorization still comes from the hard
    amount checks below.
    """
    if amount is None or amount <= 0:
        return 0.0
    if free <= 0:
        return 0.0
    return round(min(1.0, float(free / amount)), 3)


def _policy_violation(
    proposed: ProposedAction,
    amount: Decimal | None,
    free: Decimal,
    policy: Policy,
) -> bool:
    """Whether the proposed action breaks a deterministic policy limit."""
    if amount is None or amount <= 0:
        return True
    if policy.is_locked(proposed.sleeve):
        return True
    if amount > policy.max_with_confirm:
        return True
    if proposed.type in ("onramp", "offramp"):
        # These do not draw on the spendable sleeve; the amount ceiling still
        # applies, but rent-first does not.
        return False
    return amount > free


def _reversibility(
    proposed: ProposedAction, amount: Decimal | None, policy: Policy
) -> bool:
    """Whether this action is reversible under the configured band."""
    if amount is None or amount <= 0:
        return False
    if proposed.type in _INTERNAL_ACTIONS:
        return True
    return amount <= policy.reversible_under


def _apply_allowed_action(
    outcome: RuleOutcome,
    *,
    proposed: ProposedAction,
    amount: Decimal,
    free: Decimal,
    policy: Policy,
    cap: Decimal,
    rent_short: bool = False,
    force_confirmation: bool = False,
) -> None:
    """Fill the allow/ask/deny branch once the hard checks passed."""
    if outcome.reversibility:
        outcome.add(Reason.REVERSIBLE)

    if amount > free and proposed.type not in ("onramp", "offramp"):
        if rent_short:
            outcome.add(Reason.RENT_SHORT)
        else:
            outcome.add(Reason.OVER_BALANCE)
        outcome.next_mode = "ask"
        outcome.action_choice = "deny"
        return

    if amount > policy.max_with_confirm:
        outcome.add(Reason.OVER_LIMIT)
        outcome.next_mode = "ask"
        outcome.action_choice = "deny"
        return

    if amount > policy.max_auto:
        outcome.add(Reason.OVER_AUTO)
        outcome.next_mode = "ask"
        if cap > 0:
            outcome.action_choice = "allow_smaller"
            outcome.suggested_amount = cap
        else:
            # Every movement must be tapped. Offer the full amount for a
            # challenge instead of leaving the user with nothing to approve.
            outcome.action_choice = "allow"
        if force_confirmation:
            outcome.add(Reason.CONFIRMATION_REQUIRED)
        return

    outcome.add(Reason.MATCHES_POLICY, Reason.AFFORDABLE)
    if force_confirmation:
        outcome.add(Reason.CONFIRMATION_REQUIRED)
        outcome.next_mode = "ask"
        outcome.action_choice = "allow"
        return
    outcome.next_mode = "act"
    outcome.action_choice = "allow"


def apply_rules(
    *,
    state: HandlerState,
    judgment: MoneyJudgment | None,
    ledger: Ledger,
    policy: Policy,
    cap: Decimal,
) -> RuleOutcome:
    """Compose the semantic JEV answers with deterministic ledger rules."""
    outcome = RuleOutcome(next_mode="ask", action_choice="none")

    # -- an incomplete STATE is never acted on --------------------------
    if state.status != "ok":
        outcome.add(Reason.INSUFFICIENT_STATE)
        return outcome

    proposed = state.proposed_action
    amount = proposed.amount if proposed is not None else None

    # -- JEV unreachable: fail closed ------------------------------------
    if judgment is None:
        outcome.degraded = True
        outcome.add(Reason.JEV_UNAVAILABLE, Reason.LOW_CONFIDENCE)
        if proposed is None:
            outcome.next_mode = "stay_quiet"
            outcome.action_choice = "classify_only"
        else:
            outcome.next_mode = "ask"
            outcome.action_choice = "defer"
        return outcome

    inflow_conf = judgment.inflow_class.confidence
    intent = judgment.intent_type.choice
    intent_conf = judgment.intent_type.confidence

    if proposed and proposed.type in ("onramp", "offramp"):
        # Funding legs do not draw on spendable; their affordability is the
        # transaction ceiling, not the user's current spendable balance.
        outcome.affordability = (
            1.0 if amount is not None and 0 < amount <= policy.max_with_confirm else 0.0
        )
    else:
        outcome.affordability = (
            _affordability(amount, _free_for(proposed, ledger)) if proposed else 0.0
        )
    outcome.policy_violation = (
        _policy_violation(proposed, amount, _free_for(proposed, ledger), policy)
        if proposed
        else False
    )
    outcome.reversibility = (
        _reversibility(proposed, amount, policy) if proposed else False
    )

    # -- classification confidence --------------------------------------
    if state.pending_inflow is not None and (
        judgment.inflow_class.choice == "unknown"
        or inflow_conf < THRESHOLDS.inflow_min_confidence
    ):
        outcome.add(Reason.UNKNOWN_INFLOW)
        if proposed is None:
            # The split already ran in Hands. Not knowing what the money was
            # does not stop the arithmetic; it stops the claim about it.
            outcome.next_mode = "stay_quiet"
            outcome.action_choice = "classify_only"
            return outcome

    if (
        intent_conf < THRESHOLDS.intent_min_confidence
        or choice_margin(judgment.intent_type) < THRESHOLDS.intent_min_margin
    ):
        outcome.add(Reason.LOW_CONFIDENCE)
        outcome.next_mode = "ask"
        outcome.action_choice = "defer"
        return outcome

    # -- a confirm must carry an id, never be read out of a chat word ---
    if intent in ("confirm", "cancel") and proposed is None:
        outcome.add(Reason.CONFIRM_WITHOUT_ID)
        outcome.next_mode = "act"
        outcome.action_choice = "none"
        return outcome

    if intent == "cancel":
        outcome.add(Reason.USER_CANCELLED)
        outcome.next_mode = "ask"
        outcome.action_choice = "deny"
        return outcome

    if proposed is None:
        if state.pending_inflow is not None:
            outcome.next_mode = "stay_quiet"
            outcome.action_choice = "classify_only"
            return outcome
        outcome.add(Reason.NEEDS_AN_ANSWER)
        outcome.next_mode = "ask"
        outcome.action_choice = "none"
        return outcome

    # -- advice is answered, never executed ------------------------------
    if intent == "advice" or proposed.type == "purchase":
        outcome.add(Reason.ADVICE_ONLY)
        outcome.next_mode = "ask"
        outcome.action_choice = "none"
        return outcome

    # Only an order (or an explicit confirmation that still needs a challenge)
    # can authorize a movement. A status/smalltalk turn that accidentally
    # parses an action must ask, not move money.
    if intent not in ("order", "confirm"):
        outcome.add(Reason.NEEDS_AN_ANSWER)
        outcome.next_mode = "ask"
        outcome.action_choice = "none"
        return outcome

    if amount is None:
        outcome.add(Reason.MISSING_AMOUNT)
        outcome.next_mode = "ask"
        outcome.action_choice = "defer"
        return outcome

    if amount <= 0:
        outcome.add(Reason.NON_POSITIVE_AMOUNT)
        outcome.next_mode = "ask"
        outcome.action_choice = "deny"
        return outcome

    if policy.is_locked(proposed.sleeve):
        outcome.add(Reason.LOCKED_SLEEVE)
        outcome.next_mode = "ask"
        outcome.action_choice = "deny"
        return outcome

    # Funding legs are app-staged: funds-in needs no spendable balance and
    # funds-out is authorised by the tap, so the ordinary auto-ceiling and
    # spendable cap do not apply. The absolute transaction ceiling still does.
    if proposed.type in ("onramp", "offramp"):
        if amount > policy.max_with_confirm:
            outcome.add(Reason.OVER_LIMIT)
            outcome.next_mode = "ask"
            outcome.action_choice = "deny"
        else:
            outcome.add(Reason.MATCHES_POLICY)
            outcome.next_mode = "ask"
            outcome.action_choice = "allow"
        return outcome

    free = _free_for(proposed, ledger)
    _apply_allowed_action(
        outcome,
        proposed=proposed,
        amount=amount,
        free=free,
        policy=policy,
        cap=cap,
        rent_short=ledger.rent_first.gap > 0,
        force_confirmation=intent == "confirm",
    )
    return outcome


def empty_judgment_reasons() -> list[str]:
    """The reason codes for a turn where JEV had nothing to answer."""
    return [Reason.JEV_UNAVAILABLE.value]


__all__ = ["Reason", "RuleOutcome", "apply_rules", "empty_judgment_reasons"]

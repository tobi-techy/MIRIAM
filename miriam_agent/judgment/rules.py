"""Layer 2 - JUDGMENT. The hard rules that override JEV.

These are if-statements, not model output. JEV proposes; this module decides.
Every override is here for one reason: a model's answer must never be the last
word on whether money moves.

The overrides, in precedence order:

* an incomplete STATE is an ask, never an act
* JEV unreachable, or an answer below the confidence bar, is an ask
* a locked sleeve is a deny
* rent that would go unpaid is a deny
* an amount above the confirm ceiling is a deny
* an amount above the act-without-asking ceiling is an ask, with the smaller
  amount Hands computed offered as an alternative
* an advice question is answered, never executed
* a bare "yes" with no confirm id is not a confirmation

Reason codes are the only vocabulary Voice is allowed to quote. It may repeat
them; it may not change them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import cast

from miriam_agent.hands.ledger import Ledger
from miriam_agent.hands.limits import Policy, free_after_obligations
from miriam_agent.hands.state import HandlerState
from miriam_agent.judgment.schema import (
    THRESHOLDS,
    ActionChoice,
    MoneyJudgment,
    NextMode,
)


class Reason(StrEnum):
    """The enum codes that ride on a decision. Copy may quote them, nothing else."""

    RENT_SHORT = "RENT_SHORT"
    TRACK_BREAK = "TRACK_BREAK"
    OVER_LIMIT = "OVER_LIMIT"
    OVER_AUTO = "OVER_AUTO"
    OVER_BALANCE = "OVER_BALANCE"
    LOCKED_SLEEVE = "LOCKED_SLEEVE"
    UNKNOWN_INFLOW = "UNKNOWN_INFLOW"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    LOW_AFFORDABILITY = "LOW_AFFORDABILITY"
    INSUFFICIENT_STATE = "INSUFFICIENT_STATE"
    JEV_UNAVAILABLE = "JEV_UNAVAILABLE"
    ADVICE_ONLY = "ADVICE_ONLY"
    CONFIRM_WITHOUT_ID = "CONFIRM_WITHOUT_ID"
    NEEDS_AN_ANSWER = "NEEDS_AN_ANSWER"
    REVERSIBLE = "REVERSIBLE"
    AFFORDABLE = "AFFORDABLE"
    MATCHES_POLICY = "MATCHES_POLICY"


# Stricter wins. A rule may only ever make a decision safer, never looser: if
# JEV says deny and a rule would allow, the deny stands.
_MODE_RANK: dict[str, int] = {"act": 0, "ask": 1, "stay_quiet": 2}
_ACTION_RANK: dict[str, int] = {
    "allow": 0,
    "allow_smaller": 1,
    "classify_only": 2,
    "none": 3,
    "defer": 4,
    "deny": 5,
}


def _stricter_mode(*modes: NextMode) -> NextMode:
    return max(modes, key=lambda mode: _MODE_RANK.get(mode, 1))


def _stricter_action(*actions: ActionChoice) -> ActionChoice:
    return max(actions, key=lambda action: _ACTION_RANK.get(action, 3))


def _mode(value: str) -> NextMode:
    """Narrow a JEV choice onto the mode vocabulary, defaulting to the safe one.

    A label the catalog does not define is either a bug or schema drift, and the
    answer to both is to ask rather than to act.
    """
    return cast(NextMode, value) if value in _MODE_RANK else "ask"


def _action(value: str) -> ActionChoice:
    """Narrow a JEV choice onto the action vocabulary, defaulting to defer."""
    return cast(ActionChoice, value) if value in _ACTION_RANK else "defer"


@dataclass
class RuleOutcome:
    """What the rules decided, before it is wrapped in a Decision."""

    next_mode: NextMode
    action_choice: ActionChoice
    reasons: list[str] = field(default_factory=list)
    suggested_amount: Decimal | None = None
    degraded: bool = False

    def add(self, *reasons: Reason) -> None:
        for reason in reasons:
            if reason.value not in self.reasons:
                self.reasons.append(reason.value)


def apply_rules(
    *,
    state: HandlerState,
    judgment: MoneyJudgment | None,
    ledger: Ledger,
    policy: Policy,
    cap: Decimal,
) -> RuleOutcome:
    """Compose JEV's answers with the hard overrides into one decision.

    ``judgment`` is ``None`` when JEV could not be reached. That path fails
    closed: an order becomes an ask, and a turn with nothing proposed stays
    quiet and moves nothing.
    """
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

    mode = _mode(judgment.next_mode.choice)
    action = _action(judgment.action_choice.choice)
    inflow_conf = judgment.inflow_class.confidence
    intent = judgment.intent_type.choice
    intent_conf = judgment.intent_type.confidence
    afford = judgment.affordability.score
    violates = judgment.policy_violation.noul >= THRESHOLDS.noul_true
    reversible = judgment.reversibility.noul >= THRESHOLDS.noul_true

    outcome.next_mode = mode
    outcome.action_choice = action

    if reversible:
        outcome.add(Reason.REVERSIBLE)

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

    if intent_conf < THRESHOLDS.intent_min_confidence:
        outcome.add(Reason.LOW_CONFIDENCE)
        outcome.next_mode = _stricter_mode(outcome.next_mode, "ask")

    # -- a confirm must carry an id, never be read out of a chat word ---
    if intent in ("confirm", "cancel") and proposed is None:
        outcome.add(Reason.CONFIRM_WITHOUT_ID)
        outcome.next_mode = "act"
        outcome.action_choice = "none"
        return outcome

    if proposed is None:
        return outcome

    # -- advice is answered, never executed ------------------------------
    if intent == "advice" or proposed.type == "purchase":
        outcome.add(Reason.ADVICE_ONLY)
        outcome.next_mode = _stricter_mode(outcome.next_mode, "ask")
        outcome.action_choice = "none"
        if afford < THRESHOLDS.affordability_min:
            outcome.add(Reason.LOW_AFFORDABILITY)
        return outcome

    if intent != "order" and proposed.type != "transfer":
        # NGN <-> crypto funding rides the same "order" intent as sleeve
        # orders: a buy/sell with an amount is an instruction, not advice.
        if proposed.type not in ("onramp", "offramp", "bill"):
            outcome.action_choice = _stricter_action(outcome.action_choice, "none")
            return outcome

    # Funds-IN needs no spendable balance and funds-OUT is app-only staged,
    # so neither is gated on free spendable here. The tap still authorises.
    # Unknown JEV labels fail CLOSED: "do nothing" stays "do nothing"
    # (defer + ask), never loosened into an approval prompt.
    if proposed.type in ("onramp", "offramp"):
        outcome.next_mode = _stricter_mode(outcome.next_mode, "ask")
        if outcome.action_choice not in ("allow", "allow_smaller", "deny", "defer"):
            outcome.action_choice = "defer"
        return outcome

    if amount is None:
        outcome.action_choice = "none"
        return outcome

    # -- the money rules --------------------------------------------------
    # Invest draws on the stash (savings) sleeve, never the spend pot, so its
    # affordability is measured there. Everything else is measured against
    # free spendable.
    if proposed.type == "invest":
        free = ledger.balance("savings")
    else:
        free = free_after_obligations(
            ledger.balance("spendable"),
            ledger.rent_first.required,
            ledger.rent_first.reserved,
        )

    if policy.is_locked(proposed.sleeve):
        outcome.add(Reason.LOCKED_SLEEVE)
        outcome.next_mode = "ask"
        outcome.action_choice = "deny"

    if violates:
        outcome.add(Reason.TRACK_BREAK)
        outcome.next_mode = _stricter_mode(outcome.next_mode, "ask")

    if afford < THRESHOLDS.affordability_min:
        outcome.add(Reason.LOW_AFFORDABILITY)
        outcome.next_mode = _stricter_mode(outcome.next_mode, "ask")

    # Order matters: the most specific refusal is the one the user is told.
    if amount > free:
        if ledger.rent_first.gap > 0:
            outcome.add(Reason.RENT_SHORT)
        else:
            outcome.add(Reason.OVER_BALANCE)
        outcome.next_mode = "ask"
        outcome.action_choice = _stricter_action(outcome.action_choice, "deny")
    elif amount > policy.max_with_confirm:
        outcome.add(Reason.OVER_LIMIT)
        outcome.next_mode = "ask"
        outcome.action_choice = _stricter_action(outcome.action_choice, "deny")
    elif amount > policy.max_auto:
        # Above what may be done silently, but affordable. The smaller version
        # is offered, and its number comes from Hands, never from the model.
        outcome.add(Reason.OVER_AUTO)
        outcome.next_mode = "ask"
        if cap > 0:
            outcome.action_choice = _stricter_action(
                outcome.action_choice, "allow_smaller"
            )
            outcome.suggested_amount = cap
        else:
            # Nothing may be done silently at all, because the act-without-asking
            # ceiling is zero. That is a hold, not a refusal: every movement has to
            # be tapped, so offer the full amount for confirmation. Deferring here
            # would leave the user nothing to tap and money they could never send,
            # which is a different product from "confirm first".
            outcome.action_choice = _stricter_action(outcome.action_choice, "allow")
    elif outcome.action_choice in ("allow", "allow_smaller"):
        outcome.add(Reason.MATCHES_POLICY, Reason.AFFORDABLE)

    if outcome.next_mode == "act" and outcome.action_choice == "allow_smaller":
        # Never silently shrink without asking.
        outcome.next_mode = "ask"

    if outcome.next_mode == "stay_quiet" and proposed.type in (
        "transfer",
        "lock",
        "unlock",
    ):
        # A concrete instruction is never dropped in silence. If the answer is
        # "say nothing", that is not an answer to "send 200k to Femi".
        outcome.add(Reason.NEEDS_AN_ANSWER)
        outcome.next_mode = "ask"
        if outcome.action_choice in ("none", "classify_only"):
            outcome.action_choice = "defer"

    return outcome


def empty_judgment_reasons() -> list[str]:
    """The reason codes for a turn where JEV had nothing to answer."""
    return [Reason.JEV_UNAVAILABLE.value]


__all__ = ["Reason", "RuleOutcome", "apply_rules", "empty_judgment_reasons"]

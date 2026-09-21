"""Layer 2 - JUDGMENT. The typed questions, answers and decision.

No prose lives in this module and no money moves in it. JEV answers a fixed set
of questions about STATE; :mod:`~miriam_agent.judgment.rules` turns those
answers into a decision using if-statements. The model never picks the answer to
"may this move", and it is never asked to write the decision.

The seven questions are the whole surface:

    inflow_class      choice   salary | invoice | gift | refund | transfer_in | unknown
    intent_type       choice   order | advice | status | smalltalk | confirm | cancel
    affordability     score    0..1
    policy_violation  noul     breaks 70/30, rent-first, lock or limit
    reversibility     noul     undoable under policy.reversible_under
    next_mode         choice   act | ask | stay_quiet
    action_choice     choice   allow | allow_smaller | deny | defer | none

``suggested_amount`` is deliberately *not* a question. Judgment picks the label
``allow_smaller``; Hands computes the number from the ledger. A model that can
write a figure into a movement is a model that can move money.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field
from typesafe_sdk import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    SystemOneResponse,
)

from miriam_agent.judgment.questions import Catalog

InflowClass = Literal["salary", "invoice", "gift", "refund", "transfer_in", "unknown"]
IntentType = Literal["order", "advice", "status", "smalltalk", "confirm", "cancel"]
NextMode = Literal["act", "ask", "stay_quiet"]
ActionChoice = Literal[
    "allow", "allow_smaller", "deny", "defer", "classify_only", "none"
]

# Derived from the Literals above so a new label cannot be added to one and
# forgotten in the other. Used to narrow a JEV answer onto the vocabulary.
INFLOW_CLASSES: tuple[str, ...] = get_args(InflowClass)
INTENT_TYPES: tuple[str, ...] = get_args(IntentType)

MONEY_CATALOG_VERSION = "1"


class MoneyThresholds(BaseModel):
    """Every number the money rules branch on, in one place.

    Changing one of these is a product change: it decides when Miriam asks
    instead of acting, so it should be reviewed like one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Below these, the answer is not good enough to act on and Miriam asks.
    inflow_min_confidence: float = 0.60
    intent_min_confidence: float = 0.62
    affordability_min: float = 0.35
    policy_violation_high: float = 0.60
    # Above this, a JEV answer is treated as a positive.
    noul_true: float = 0.60


THRESHOLDS = MoneyThresholds()


class MoneyJudgment(SystemOneResponse):
    """The typed answers for one STATE. No prose, no amounts, no verbs."""

    inflow_class: ChoiceAnswer
    intent_type: ChoiceAnswer
    affordability: ScoreAnswer
    policy_violation: NoulAnswer
    reversibility: NoulAnswer
    next_mode: ChoiceAnswer
    action_choice: ChoiceAnswer


_INFLOW_CRITERIA = {
    "salary": {
        "what": "Pay for work, arriving on a rhythm -- payroll, a retainer, a wage.",
        "examples": ["monthly salary", "payroll credit", "retainer from one client"],
    },
    "invoice": {
        "what": "Payment against work billed to a specific client or job.",
        "examples": ["invoice 0042 paid", "client settled the project balance"],
    },
    "gift": {
        "what": "Money given with nothing owed in return.",
        "examples": ["mum sent me money", "birthday gift"],
    },
    "refund": {
        "what": "Money returned for something already paid for.",
        "examples": ["refund from the airline", "reversal of a failed transfer"],
    },
    "transfer_in": {
        "what": "The user moving their own money into this account.",
        "examples": ["from my other bank", "moved my savings here"],
    },
    "unknown": {
        "what": "The source cannot be established from the text or the history.",
        "examples": ["a credit with no label", "an unlabelled alert"],
    },
}

_INTENT_CRITERIA = {
    "order": {
        "what": "An instruction to move money or change money state now.",
        "examples": ["send 200k to Femi", "lock 50k", "unlock my savings"],
    },
    "advice": {
        "what": "A question about whether something is wise or affordable, which "
        "is answered rather than executed.",
        "examples": ["can I buy this phone for 95k", "should I move this to savings"],
    },
    "status": {
        "what": "A request for the current numbers or what happened recently.",
        "examples": ["what is my balance", "did the split run"],
    },
    "smalltalk": {
        "what": "Greeting or chit-chat with no money request.",
        "examples": ["hey", "how are you"],
    },
    "confirm": {
        "what": "Assent to an action that was already proposed and has an id.",
        "examples": ["yes do it", "confirm"],
    },
    "cancel": {
        "what": "Withdrawal of an action that was already proposed.",
        "examples": ["no", "cancel that", "forget it"],
    },
}

_MODE_CRITERIA = {
    "act": {
        "what": "The request is clear, affordable and permitted, so the action "
        "should proceed if it is authorised.",
    },
    "ask": {
        "what": "Something is unclear or unsafe: the user should be asked a "
        "question, or asked to confirm an id.",
    },
    "stay_quiet": {
        "what": "Nothing needs doing and nothing needs saying.",
    },
}

_ACTION_CRITERIA = {
    "allow": {
        "what": "The proposed action may proceed exactly as proposed.",
    },
    "allow_smaller": {
        "what": "The action may proceed only at a smaller amount that policy "
        "permits. Judgment picks this label; code computes the amount.",
    },
    "deny": {
        "what": "The action must not happen.",
    },
    "defer": {
        "what": "The action cannot be decided yet; ask the user before it moves.",
    },
    "classify_only": {
        "what": "There is nothing to authorise; classify the event and stop.",
    },
    "none": {
        "what": "No action was proposed at all.",
    },
}

MONEY_QUESTIONS: dict[str, Any] = {
    "inflow_class": Choice(
        instructions=(
            "Classify `pending_inflow.source_raw` using `last_30d` and "
            "`last_receipts` as evidence. If the source cannot be established, "
            "answer unknown rather than guessing."
        ),
        criteria=_INFLOW_CRITERIA,
    ),
    "intent_type": Choice(
        instructions="What does `turn_text` ask for, given the whole of STATE?",
        criteria=_INTENT_CRITERIA,
    ),
    "affordability": Score(
        instructions=(
            "Can the user do `proposed_action` without breaking rent-first, the "
            "`track` ratios, or a locked sleeve? 0 means it cannot be done "
            "safely at all, 1 means it is trivially affordable."
        ),
        criteria=[
            "Cannot be done without breaking rent or a locked sleeve",
            "Only a small fraction of what was asked is safe",
            "About half of what was asked is safe",
            "Affordable with a real but survivable squeeze",
            "Clearly affordable, nothing important is disturbed",
        ],
    ),
    "policy_violation": Noul(
        instructions=(
            "Does `proposed_action` break the `track` ratios, rent-first, a "
            "locked sleeve, or a limit in `policy`?"
        ),
        criteria={
            "true": (
                "The action would leave rent unpaid, move money out of a locked "
                "sleeve, exceed max_with_confirm, or take from the savings or "
                "yield shares rather than spendable."
            ),
            "false": (
                "The action fits inside spendable money that is not reserved for "
                "rent, and stays within the policy limits."
            ),
        },
    ),
    "reversibility": Noul(
        instructions=(
            "Is `proposed_action` reversible under policy.reversible_under, "
            "meaning it could be undone or was small enough to recover from?"
        ),
        criteria={
            "true": (
                "The action is small relative to the reversible band, or it only "
                "moves money between the user's own sleeves."
            ),
            "false": (
                "The action sends money to a third party or is too large to "
                "recover under the reversible band."
            ),
        },
    ),
    "next_mode": Choice(
        instructions=(
            "What should happen next? Answer act only when the request is clear "
            "and the numbers support it; ask when anything is unclear or unsafe."
        ),
        criteria=_MODE_CRITERIA,
    ),
    "action_choice": Choice(
        instructions=(
            "If there is a `proposed_action`, should it proceed? Choose the "
            "strictest honest answer: allow, allow_smaller, deny, or defer. Use "
            "none when no action was proposed."
        ),
        criteria=_ACTION_CRITERIA,
    ),
}


MONEY = Catalog(
    name="money",
    version=MONEY_CATALOG_VERSION,
    questions=MONEY_QUESTIONS,
    response_model=MoneyJudgment,
)


class Decision(BaseModel):
    """The typed decision. This is what Hands reads and Voice may quote."""

    model_config = ConfigDict(extra="forbid")

    id: str
    at: datetime
    inflow_class: InflowClass = "unknown"
    inflow_conf: float = 0.0
    intent_type: IntentType = "status"
    intent_conf: float = 0.0
    affordability: float = 0.0
    policy_violation: bool = False
    reversibility: bool = False
    next_mode: NextMode = "ask"
    action_choice: ActionChoice = "none"
    suggested_amount: Decimal | None = None
    # Enum codes only. Voice may quote these; it may not change them.
    reasons: list[str] = Field(default_factory=list)
    # Set when the decision is "ask" and a confirm_id was created for it.
    confirm_id: str = ""
    # True when JEV could not be reached and the rules failed closed.
    degraded: bool = False


__all__ = [
    "MONEY",
    "MONEY_CATALOG_VERSION",
    "MONEY_QUESTIONS",
    "THRESHOLDS",
    "ActionChoice",
    "Decision",
    "INFLOW_CLASSES",
    "INTENT_TYPES",
    "InflowClass",
    "IntentType",
    "MoneyJudgment",
    "MoneyThresholds",
    "NextMode",
]

"""Layer 2 - JUDGMENT. The typed questions, answers and decision.

No prose lives in this module and no money moves in it. JEV answers a fixed set
of questions about STATE; :mod:`~miriam_agent.judgment.rules` turns those
answers into a decision using if-statements. The model never picks the answer to
"may this move", and it is never asked to write the decision.

The model answers only the two semantic questions code cannot compute:

    inflow_class      choice   salary | invoice | gift | refund | transfer_in | unknown
    intent_type       choice   order | advice | status | smalltalk | confirm | cancel

Everything else -- affordability, policy violations, reversibility, next mode,
action choice, and suggested amount -- is computed deterministically from
STATE, the ledger, and the policy. The model never votes on whether money moves.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field
from typesafe_sdk import Choice, ChoiceAnswer, SystemOneResponse

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

MONEY_CATALOG_VERSION = "2"


class MoneyThresholds(BaseModel):
    """Every number the money rules branch on, in one place.

    Changing one of these is a product change: it decides when Miriam asks
    instead of acting, so it should be reviewed like one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Below these, the semantic answer is not good enough to act on and Miriam
    # asks instead of guessing.
    inflow_min_confidence: float = 0.60
    intent_min_confidence: float = 0.62
    intent_min_margin: float = 0.15


THRESHOLDS = MoneyThresholds()


class MoneyJudgment(SystemOneResponse):
    """The semantic answers for one STATE. No prose, no amounts, no verbs."""

    inflow_class: ChoiceAnswer
    intent_type: ChoiceAnswer


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

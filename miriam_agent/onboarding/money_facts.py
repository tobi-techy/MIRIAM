"""Money-fact absorption and structured reads for onboarding.

Extracted from ``miriam_agent.onboarding.service`` as a pure move for the
blob-ratchet split: money-intent detection, the reserved fact keys, and the
helpers that merge learned facts onto the state's first-class and structured
fields and hold amount facts to the grounded-number rule.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from miriam_agent.onboarding import driver
from miriam_agent.onboarding.contracts import GoalMeta, MoneyMomentMeta
from miriam_agent.onboarding.gap_questions import _lower
from miriam_agent.onboarding.quality import EvalMeta, evaluate_reply
from miriam_agent.onboarding.state import OnboardingState

logger = logging.getLogger(__name__)


# Clear money-action intents that bypass the interview so a first message like
# "send 5k to Tola" or "what's my balance" is honored immediately instead.
_ACTION_INTENT = re.compile(
    r"\b(send|transfer|pay|withdraw|invest|buy|sell|swap|exchange|balance)\b",
    re.IGNORECASE,
)

# Mid-flow money detection.  The first-message gate (_ACTION_INTENT) is
# deliberately broad -- there is no flow to protect.  Later, the interview,
# statement and consent answers must never be hijacked, so a request only
# counts as a money move when it is a concrete, now-directed action: an
# execution verb followed (in the near window) by an amount or a concrete
# money beneficiary (a person, a bill, an account, an instrument) -- or a
# balance read.  "send it" (a statement answer) and "pay attention to X"
# never trip it.
_MONEY_EXECUTE_VERB = re.compile(
    r"\b(send|transfer|pay|withdraw|invest|buy|sell|swap|exchange|deposit|"
    r"put|move)\b",
    re.IGNORECASE,
)
_MONEY_BENEFICIARY = re.compile(
    r"\b(mom|mum|mother|dad|father|parent|family|brother|sister|friend|"
    r"him|her|them|landlord|shop|car|house|rent|bill|bills|account|card|"
    r"bank|debt|btc|bitcoin|eth|ethereum|usdc|sol|etf|stock|stocks|"
    r"fund|funds|tuition|school)\b",
    re.IGNORECASE,
)
# "pay off / pay down X" is a debt-reduction goal, not a do-it-now transfer.
_MONEY_DEBT_REFRAME = re.compile(r"\bpay\s+(off|down)\b", re.IGNORECASE)
_MONEY_BALANCE_READ = re.compile(r"\bbalance\b", re.IGNORECASE)
# Habitual/scheduled/future phrasing ("I pay the rent every month", "I'll buy
# etf tomorrow") is the user describing their life, not instructing a transfer
# right now -- so a beneficiary match in that framing never hijacks the chat.
_HABIT_OR_FUTURE = re.compile(
    r"\b(every\s+\w+|each\s+\w+|monthly|weekly|biweekly|yearly|always|usually|"
    r"normally|sometimes|tomorrow|next\s+\w+|on\s+the\s+(first|1st|"
    r"[0-9]+(?:st|nd|rd|th)))\b",
    re.IGNORECASE,
)


# A question about the user's OWN data. The onboarding conductor has no tools,
# so a request it cannot answer must never be absorbed into the interview:
# chat-first users ask for real numbers constantly, and a swallowed request
# reads as the product being broken. Requiring BOTH an interrogative opener and
# a data noun keeps the interview's own questions ("what do you spend on?") and
# the user's narrative answers ("I spend too much on food") in the flow.
_DATA_READ_ASK = re.compile(
    r"\b(what|how much|how many|show|list|tell me|do i|did i|have i|can i|"
    r"where|when|which|any)\b",
    re.IGNORECASE,
)
_DATA_DOMAIN = re.compile(
    r"\b(transaction|transactions|spend|spends|spending|spent|purchase|"
    r"purchases|payment|payments|bill|bills|subscription|subscriptions|"
    r"portfolio|investment|investments|position|positions|income|salary|"
    r"paid|earn|earnings?|balance|statement|card|cards|savings?|net worth|"
    r"report|fee|fees|charge|charges|money|cash|funds?|afford)\b",
    re.IGNORECASE,
)


def _asks_for_own_data(text: str) -> bool:
    """True when the user is asking a question about their own money data.

    The onboarding conductor has no tool access, so a question it cannot answer
    must be handed to the general agent rather than folded into the interview --
    answering it and then resuming the flow is the whole point of chat-first
    onboarding.
    """
    t = _lower(text)
    return bool(_DATA_READ_ASK.search(t) and _DATA_DOMAIN.search(t))


def _wants_money_action(text: str) -> bool:
    """True when the user is asking to move money *now* (or read a balance) --
    the one thing the onboarding flow must never swallow, so it hands off to
    the general agent with its own safety net instead of folding the request
    into the plan chat.

    Polar without being reckless: a bare "pay" or "invest" (a goal or a
    thanks) is not a move; "send 500 to mom" and "what's my balance" are.
    Amounts asked for live ("send 500") always count; a beneficiary match only
    counts when the request is not habitual/scheduled/future phrasing.
    """
    t = _lower(text)
    if _MONEY_DEBT_REFRAME.search(t):
        return False
    if _MONEY_BALANCE_READ.search(t):
        return True
    m = _MONEY_EXECUTE_VERB.search(t)
    if not m:
        return False
    tail = t[m.end() : m.end() + 24]
    if any(ch.isdigit() for ch in tail):
        return True
    if _HABIT_OR_FUTURE.search(tail) or _HABIT_OR_FUTURE.search(t):
        return False
    return _MONEY_BENEFICIARY.search(tail) is not None


# Free-form fact keys the agent reserves for the two first-class fields. When a
# fact carries one of these, the service lifts it onto the state's dedicated
# field so the plan and the prompt always see the money moment and the goal.
_MONEY_MOMENT_KEYS = frozenset(
    {"money_moment", "money_moment_description", "the_money_moment", "whats_bothering"}
)
_GOAL_KEYS = frozenset(
    {
        "goal",
        "concrete_goal",
        "the_goal",
        "rich_life",
        "desired_life",
        "money_goal",
        "goal_detail",
    }
)

# spec §6/§7: reserved keys that carry the structured reads of the money moment
# and the goal. Each is lifted off the free-form facts onto the structured
# fields (emotion/suspected problem/confidence, target date/cost/priority).
_MONEY_MOMENT_META_KEYS = {
    "money_moment_emotion": "emotion",
    "money_moment_suspected_problem": "suspected_problem",
    "money_moment_confidence": "confidence",
}
_GOAL_META_KEYS = {
    "goal_target_date": "target_date",
    "goal_estimated_cost": "estimated_cost",
    "goal_priority": "priority",
    "goal_funding_status": "funding_status",
}

# spec §22/§29: quiet conversation-state signals the agent may report. They are
# lifted into conversation_state (the money script is remembered, never shown).
_SENTIMENT_KEYS = frozenset({"sentiment", "user_sentiment"})
_MONEY_SCRIPT_KEYS = frozenset({"money_script", "script"})

# Fact keys that hold a quantity of money (or runway), and are therefore held
# to the grounded-number rule. Reserved meta keys are deliberately absent: a
# confidence score or a goal target date is the model's own read, not a claim
# about what the user said.
_MONEY_AMOUNT_FACT_KEYS = frozenset(
    {
        "cashflow",
        "income",
        "income_amount",
        "fixed_costs",
        "rent",
        "obligations",
        "savings",
        "debt",
        "expenses",
        "spend",
        "spending",
        "budget",
        "salary",
        "runway",
    }
)

# Structured values are short labels, never long prose.
_META_VALUE_MAX = 64


def dimension_for(key: str) -> str:
    """Map a free-form fact label to a broad memory dimension (for the
    memory store only; the plan builder reads the raw text)."""
    k = key.casefold()
    if "goal" in k or "rich" in k or "life" in k:
        return "goal"
    if "moment" in k or "bother" in k or "worry" in k:
        return "liquidity"
    if "income" in k or "cash" in k or "earn" in k:
        return "cashflow"
    if "oblig" in k or "family" in k or "dependent" in k or "support" in k:
        return "obligations"
    if "debt" in k or "loan" in k or "credit" in k or "owe" in k:
        return "debt"
    if "spend" in k or "leak" in k or "behavior" in k or "discipline" in k:
        return "behavior"
    if "runway" in k or "short" in k or "buffer" in k or "savings" in k:
        return "liquidity"
    if "hand" in k or "auto" in k or "involv" in k:
        return "behavior"
    return "onboarding"


def lift_first_class(state: OnboardingState) -> None:
    """Promote the money moment and the goal out of the free-form fact set
    onto their first-class fields (only when they are not set yet, so the
    first articulation wins and later refinements keep refining it)."""
    for key in list(state.learned):
        if key.casefold() in _MONEY_MOMENT_KEYS and not state.money_moment:
            state.money_moment = state.learned.pop(key)
        elif key.casefold() in _GOAL_KEYS and not state.goal:
            state.goal = state.learned.pop(key)


def meta_float(value: str) -> float | None:
    """The money-moment confidence is the one numeric structured value.
    Anything unparseable is not a confidence, so it is dropped rather than
    stored as a junk string (which would fail the typed contract)."""
    try:
        return float(value[:_META_VALUE_MAX])
    except (TypeError, ValueError):
        return None


def meta_string(value: str) -> str:
    return value[:_META_VALUE_MAX]


def lift_meta(state: OnboardingState) -> None:
    """spec §6/§7/§22/§29: lift the reserved meta fact keys onto the
    structured fields (money-moment read, goal read, sentiment, money
    script) and off the free-form fact set, so they never pollute the plan
    text. The agent's first read wins."""
    for key in list(state.learned):
        k = key.casefold()
        if k == "money_moment_confidence":
            # Unparseable confidence (e.g. the LLM emitting "high"): the
            # key is consumed but nothing junk is stored.
            parsed = meta_float(state.learned.pop(key))
            if parsed is not None:
                state.money_moment_meta["confidence"] = parsed
        elif k in _MONEY_MOMENT_META_KEYS:
            state.money_moment_meta[_MONEY_MOMENT_META_KEYS[k]] = meta_string(
                state.learned.pop(key)
            )
        elif k in _GOAL_META_KEYS:
            state.goal_meta[_GOAL_META_KEYS[k]] = meta_string(state.learned.pop(key))
        elif k in _SENTIMENT_KEYS and not state.conversation_state.get(
            "user_sentiment"
        ):
            state.conversation_state["user_sentiment"] = meta_string(
                state.learned.pop(key)
            )
        elif k in _MONEY_SCRIPT_KEYS:
            state.conversation_state["money_script"] = meta_string(
                state.learned.pop(key)
            )
    validate_meta(state)


def sanitized_meta(raw: dict[str, Any], model: type[BaseModel]) -> dict[str, Any]:
    """Validate a meta dict field-by-field so one corrupt value (a string
    where the contract wants a float, an out-of-range emotion) sanitizes
    itself instead of either (a) failing whole-model validation and keeping
    the raw bad dict in Redis, or (b) nuking the valid fields beside it."""
    cleaned: dict[str, Any] = {}
    for name in model.model_fields:
        if name not in raw:
            continue
        value = raw[name]
        if value is None or value == "":
            continue
        try:
            model.model_validate({name: value})
        except ValidationError:
            logger.warning(
                "meta field '%s' failed contract validation; dropping it",
                name,
            )
            continue
        cleaned[name] = value
    try:
        return model.model_validate(cleaned).model_dump(
            exclude_none=True, exclude_defaults=True
        )
    except ValidationError:
        return {}


def validate_meta(state: OnboardingState) -> None:
    """Typed at the write boundary: the structured reads persist as the
    validated meta dumps, defaults dropped so empty reads stay {}."""
    state.money_moment_meta = sanitized_meta(state.money_moment_meta, MoneyMomentMeta)
    state.goal_meta = sanitized_meta(state.goal_meta, GoalMeta)


def grounding_text(state: OnboardingState, extra: str = "") -> str:
    """The texts every number in a reply may legally appear in: the user's
    own words (money moment, goal, statement summary, learned facts,
    adjustments) plus any extra context (the current message, or the
    deterministic plan). A figure in a reply that matches none of these is
    an invented number."""
    parts: list[str] = []
    if state.money_moment:
        parts.append(state.money_moment)
    if state.goal:
        parts.append(state.goal)
    if state.document_summary:
        parts.append(state.document_summary)
    for value in state.learned.values():
        parts.append(value)
    if state.adjustments:
        parts.extend(state.adjustments)
    if extra:
        parts.append(extra)
    return " ".join(parts)


def fact_violations(
    state: OnboardingState,
    outcome: driver.DriverOutcome,
    *,
    grounded_extra: str,
) -> list[str]:
    """R10 for a money-amount fact whose value reports a figure nobody said.

    The reply is linted, but the *facts* are persisted to state and memory
    and later read back as what the user told her. A figure the model parks
    in a fact beside a clean reply would otherwise resurface as ground
    truth on the next turn, so facts get the same grounded-number rule.

    Only amount facts are checked. The reserved meta fields (confidence,
    sentiment, target dates, estimated goals, the money script) are the
    model's own structured reads, and they legitimately carry numbers the
    user never spoke -- "confidence 0.72", "target 2027". Treating those as
    invented figures would refuse almost every interview turn.
    """
    ground = grounding_text(state, extra=grounded_extra)
    violations: list[str] = []
    for key, value in (outcome.facts or {}).items():
        if key.casefold() not in _MONEY_AMOUNT_FACT_KEYS:
            continue
        if not isinstance(value, str) or not value:
            continue
        if "R10" in evaluate_reply(value, EvalMeta(grounded=ground)):
            violations.append("R10")
    return violations

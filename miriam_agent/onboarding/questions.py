"""The onboarding question bank: the canonical 7-dimension vocabulary.

The LLM-led conductor (``driver.py``) turns each track (core + follow-ups)
into the taxonomy it classifies the user's words into (``PROBE_HINTS`` and
``canonicalize`` keep the classification honest). The same bank drives the
deterministic fallback interview (``next_question``) that runs whenever the
LLM is unreachable, so signup never stalls and the plan always speaks the
exact vocabulary ``plan.py`` understands.

The interview follows a 7-dimension Financial State Model (cash flow,
liquidity, obligations, debt, behavior, confidence, goal). A small number of
high-information signals are always asked; follow-ups are only asked when an
answer gives a reason (never on a fixed script), and at most
``ONBOARDING_MAX_FOLLOWUPS`` of them fire per fallback interview so signup
never feels like a form.

Copy stays in Miriam's voice: calm, concrete, never accusatory, and it asks
about behavior rather than self-assessed financial literacy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Number of triggered follow-ups shown before moving to the statement step.
# Overridable via settings.ONBOARDING_MAX_FOLLOWUPS.
MAX_FOLLOWUPS = 3


@dataclass(frozen=True)
class Question:
    """One interview question, rendered as an iMessage poll."""

    id: str
    dimension: str
    prompt: str
    options: tuple[str, ...]
    # Follow-ups that may be asked after this core question. Empty for follow-ups.
    enables: tuple[str, ...] = ()


CORE = (
    Question(
        id="money_feelings",
        dimension="confidence",
        prompt=(
            "Before I can be useful, a few quick ones. "
            "How is money feeling for you these days?"
        ),
        options=(
            "Calm",
            "Mostly fine",
            "Unpredictable",
            "Stressful",
            "Honestly, a mess",
        ),
    ),
    Question(
        id="income_predictability",
        dimension="cashflow",
        prompt="How predictable is your income?",
        options=(
            "Very predictable",
            "Mostly predictable",
            "Varies seasonally",
            "All over the place",
            "I don't have a steady one",
        ),
    ),
    Question(
        id="shortage_cause",
        dimension="cashflow",
        prompt="When money runs low, what is usually behind it?",
        options=(
            "It just doesn't stretch",
            "The timing, it comes in late",
            "I spend more than I plan",
            "I support other people",
            "Honestly, not sure",
        ),
    ),
    Question(
        id="liquidity_runway",
        dimension="liquidity",
        prompt="If your income stopped today, how long could you keep going?",
        options=(
            "A few days",
            "Maybe a month",
            "Three to six months",
            "Six months or more",
        ),
    ),
    Question(
        id="dependents",
        dimension="obligations",
        prompt="Who else leans on your income?",
        options=(
            "Just me",
            "Partner and kids",
            "Extended family",
            "I support others regularly",
        ),
    ),
    Question(
        id="goal_direction",
        dimension="goal",
        prompt="What do you most want your money to help you do?",
        options=(
            "Build wealth",
            "Get my life organized",
            "Stop the stress",
            "Save for something big",
            "Not sure yet",
        ),
    ),
    Question(
        id="involvement",
        dimension="behavior",
        prompt="And how hands-on should I be?",
        options=(
            "Set it up for me",
            "Suggest things, I'll do it",
            "Just track and guide",
            "Keep it light",
        ),
    ),
)

CORE_BY_ID = {q.id: q for q in CORE}

# Triggered follow-ups. `trigger` is a pure function of the answers collected so
# far; lower `priority` fires first. At most MAX_FOLLOWUPS fire per interview.
FOLLOWUPS: tuple[Question, ...] = (
    Question(
        id="debt_probe",
        dimension="debt",
        prompt="How much of that pressure is money you already owe?",
        options=(
            "None, just day to day",
            "Some, and it is under control",
            "A noticeable amount",
            "A lot, I would rather not look",
        ),
    ),
    Question(
        id="volatility_probe",
        dimension="liquidity",
        prompt="When money comes in late or uneven, what happens?",
        options=(
            "I hold my breath and wait",
            "I borrow or lean on credit",
            "I dip into what I saved",
            "Something always shows up",
        ),
    ),
    Question(
        id="obligations_probe",
        dimension="obligations",
        prompt="How fixed is the amount you send to family?",
        options=(
            "A set amount every month",
            "It varies",
            "Usually more than I planned",
            "It depends on what they need",
        ),
    ),
    Question(
        id="behavior_probe",
        dimension="behavior",
        prompt="When spending gets away from you, what is usually happening?",
        options=(
            "Small things add up",
            "I don't track it closely",
            "Stress or celebrating",
            "Big one-off purchases",
        ),
    ),
    Question(
        id="builder_probe",
        dimension="goal",
        prompt="How far along are you on building wealth?",
        options=(
            "Nothing yet",
            "A little",
            "Some, want more",
            "A good base already",
        ),
    ),
)

FOLLOWUP_BY_ID = {q.id: q for q in FOLLOWUPS}

_TRIGGERS: dict[str, Callable[[dict[str, str]], bool]] = {
    "debt_probe": lambda a: (
        a.get("money_feelings", "")
        in (
            "Stressful",
            "Honestly, a mess",
        )
    ),
    "volatility_probe": lambda a: (
        a.get("income_predictability", "")
        in (
            "Varies seasonally",
            "All over the place",
            "I don't have a steady one",
        )
        or a.get("shortage_cause", "") == "The timing, it comes in late"
    ),
    "obligations_probe": lambda a: (
        a.get("dependents", "") in ("Extended family", "I support others regularly")
        or a.get("shortage_cause", "") == "I support other people"
    ),
    "behavior_probe": lambda a: (
        a.get("shortage_cause", "") == "I spend more than I plan"
    ),
    "builder_probe": lambda a: a.get("goal_direction", "") == "Build wealth",
}

FOLLOWUP_PRIORITY = {
    "debt_probe": 1,
    "volatility_probe": 2,
    "obligations_probe": 3,
    "behavior_probe": 4,
    "builder_probe": 5,
}


def next_question(
    answers: dict[str, str],
    asked: list[str],
    max_followups: int = MAX_FOLLOWUPS,
) -> Question | None:
    """Return the question Miriam should ask next, or None when the interview
    spine is done (the caller moves on to the statement/plan step).

    ``answers`` maps question id -> chosen option text; ``asked`` is the ordered
    list of question ids already asked. Deterministic: the 7 core signals come
    first in order, then triggered follow-ups by priority (capped).
    """
    asked_ids = set(asked)
    for core in CORE:
        if core.id not in answered(answers) and core.id not in asked_ids:
            return core
    return _next_followup(answers, asked_ids, max_followups)


def _next_followup(
    answers: dict[str, str], asked_ids: set[str], max_followups: int
) -> Question | None:
    num_followups_asked = sum(1 for qid in asked_ids if qid in FOLLOWUP_BY_ID)
    eligible = [
        FOLLOWUP_BY_ID[qid]
        for qid in FOLLOWUP_PRIORITY
        if qid not in asked_ids and _TRIGGERS[qid](answers)
    ]
    if not eligible or num_followups_asked >= max_followups:
        return None
    return min(eligible, key=lambda q: FOLLOWUP_PRIORITY[q.id])


def answered(answers: dict[str, str]) -> dict[str, str]:
    """Answers keyed by id with a non-empty selection."""
    return {k: v for k, v in answers.items() if v}


def is_followup(question_id: str) -> bool:
    return question_id in FOLLOWUP_BY_ID


def all_question_ids() -> list[str]:
    return [q.id for q in CORE] + [q.id for q in FOLLOWUPS]


# When-suggestions for the LLM-led conductor: when it should reach for each
# follow-up, phrased as a hint instead of the deterministic trigger function.
# The adaptive branching still exists for the deterministic fallback path.
PROBE_HINTS: dict[str, str] = {
    "debt_probe": "ask when money feels stressful or messy, to size any debt",
    "volatility_probe": "ask when income is uneven or comes in late",
    "obligations_probe": "ask when the user says they support others",
    "behavior_probe": "ask when they mention overspending or not tracking",
    "builder_probe": "ask when their goal is building wealth",
}


def canonicalize(question_id: str, text: str) -> str:
    """Coerce an answer toward a canonical option for ``question_id``.

    Tolerant, case-insensitive match (exact first, then substring for text
    >= 3 chars). Returns the option verbatim when matched; otherwise returns
    ``text`` stripped so the plan builder still gets a usable value (it
    degrades gracefully on values it does not recognize). ``goal_direction``
    is intentionally left free-form when it does not match -- goals like
    "buy a house" are exactly what the plan should carry.
    """
    question = CORE_BY_ID.get(question_id) or FOLLOWUP_BY_ID.get(question_id)
    cleaned = text.strip()
    if question is None or not cleaned:
        return cleaned
    lowered = cleaned.casefold()
    if not lowered:
        return cleaned
    for option in question.options:
        if lowered == option.casefold():
            return option
    if len(cleaned) >= 3:
        for option in question.options:
            if lowered in option.casefold():
                return option
    return cleaned

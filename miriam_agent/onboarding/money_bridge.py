"""Deterministic bridge: conversational onboarding state -> real money plan."""
from __future__ import annotations
import logging
import re
from typing import Any
logger = logging.getLogger(__name__)

# The interview is three facts. Anything else waits until the plan exists.
# Questions stay short enough to be a poll title without cutting mid-word.
GAP_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("income_amount", "About how much do you take home in a normal month?"),
    ("income_frequency", "How often does that money arrive?"),
    ("fixed_costs", "About how much has to go out every month?"),
)
# Pay rhythm is the only multiple-choice. A number question gets no taps:
# "Build my savings plan" does not answer "what hits your account".
CADENCE_TAPS = ("Weekly", "Biweekly", "Monthly", "Irregular")
PLAN_CTA_TAPS = ("Build my savings plan", "How much can I save?")
_GAP_KEYS = ("income_amount", "income_frequency", "fixed_costs")
_PLAN_NOW = (
    "build my savings plan",
    "how much can i save",
    "how much can i save?",
    "show me the plan",
    "make the plan",
    "just make the plan",
)
# Explicit pay-rhythm phrases only. A bare "month" is a time word ("maybe a
# month of runway") and must not be stored as a salary cadence.
_CADENCE_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("biweekly", ("biweekly", "bi-weekly", "every two weeks", "every 2 weeks", "twice a month", "fortnight", "fortnightly")),
    ("weekly", ("weekly", "every week", "each week", "per week", "once a week")),
    ("monthly", ("monthly", "every month", "each month", "per month", "once a month", "end of the month")),
    ("irregular", ("irregular", "not fixed", "whenever it comes", "whenever i get paid")),
)
_GOAL_REPLY = re.compile(
    r"\b(invest|investment|grow|growing|save|saving|savings|buffer|stocks?)\b",
    re.IGNORECASE,
)
_CHOICE_INDEX = re.compile(r"^[1-4]$")

def _explicit_cadence(state: Any) -> bool:
    """True when pay rhythm was answered, or the income sentence already named it."""
    learned = getattr(state, "learned", None) or {}
    if not isinstance(learned, dict):
        return False
    rhythm = str(learned.get("pay_rhythm") or learned.get("income_frequency") or "")
    if rhythm.strip():
        return True
    return bool(_cadence_in(str(learned.get("income") or "")))


def money_readiness(state: Any) -> tuple[bool, list[str]]:
    try:
        from miriam_agent.financial.profile import profile_from_onboarding_state
        from miriam_agent.money.intake import from_financial_profile
    except Exception:
        return False, ["income_amount"]
    try:
        profile = profile_from_onboarding_state(state)
        intake = from_financial_profile(profile)
    except Exception:
        return False, ["income_amount"]
    turns = int(getattr(state, "interview_turns", 0) or 0)
    missing: list[str] = []
    if intake.monthly_income is None:
        missing.append("income_amount")
    # Ask how pay lands once, unless the income sentence already said it
    # ("every month") or they tapped Weekly/Biweekly/Monthly/Irregular.
    if not _explicit_cadence(state) and turns < 4:
        missing.append("income_frequency")
    if intake.fixed_costs is None and turns < 4:
        missing.append("fixed_costs")
    # Income alone is not a plan. Missing costs were being stored as zero,
    # which put the whole paycheck in guilt-free and still offered a stock buy.
    ready = intake.monthly_income is not None and intake.fixed_costs is not None
    if ready:
        missing = []
    return ready, missing

def build_money_plan_dict(state: Any) -> dict[str, Any] | None:
    try:
        from miriam_agent.money.intake import from_onboarding_state
        from miriam_agent.money.plan import build_money_plan
    except Exception:
        return None
    try:
        return build_money_plan(from_onboarding_state(state)).model_dump(mode="json")
    except Exception:
        logger.warning("money pipeline build failed", exc_info=True)
        return None

def gap_question(missing: list[str]) -> str:
    question = GAP_QUESTIONS[-1][1]
    for key, text in GAP_QUESTIONS:
        if key in missing:
            question = text
            break
    return question


def gap_taps(missing: list[str]) -> tuple[str, ...]:
    """Taps that answer the question. Number questions get none."""
    for key, _text in GAP_QUESTIONS:
        if key in missing:
            return CADENCE_TAPS if key == "income_frequency" else ()
    return ()


def next_unasked_gap(state: Any, missing: list[str]) -> str:
    asked = {str(item) for item in (getattr(state, "asked_gaps", None) or [])}
    for key in _GAP_KEYS:
        if key in missing and key not in asked:
            return key
    return ""


def wants_plan(text: str) -> bool:
    folded = (text or "").strip().casefold()
    return folded in _PLAN_NOW


def cap_should_present(state: Any, text: str) -> bool:
    """At the question cap: show the plan unless one foundational gap is still unasked."""
    ready, missing = money_readiness(state)
    if ready or wants_plan(text):
        return True
    signal = bool(
        (getattr(state, "learned", None) or {})
        or getattr(state, "money_moment", "")
        or getattr(state, "goal", "")
    )
    if not signal:
        return True
    return next_unasked_gap(state, missing) == ""


def mark_gap_asked(state: Any, key: str) -> None:
    if not key:
        return
    asked = list(getattr(state, "asked_gaps", None) or [])
    if key not in asked:
        asked.append(key)
    state.asked_gaps = asked


def remember_poll(state: Any, title: str, options: list[str] | tuple[str, ...]) -> None:
    state.last_poll_title = (title or "").strip()
    state.last_poll_options = [str(item).strip() for item in options if str(item).strip()]


def _cadence_in(text: str) -> str:
    folded = (text or "").casefold()
    for name, phrases in _CADENCE_PHRASES:
        if any(phrase in folded for phrase in phrases):
            return name
    return ""


def _resolve_choice(state: Any, text: str) -> str:
    """Map '1' to the first option on the poll they are answering."""
    raw = (text or "").strip()
    options = [str(item) for item in (getattr(state, "last_poll_options", None) or [])]
    if _CHOICE_INDEX.fullmatch(raw) and options:
        index = int(raw) - 1
        if 0 <= index < len(options):
            return options[index]
    return raw


def _is_choice_index(text: str) -> bool:
    return bool(_CHOICE_INDEX.fullmatch((text or "").strip()))


_EXPLICIT_AMOUNT = re.compile(
    r"(\$|₦|£|€|\b(?:ngn|usd|gbp)\b|\d[\d,]*(?:\.\d+)?\s*(?:k|m|thousand|million)\b)",
    re.IGNORECASE,
)
_AMOUNT_REPLY = re.compile(
    r"(?:about|around|roughly|maybe|like|between|under|over)?\s*"
    r"(?:\$|₦|£|€)?\s*\d[\d,]*(?:\.\d+)?\s*(?:k|m)?"
    r"(?:\s*(?:or less|or so|a month|per month|monthly|a week|per week))?",
    re.IGNORECASE,
)


def _explicit_amount_reply(text: str) -> bool:
    """A reply that is actually an amount, not an incidental digit like "m1"."""
    raw = (text or "").strip()
    if not raw or _is_choice_index(raw):
        return False
    if _EXPLICIT_AMOUNT.search(raw):
        return True
    return bool(_AMOUNT_REPLY.fullmatch(raw))


def absorb_reply(state: Any, text: str, *, poll_title: str = "") -> None:
    """Write salary, pay rhythm, and fixed costs from the user's own words.

    The model is not the source of truth for these. A tap, a number, or
    "biweekly" has to land even when the reply is short or numbered.
    """
    spoken = _resolve_choice(state, text)
    if not spoken.strip():
        return
    if wants_plan(spoken):
        return
    ready_before, missing = money_readiness(state)
    del ready_before
    gap = ""
    title = (poll_title or getattr(state, "last_poll_title", "") or "").casefold()
    for key, question in GAP_QUESTIONS:
        if question.casefold() in title or key.replace("_", " ") in title:
            gap = key
            break
    if not gap and missing:
        gap = missing[0]

    cadence = _cadence_in(spoken)
    if cadence:
        state.learned["pay_rhythm"] = cadence
    elif gap == "income_frequency":
        tapped = spoken.strip().casefold()
        for name in ("weekly", "biweekly", "monthly", "irregular"):
            if tapped == name or tapped.startswith(name):
                state.learned["pay_rhythm"] = name
                break

    try:
        from miriam_agent.financial.profile import amount_in, extract_money_facts
    except Exception:
        return
    facts = extract_money_facts(spoken)
    learned = state.learned
    spoke_amount = _explicit_amount_reply(spoken) and amount_in(spoken) is not None
    if "income_amount" in facts or (gap == "income_amount" and spoke_amount):
        learned["income"] = spoken.strip()[:400]
    if "essential_expenses" in facts or (gap == "fixed_costs" and spoke_amount):
        learned["fixed"] = spoken.strip()[:400]
    goal = str(getattr(state, "goal", "") or "")
    if not goal and _GOAL_REPLY.search(spoken) and amount_in(spoken) is None and not cadence:
        state.goal = spoken.strip()[:120]

_RHYTHM_SENTENCE = {
    "weekly": "It arrives every week.",
    "biweekly": "It arrives every two weeks.",
    "monthly": "It arrives once a month.",
    "irregular": "It arrives at uneven times.",
}


def reply_has_money_fact(text: str) -> bool:
    """True when this message itself is an amount or a pay-rhythm answer."""
    return _explicit_amount_reply(text) or bool(_cadence_in(text))


def pay_rhythm_known(state: Any) -> bool:
    learned = getattr(state, "learned", None) or {}
    if not isinstance(learned, dict):
        return False
    return bool(str(learned.get("pay_rhythm") or learned.get("income_frequency") or "").strip())


def _dec(value: Any) -> Any:
    from decimal import Decimal

    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _months_phrase(value: Any) -> str:
    from decimal import Decimal

    number = _dec(value)
    if number is None:
        return ""
    whole = number.to_integral_value()
    if number == whole:
        count = int(whole)
        unit = "month" if count == 1 else "months"
        return f"{count} {unit}"
    return f"{number.quantize(Decimal('0.1'))} months"


def _amt(value: Any, currency: str) -> str:
    try:
        from miriam_agent.money.formatting import format_amount as _fmt

        return str(_fmt(value, currency))
    except Exception:
        return f"{currency} {value}"


def _intake(state: Any) -> Any:
    from miriam_agent.financial.profile import profile_from_onboarding_state
    from miriam_agent.money.intake import from_financial_profile

    return from_financial_profile(profile_from_onboarding_state(state))


def fact_readback(state: Any) -> str:
    """Say the three facts back in plain English before any plan is written."""
    learned = getattr(state, "learned", None) or {}
    if not isinstance(learned, dict):
        learned = {}
    name = str(getattr(state, "name", "") or "").strip()
    try:
        intake = _intake(state)
    except Exception:
        intake = None
    if intake is None or intake.monthly_income is None:
        return "Let me check I heard you right before I write the plan. Is that right?"
    currency = str(intake.currency or "NGN")
    income = _amt(intake.monthly_income, currency)
    raw_income = str(learned.get("income") or "").casefold()
    if "or less" in raw_income or "under " in raw_income:
        income_sentence = (
            f"You said {income} or less. I will use {income} unless you tell me it is lower."
        )
    elif any(word in raw_income for word in ("about", "around", "roughly", "maybe")):
        income_sentence = f"I will use about {income} a month."
    else:
        income_sentence = f"I will use {income} a month."
    rhythm = str(learned.get("pay_rhythm") or "").strip().casefold()
    rhythm_sentence = _RHYTHM_SENTENCE.get(
        rhythm, "I do not know yet how often it arrives."
    )
    if intake.monthly_fixed is None:
        fixed_sentence = "I still need the amount that has to go out."
    else:
        fixed_sentence = f"About {_amt(intake.monthly_fixed, currency)} has to go out."
    if name:
        income_sentence = income_sentence[0].lower() + income_sentence[1:]
        return f"{name}, {income_sentence} {rhythm_sentence} {fixed_sentence} Is that right?"
    return f"{income_sentence} {rhythm_sentence} {fixed_sentence} Is that right?"


def plain_month_lines(money_plan: dict[str, Any]) -> list[str]:
    """The month as short sentences. Unknown lines are left out, not printed as zero."""
    currency = str(money_plan.get("currency") or "NGN")
    cash = money_plan.get("cashflow") or {}
    buffer = money_plan.get("buffer") or {}
    income = _dec(money_plan.get("monthly_take_home"))
    fixed = _dec(cash.get("fixed"))
    from decimal import Decimal

    zero = Decimal("0")
    savings = _dec(cash.get("savings")) or zero
    debt = _dec(cash.get("debt")) or zero
    invest = _dec(cash.get("investments")) or zero
    guilt = _dec(cash.get("guilt_free")) or zero
    target = _dec(buffer.get("target_amount"))
    lines: list[str] = []

    if fixed is None or fixed <= 0:
        if income is not None and income > 0:
            lines.append(
                f"You take home {_amt(income, currency)}. "
                "I do not know what has to go out yet, so this is not a finished plan."
            )
        lines.append("Nothing goes to stocks yet.")
        return lines

    if income is not None:
        left = income - fixed
        if left < 0:
            left = Decimal("0")
        lines.append(
            f"You take home {_amt(income, currency)}. "
            f"About {_amt(fixed, currency)} has to go out. "
            f"Before the plan does anything, that leaves {_amt(left, currency)}."
        )
    else:
        lines.append(f"About {_amt(fixed, currency)} has to go out.")

    if target is not None and target > 0 and savings > 0:
        cover = _months_phrase(buffer.get("target_months"))
        cover_bit = f"cover {cover} of bills" if cover else "cover the bills"
        fill = "That does not fill it." if savings < target else "That fills it."
        lines.append(
            f"The buffer should {cover_bit}, which is {_amt(target, currency)}. "
            f"This month {_amt(savings, currency)} goes there. {fill}"
        )
    elif savings > 0:
        lines.append(f"This month {_amt(savings, currency)} goes to savings.")

    if debt > 0:
        lines.append(f"{_amt(debt, currency)} goes to a debt payment.")

    tail: list[str] = []
    if guilt > 0:
        tail.append(f"You can spend {_amt(guilt, currency)} on anything you want.")
    elif income is not None:
        tail.append("After the plan, nothing is left to spend freely this month.")
    if invest > 0:
        tail.append(
            "The stock money buys the Rail Stock Sleeve: tokenized Apple, Nvidia, and Tesla."
        )
    else:
        tail.append("Nothing goes to stocks yet.")
    lines.append(" ".join(tail))
    return lines


def plan_receipt_lines(money_plan: dict[str, Any]) -> list[str]:
    """The two numbers that matter after consent: what goes out, what is saved.

    Deliberately shorter than ``plain_month_lines``. After "Locked in." the user
    has already read the month once, so re-sending the whole plan reads as a
    glitch rather than a receipt.
    """
    currency = str(money_plan.get("currency") or "NGN")
    cash = money_plan.get("cashflow") or {}
    income = _dec(money_plan.get("monthly_take_home"))
    fixed = _dec(cash.get("fixed"))
    savings = _dec(cash.get("savings"))
    lines: list[str] = []
    if fixed is not None and fixed > 0:
        if income is not None and income > 0:
            lines.append(
                f"You take home {_amt(income, currency)}. "
                f"About {_amt(fixed, currency)} has to go out."
            )
        else:
            lines.append(f"About {_amt(fixed, currency)} has to go out.")
    if savings is not None and savings > 0:
        lines.append(f"This month {_amt(savings, currency)} goes to the buffer.")
    return lines


def plan_bubbles(money_plan: dict[str, Any]) -> list[str]:
    """At most three short texts. The last one always asks to lock or change a number."""
    lines = plain_month_lines(money_plan)
    question = "Want me to lock this in, or change a number?"
    if not lines:
        return [question]
    lines[-1] = f"{lines[-1]} {question}"
    if len(lines) > 3:
        lines = lines[:2] + [" ".join(lines[2:])]
    return lines


def render_money_plan_text(money_plan: dict[str, Any]) -> str:
    return "\n\n".join(plan_bubbles(money_plan)).strip()

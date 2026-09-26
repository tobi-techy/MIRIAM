"""Deterministic bridge: conversational onboarding state -> real money plan."""
from __future__ import annotations
import logging
import re
from typing import Any
logger = logging.getLogger(__name__)

# The interview is three facts. Anything else waits until the plan exists.
# Questions stay short enough to be a poll title without cutting mid-word.
GAP_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("income_amount", "No wahala, what hits your account in a normal month?"),
    ("income_frequency", "Does that pay land weekly, biweekly, monthly, or irregular?"),
    ("fixed_costs", "Roughly what must go out every month?"),
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
    # A goal does not block the plan. Income plus fixed costs is the plan.
    # After three interview turns, income alone is enough and the plan labels
    # whatever it had to assume.
    ready = intake.monthly_income is not None and (
        intake.fixed_costs is not None or turns >= 3
    )
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

def render_money_plan_text(money_plan: dict[str, Any]) -> str:
    try:
        from miriam_agent.money.formatting import format_amount as _fmt
    except Exception:
        _fmt = None  # type: ignore
    def _amt(value: Any, currency: str) -> str:
        try:
            if _fmt is not None:
                return str(_fmt(value, currency))
        except Exception:
            pass
        return f"{currency} {value}"
    currency = str(money_plan.get("currency") or "NGN")
    lines: list[str] = []
    diagnosis = str(money_plan.get("diagnosis") or "").strip()
    if diagnosis:
        lines += [diagnosis, ""]
    lines.append("Here's your savings split for the month:")
    cashflow = money_plan.get("cashflow") or {}
    for label, key in (("Fixed costs", "fixed"), ("Debt attack", "debt"), ("Savings (buffer)", "savings"), ("Investments", "investments"), ("Guilt-free", "guilt_free")):
        lines.append(f"- {label}: {_amt(cashflow.get(key, 0), currency)}")
    actions = [a for a in (money_plan.get("actions_90d") or []) if isinstance(a, dict)][:3]
    if actions:
        lines += ["", "Your next moves:"]
        for action in actions:
            when = str(action.get("when") or "").strip()
            what = str(action.get("what") or "").strip()
            if what:
                lines.append(f"- {(when + ': ' if when else '')}{what}".strip())
    rules = money_plan.get("automation_rules") or []
    if rules and str(rules[0]).strip():
        lines += ["", f"Automation: {str(rules[0]).strip()}."]
    invest = cashflow.get("investments")
    try:
        invest_amount = float(invest or 0)
    except (TypeError, ValueError):
        invest_amount = 0
    if invest_amount > 0:
        lines.append(
            "That invest slice buys the Rail Stock Sleeve: tokenized Apple, Nvidia, and Tesla."
        )
    else:
        lines.append(
            "Once the month closes, that invest slice buys the Rail Stock Sleeve: tokenized Apple, Nvidia, and Tesla."
        )
    lines += ["", "Want me to lock this in sharp sharp, or adjust anything?"]
    assumptions = money_plan.get("assumptions") or []
    if assumptions:
        lines.append(f"Note: {str(assumptions[0]).strip()}")
    if str(money_plan.get("confidence") or "") == "low":
        lines.append("Confidence is low until I have your exact fixed costs.")
    lines.append(str(money_plan.get("disclaimer") or "").strip())
    return "\n".join(lines).strip()

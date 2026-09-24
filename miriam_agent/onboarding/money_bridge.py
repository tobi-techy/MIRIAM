"""Deterministic bridge: conversational onboarding state -> real money plan."""
from __future__ import annotations
import logging
from typing import Any
logger = logging.getLogger(__name__)
GAP_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("income_amount", "No wahala, just roughly: what hits your account in a normal month \u2014 salary, hustle, everything together?"),
    ("fixed_costs", "Roughly what must go out every month \u2014 rent, food, transport, everything you can't skip?"),
    ("goal", "If your money worked, what would it do for you first \u2014 a buffer, clearing debt, or a named goal?"),
)
PLAN_CTA_TAPS = ("Build my savings plan", "How much can I save?")

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
    goal = str(getattr(state, "goal", "") or "")
    missing: list[str] = []
    if intake.monthly_income is None:
        missing.append("income_amount")
    if intake.fixed_costs is None and turns < 4:
        missing.append("fixed_costs")
    if not goal.strip() and not missing:
        missing.append("goal")
    ready = not missing or (intake.monthly_income is not None and turns >= 4)
    if ready:
        missing = [m for m in missing if m != "fixed_costs"]
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
    lines += ["", "Want me to lock this in sharp sharp, or adjust anything?"]
    assumptions = money_plan.get("assumptions") or []
    if assumptions:
        lines.append(f"Note: {str(assumptions[0]).strip()}")
    if str(money_plan.get("confidence") or "") == "low":
        lines.append("Confidence is low until I have your exact fixed costs.")
    lines.append(str(money_plan.get("disclaimer") or "").strip())
    return "\n".join(lines).strip()

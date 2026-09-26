"""Stateless completion copy: consent receipts and plan resumption."""
from __future__ import annotations
from typing import Any

def automated_completion_text(state: Any) -> str:
    """Receipt after consent_yes: real amounts first, vibe labels never."""
    money_plan = getattr(state, "money_plan", None) or {}
    if money_plan:
        from miriam_agent.money.formatting import format_amount as _fmt
        cur = str(money_plan.get("currency") or "NGN")
        cashflow = money_plan.get("cashflow") or {}
        def _amt(v: Any) -> str:
            try:
                return str(_fmt(v, cur))
            except Exception:
                return f"{cur} {v}"
        body = "Locked in - e don set. Your month now runs like this:\n"
        for label, key in (("Fixed costs", "fixed"), ("Savings (buffer)", "savings"), ("Debt attack", "debt"), ("Guilt-free", "guilt_free")):
            body += f"\u2022 {label}: {_amt(cashflow.get(key, 0))}\n"
        try:
            invest_amount = float(cashflow.get("investments") or 0)
        except (TypeError, ValueError):
            invest_amount = 0
        learned = getattr(state, "learned", None) or {}
        costs_known = bool(str(learned.get("fixed") or "").strip())
        if not costs_known and float(cashflow.get("fixed") or 0) == 0:
            body += (
                "\nI still don't have what must go out each month, so this split "
                "is a placeholder and nothing is invested.\n"
            )
        elif invest_amount > 0:
            body += (
                "\nThe invest slice buys the Rail Stock Sleeve: "
                "tokenized Apple, Nvidia, and Tesla.\n"
            )
        else:
            body += (
                "\nNothing goes to stocks yet. The Rail Stock Sleeve waits until "
                "the month leaves an invest slice.\n"
            )
        body += "\nWhen pay lands, the buffer comes out first. You stay the one who decides."
        return body
    plank = getattr(state, "plan", None) or {}
    bullets = [s["title"].lower() for s in plank.get("steps", [])][:4]
    body = "E don set - this is now how I work for you:\n"
    for b in bullets:
        body += f"\u2022 {b} first\n"
    body += "\nI keep an eye on it and bring things up when they deserve attention. You stay the one who decides."
    return body

def draft_completion_text() -> str:
    return ("No wahala. I've saved the plan - ask me to put it into action anytime and there's no need to go through this again.")

def resume_payload(state: Any) -> dict[str, Any]:
    """Resume payload: where the user stands, what's missing, next step."""
    from miriam_agent.onboarding.money_bridge import money_readiness
    from miriam_agent.onboarding.state import STAGE_COMPLETE
    ready, missing = money_readiness(state)
    money_plan = getattr(state, "money_plan", None) or {}
    stage = getattr(state, "stage", "")
    if stage == STAGE_COMPLETE:
        nxt = "onboarding complete — ask for the plan, an adjustment, or a fresh check-in"
    elif money_plan:
        nxt = "review the savings split and lock it in, or adjust it"
    elif missing == ["income_amount"] or (not missing and not ready):
        nxt = "share roughly what hits your account monthly"
    elif "fixed_costs" in missing:
        nxt = "share roughly what must go out monthly"
    elif "goal" in missing:
        nxt = "name what the money should do first (buffer, debt, goal)"
    else:
        nxt = "continue the interview"
    completed = ["greeting"] if getattr(state, "name", "") else []
    if getattr(state, "money_moment", ""):
        completed.append("money_moment")
    if ready or money_plan:
        completed.append("numbers_captured")
    if money_plan:
        completed.append("plan_presented")
    return {"current_stage": stage, "completed_stages": completed, "unresolved": list(missing), "money_ready": bool(ready), "has_money_plan": bool(money_plan), "next_recommended_step": nxt}

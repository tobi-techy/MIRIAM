"""Stateless completion copy: consent receipts and plan resumption."""
from __future__ import annotations
from typing import Any

def automated_completion_text(state: Any) -> str:
    """Receipt after consent_yes: plain sentences, and no line for a fact she never asked."""
    money_plan = getattr(state, "money_plan", None) or {}
    if money_plan:
        from miriam_agent.onboarding.money_bridge import plan_receipt_lines

        cashflow = money_plan.get("cashflow") or {}
        learned = getattr(state, "learned", None) or {}
        costs_known = bool(str(learned.get("fixed") or "").strip())
        try:
            fixed_amount = float(cashflow.get("fixed") or 0)
        except (TypeError, ValueError):
            fixed_amount = 0
        lines = ["Locked in.", ""]
        # A short receipt, not the whole plan again: the user has just read it.
        lines.extend(plan_receipt_lines(money_plan))
        if not costs_known and fixed_amount == 0:
            lines.append(
                "I still do not know what has to go out each month, so this is a "
                "placeholder and nothing is invested."
            )
        lines.append(
            "When pay arrives, the bills come out first, then the buffer. You still decide."
        )
        return "\n".join(lines).strip()
    plank = getattr(state, "plan", None) or {}
    bullets = [s["title"].lower() for s in plank.get("steps", [])][:4]
    body = "E don set - this is now how I work for you:\n"
    for b in bullets:
        body += f"\u2022 {b} first\n"
    body += "\nI keep an eye on it and bring things up when they deserve attention. You stay the one who decides."
    return body

def draft_completion_text() -> str:
    return (
        "I have saved the plan. Ask me when you want to put it into action. "
        "You do not need to do this again."
    )

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

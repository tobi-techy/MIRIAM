"""Deterministic financial plan built from what Miriam learned in the interview.

Miriam extracts a free-form ``facts`` set from the conversation, plus a
``money_moment`` and a concrete ``goal``. There is no fixed vocabulary, so this
builder sniffs the *meaning* of whatever text it is handed: a user who says
"my income is all over the place" and one who says "commissions are lumpy" both
land in the same place.

The plan follows the Financial State Model: a primary diagnostic state
(Stability Seeker / Volatile Earner / Wealth Builder / Financial Beginner),
overlays that sharpen it (debt burden, family obligations, spending leakage,
goal urgency), then a small priority-ordered set of concrete steps.

Consent is what turns a plan into living standing rules: the automation bullets
only exist for a hands-on user, and Miriam never moves money on her own -- the
rules describe what she watches, suggests, and helps execute.
"""

from __future__ import annotations

import re
from typing import Any

# --- Signal sniffs: substring checks over the (lowercased) words we have. -----
# They are deliberately fuzzy because the agent's keys and phrasing vary.

_RUNWAY_TIGHT = re.compile(
    r"(few days|maybe a month|about a month|one month|single month|month to"
    r" month|paycheck to paycheck|ran out|run out|runs out|almost broke|nothing"
    r" left|zero by|a week|two weeks|immediately)"
)
_RUNWAY_STABLE = re.compile(r"(three month|3 month|six month|6 month|several month)")
_INCOME_UNSTABLE = re.compile(
    r"(unpredict|irregular|varies|volatile|seasonal|freelance|commission|"
    r"inconsistent|not steady|up and down|all over|comes in late|comes late|"
    r"not the same every)"
)
_LEANS_CREDIT = re.compile(
    r"(borrow|overdraft|lean on|advance|hold my breath|credit to cover|"
    r"cash advance|on credit|rely on credit|borrowing)"
)
_HAS_DEPENDENTS = re.compile(
    r"(family|mother|mum|mom|father|dad|parents|sibling|brother|sister|children|kid"
    r"|send.*home|support)"
)
_DEBT_HEAVY = re.compile(r"(debt|loan|owe|card debt|credit card)")
_SPENDING_LEAK = re.compile(
    r"(overspend|spend more than|don't track|not track|never track|impulse|"
    r"subscription|eating out|small things add up|discipline|stress buy|"
    r"celebration)"
)
_WANTS_WEALTH = re.compile(r"(wealth|invest|grow|build|net worth|rich)")
_SEEKING_STABILITY = re.compile(r"(stress|anxious|panic|worr|drown|scared)")
_GOAL_LIKE = re.compile(r"goal|rich life|want.*money|plan to", re.IGNORECASE)
_HANDS_ON = re.compile(
    r"set it up|automate|hands-?on|do it for me|handle it for me|take over|"
    r"just do it|run it",
    re.IGNORECASE,
)
_SELF_DRIVEN = re.compile(
    r"keep it light|just tell me|just suggest|i'll do it|i will do it|light touch",
    re.IGNORECASE,
)

# spec §21: one structured insight per plan. state -> (category, title,
# summary fragment, recommended-action type). The personality layer speaks it;
# the backend keeps the structured understanding.
_INSIGHT_RECIPES: dict[str, tuple[str, str, str, str]] = {
    "Stability Seeker": (
        "cash_flow",
        "saving happens last",
        "the buffer runs out before the money does, and saving comes last",
        "safety_net",
    ),
    "Volatile Earner": (
        "income_volatility",
        "income arrives unevenly, spending stays steady",
        "the money comes in lumps but the outgo keeps a fixed rhythm",
        "income_split",
    ),
    "Wealth Builder": (
        "growth",
        "growth can go first",
        "there is room to grow, but growth still only happens after spending",
        "automated_allocation",
    ),
    "Financial Beginner": (
        "foundations",
        "start with the floor",
        "a buffer and a rhythm come before anything fancy",
        "safety_net",
    ),
}

_ACTION_TIMING: dict[str, str] = {
    "safety_net": "income_received",
    "income_split": "each_inflow",
    "automated_allocation": "income_received",
}


def _text(facts: dict[str, str], goal: str, money_moment: str) -> str:
    parts = list(facts.values())
    if goal:
        parts.append(goal)
    if money_moment:
        parts.append(money_moment)
    return " ".join(p for p in parts if p).lower()


def _goal_text(facts: dict[str, str], goal: str) -> str:
    """The concrete goal, from the dedicated field or a goal-like fact."""
    if goal:
        return goal
    for key, value in facts.items():
        if _GOAL_LIKE.search(key) or _GOAL_LIKE.search(value):
            if value not in ("not sure yet", "not sure"):
                return value
    return ""


def _short_runway(txt: str) -> bool:
    return bool(_RUNWAY_TIGHT.search(txt))


def _stable_runway(txt: str) -> bool:
    return bool(_RUNWAY_STABLE.search(txt))


def _income_unpredictable(txt: str) -> bool:
    return bool(_INCOME_UNSTABLE.search(txt))


def _leans_on_credit(txt: str) -> bool:
    return bool(_LEANS_CREDIT.search(txt))


def _has_dependents(txt: str) -> bool:
    return bool(_HAS_DEPENDENTS.search(txt))


def _debt_heavy(txt: str) -> bool:
    return bool(_DEBT_HEAVY.search(txt))


def _spending_leak(txt: str) -> bool:
    return bool(_SPENDING_LEAK.search(txt))


def _wants_wealth(txt: str) -> bool:
    return bool(_WANTS_WEALTH.search(txt))


def _seeking_stability(txt: str) -> bool:
    return bool(_SEEKING_STABILITY.search(txt))


def _automate(txt: str) -> bool:
    return bool(_HANDS_ON.search(txt))


def diagnostic_state(
    facts: dict[str, str], *, goal: str = "", money_moment: str = ""
) -> str:
    """Primary label for the user's money situation, most urgent first."""
    txt = _text(facts, goal, money_moment)
    if _short_runway(txt):
        return "Stability Seeker"
    if _income_unpredictable(txt) or _leans_on_credit(txt):
        return "Volatile Earner"
    if _wants_wealth(txt) and _stable_runway(txt):
        return "Wealth Builder"
    return "Financial Beginner"


def overlays(
    facts: dict[str, str], *, goal: str = "", money_moment: str = ""
) -> list[str]:
    """Secondary signals that refine the diagnostic label."""
    txt = _text(facts, goal, money_moment)
    out: list[str] = []
    if _debt_heavy(txt):
        out.append("debt_burden")
    if _has_dependents(txt):
        out.append("family_obligations")
    if _spending_leak(txt):
        out.append("spending_leakage")
    if _seeking_stability(txt):
        out.append("goal_urgency")
    return out


def build_plan(
    facts: dict[str, str],
    document_summary: str | None = None,
    *,
    goal: str = "",
    money_moment: str = "",
) -> dict[str, Any]:
    """Build the full plan dict: diagnosis, steps, standing rules, copy.

    Pure and deterministic so the service can persist it verbatim and tests can
    assert exact rule sets.
    """
    txt = _text(facts, goal, money_moment)
    goal_named = _goal_text(facts, goal)
    state_name = diagnostic_state(facts, goal=goal, money_moment=money_moment)
    overlays_list = overlays(facts, goal=goal, money_moment=money_moment)
    automate = _automate(txt)

    steps: list[dict[str, str]] = []

    # 1. Safety buffer -- every user needs one; urgency scales off the runway.
    if _short_runway(txt) or _income_unpredictable(txt):
        steps.append(
            {
                "id": "buffer",
                "kind": "capability",
                "title": "Protect the next month",
                "detail": (
                    "Build a one-month buffer before anything else, tucked "
                    "away the moment income lands - not what is left over."
                ),
            }
        )
    else:
        steps.append(
            {
                "id": "buffer",
                "kind": "capability",
                "title": "Keep a three-month buffer",
                "detail": (
                    "Top the buffer to three months of essentials so surprises "
                    "bend, not break, the month."
                ),
            }
        )

    # 2. Flexible income rhythm -- volatility needs a matching income pattern.
    if _income_unpredictable(txt) or _leans_on_credit(txt):
        steps.append(
            {
                "id": "income_rhythm",
                "kind": "stability",
                "title": "Split income into paycheck-sized pieces",
                "detail": (
                    "Treat every incoming lump as several smaller paychecks "
                    "so a quiet week never starts a panic."
                ),
            }
        )

    # 3. Family obligations -- fixed envelope, never the flexible line item.
    if _has_dependents(txt):
        steps.append(
            {
                "id": "obligations",
                "kind": "protection",
                "title": "Fix the family envelope",
                "detail": (
                    "Give support a set monthly number that gets funded "
                    "first, so the people you carry are never surprised by "
                    "a variable month."
                ),
            }
        )

    # 4. Debt -- contained, ordered, and never funding everyday life.
    if _debt_heavy(txt) or _leans_on_credit(txt):
        steps.append(
            {
                "id": "debt",
                "kind": "protection",
                "title": "Stop borrowing for the timing",
                "detail": (
                    "Pay minimums everywhere, aim the extra at the smallest "
                    "balance first, and stop using credit to cover timing gaps."
                ),
            }
        )

    # 5. Spending guard -- defaults beat discipline.
    if _spending_leak(txt):
        steps.append(
            {
                "id": "spending_guard",
                "kind": "behavior",
                "title": "Put a pause in before big buys",
                "detail": (
                    "Keep essentials and fun on separate money, and give any "
                    "big purchase a 24-hour pause so it is a choice, not a habit."
                ),
            }
        )

    # 6. Forward goal -- named, automatic, first.
    if goal_named:
        steps.append(
            {
                "id": "goal",
                "kind": "goal",
                "title": "Fund the goal first",
                "detail": (
                    "Move a set slice of every income toward "
                    f"'{goal_named}' before anything else gets its turn."
                ),
            }
        )

    # 7. Weekly check-in -- the confidence layer.
    steps.append(
        {
            "id": "checkin",
            "kind": "confidence",
            "title": "Two-minute money check-in",
            "detail": (
                "One short weekly chat with me keeps the plan on track "
                "without obsessing over it."
            ),
        }
    )

    # Standing rules -- only for hands-on users; always non-destructive.
    standing_rules: list[dict[str, str]] = []
    if automate:
        save_target = (
            "the month-ahead buffer"
            if _short_runway(txt)
            else "the safety buffer first"
        )
        standing_rules.append(
            {
                "kind": "buffer",
                "trigger": "income lands",
                "action": (
                    f"move a set slice of income into {save_target} before any spending"
                ),
                "cadence": "on inflow",
            }
        )
        if _income_unpredictable(txt) or _leans_on_credit(txt):
            standing_rules.append(
                {
                    "kind": "income_rhythm",
                    "trigger": "each lump arrives",
                    "action": "split it into smaller paycheck-sized pieces",
                    "cadence": "on inflow",
                }
            )
        if _has_dependents(txt):
            standing_rules.append(
                {
                    "kind": "obligations",
                    "trigger": "monthly",
                    "action": "fund the fixed family envelope first",
                    "cadence": "monthly",
                }
            )
        if _debt_heavy(txt):
            standing_rules.append(
                {
                    "kind": "debt",
                    "trigger": "after essentials",
                    "action": (
                        "pay minimums everywhere, extra to the smallest debt first"
                    ),
                    "cadence": "monthly",
                }
            )
        if _spending_leak(txt):
            standing_rules.append(
                {
                    "kind": "spending_guard",
                    "trigger": "before any big purchase",
                    "action": "pause 24 hours and check it against the month",
                    "cadence": "on purchase",
                }
            )
        if goal_named:
            standing_rules.append(
                {
                    "kind": "goal",
                    "trigger": "income lands",
                    "action": f"move a set slice toward '{goal_named}' first",
                    "cadence": "on inflow",
                }
            )

    evidence = [m for m in (money_moment, goal_named) if m]
    for value in list(facts.values())[:1]:
        if len(evidence) < 2:
            evidence.append(value)

    summary = _summary_text(state_name, overlays_list, document_summary, goal_named)
    insight = _insight(state_name, overlays_list, evidence, document_summary)

    return {
        "diagnostic_state": state_name,
        "overlays": overlays_list,
        "insight": insight,
        "steps": steps,
        "standing_rules": standing_rules,
        "automate": automate,
        "source": "onboarding_interview",
        "statement_summary": document_summary,
        "evidence": evidence,
        "summary": summary,
    }


def _insight(
    state_name: str,
    overlays_list: list[str],
    evidence: list[str],
    document_summary: str | None,
) -> dict[str, Any]:
    """spec §21: a single structured financial insight anchored by the
    diagnostic. Deterministic, like the rest of the plan -- the numbers aren't
    real yet at onboarding (no account data), so impact stays null and the
    confidence reflects how grounded the read is."""
    category, title, summary, action_type = _INSIGHT_RECIPES[state_name]
    severity = "high" if state_name == "Stability Seeker" else "medium"
    if "debt_burden" in overlays_list and state_name in (
        "Stability Seeker",
        "Volatile Earner",
    ):
        severity = "high"
    if state_name == "Financial Beginner" and "goal_urgency" not in overlays_list:
        severity = "low"

    confidence = 0.6
    if document_summary:
        confidence += 0.15
    confidence += min(0.15, 0.05 * len(evidence))
    confidence = round(min(confidence, 0.95), 2)

    return {
        "type": "financial_insight",
        "category": category,
        "title": title,
        "summary": summary,
        "severity": severity,
        "confidence": confidence,
        "financial_impact": None,
        "evidence": list(evidence[:3]),
        "recommended_action": {
            "type": action_type,
            "amount": None,
            "timing": _ACTION_TIMING[action_type],
        },
    }


def _summary_text(
    state_name: str, overlays_list: list[str], document_summary: str | None, goal: str
) -> str:
    label_map = {
        "Stability Seeker": "you want stability, so we build a floor first",
        "Volatile Earner": (
            "your income moves a lot, so we build a rhythm that matches it"
        ),
        "Wealth Builder": "you have room to grow, so we make growth automatic",
        "Financial Beginner": "we start with the basics that actually compound",
    }
    text = label_map.get(state_name, "we start with the basics")
    if document_summary:
        text += ". Your statement backs this up with real numbers"
    if overlays_list:
        text += ". The plan is ordered so the urgent parts come first"
    if goal:
        text += "."
    return text

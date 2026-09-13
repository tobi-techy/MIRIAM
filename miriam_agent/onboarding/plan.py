"""Deterministic financial plan built from the onboarding answers.

The plan follows the Financial State Model: a primary diagnostic state
(Stability Seeker / Volatile Earner / Wealth Builder / Financial Beginner),
overlays that sharpen it (debt burden, family obligations, spending leakage,
goal urgency), then a small priority-ordered set of concrete steps. Each step is
grounded in a specific answer, never invented.

Consent is what turns a plan into living standing rules: the automation bullets
only exist for users who picked a hands-on involvement level, and Miriam never
moves money on her own — the rules describe what she watches, suggests, and
helps execute.
"""

from __future__ import annotations

from typing import Any


def _short_runway(answers: dict[str, str]) -> bool:
    return answers.get("liquidity_runway", "") in ("A few days", "Maybe a month")


def _stable_runway(answers: dict[str, str]) -> bool:
    return answers.get("liquidity_runway", "") in (
        "Three to six months",
        "Six months or more",
    )


def _income_unpredictable(answers: dict[str, str]) -> bool:
    return answers.get("income_predictability", "") in (
        "Varies seasonally",
        "All over the place",
        "I don't have a steady one",
    )


def _leans_on_credit(answers: dict[str, str]) -> bool:
    return answers.get("volatility_probe", "") in (
        "I borrow or lean on credit",
        "I hold my breath and wait",
    )


def _has_dependents(answers: dict[str, str]) -> bool:
    return (
        answers.get("dependents", "")
        in ("Extended family", "I support others regularly")
        or answers.get("shortage_cause", "") == "I support other people"
    )


def _debt_heavy(answers: dict[str, str]) -> bool:
    return answers.get("debt_probe", "") in (
        "A noticeable amount",
        "A lot, I would rather not look",
    )


def _spending_leak(answers: dict[str, str]) -> bool:
    return answers.get(
        "shortage_cause", ""
    ) == "I spend more than I plan" or answers.get("behavior_probe", "") in (
        "Small things add up",
        "I don't track it closely",
        "Stress or celebrating",
    )


def _wants_wealth(answers: dict[str, str]) -> bool:
    return answers.get("goal_direction", "") == "Build wealth"


def _seeking_stability(answers: dict[str, str]) -> bool:
    return answers.get("goal_direction", "") == "Stop the stress"


def _automate(answers: dict[str, str]) -> bool:
    return answers.get("involvement", "") in (
        "Set it up for me",
        "Suggest things, I'll do it",
    )


def _track(answers: dict[str, str]) -> bool:
    return answers.get("involvement", "") in (
        "Set it up for me",
        "Suggest things, I'll do it",
        "Just track and guide",
    )


def diagnostic_state(answers: dict[str, str]) -> str:
    """Primary label for the user's money situation, most urgent first."""
    if _short_runway(answers):
        return "Stability Seeker"
    if _income_unpredictable(answers) or _leans_on_credit(answers):
        return "Volatile Earner"
    if _wants_wealth(answers) and _stable_runway(answers):
        return "Wealth Builder"
    return "Financial Beginner"


def overlays(answers: dict[str, str]) -> list[str]:
    """Secondary signals that refine the diagnostic label."""
    out: list[str] = []
    if _debt_heavy(answers):
        out.append("debt_burden")
    if _has_dependents(answers):
        out.append("family_obligations")
    if _spending_leak(answers):
        out.append("spending_leakage")
    if _seeking_stability(answers):
        out.append("goal_urgency")
    return out


def build_plan(
    answers: dict[str, str], document_summary: str | None = None
) -> dict[str, Any]:
    """Build the full plan dict: diagnosis, steps, standing rules, copy.

    This is pure and deterministic so the service can persist it verbatim and
    tests can assert exact rule sets.
    """
    state_name = diagnostic_state(answers)
    overlays_list = overlays(answers)
    automate = _automate(answers)
    track = _track(answers)

    steps: list[dict[str, str]] = []

    # 1. Safety buffer — every user needs one; the plan scales urgency off the
    # runway answer and the volatility read.
    if _short_runway(answers) or _income_unpredictable(answers):
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

    # 2. Flexible income rhythm — volatility needs a matching income pattern.
    if _income_unpredictable(answers) or _leans_on_credit(answers):
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

    # 3. Family obligations — fixed envelope, never the flexible line item.
    if _has_dependents(answers):
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

    # 4. Debt — contained, ordered, and never funding everyday life.
    if _debt_heavy(answers) or _leans_on_credit(answers):
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

    # 5. Spending guard — defaults beat discipline.
    if _spending_leak(answers):
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

    # 6. Forward goal — named, automatic, first.
    goal = answers.get("goal_direction", "").lower()
    if goal and goal != "not sure yet":
        steps.append(
            {
                "id": "goal",
                "kind": "goal",
                "title": "Fund the goal first",
                "detail": (
                    "Move a set slice of every income toward "
                    f"'{goal}' before anything else gets its turn."
                ),
            }
        )

    # 7. Weekly check-in — the confidence layer.
    if track or _seeking_stability(answers):
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

    # Standing rules — only for hands-on involvement; always non-destructive.
    standing_rules: list[dict[str, str]] = []
    if automate:
        save_target = (
            "the month-ahead buffer"
            if _short_runway(answers)
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
        if _income_unpredictable(answers) or _leans_on_credit(answers):
            standing_rules.append(
                {
                    "kind": "income_rhythm",
                    "trigger": "each lump arrives",
                    "action": "split it into smaller paycheck-sized pieces",
                    "cadence": "on inflow",
                }
            )
        if _has_dependents(answers):
            standing_rules.append(
                {
                    "kind": "obligations",
                    "trigger": "monthly",
                    "action": "fund the fixed family envelope first",
                    "cadence": "monthly",
                }
            )
        if _debt_heavy(answers):
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
        if _spending_leak(answers):
            standing_rules.append(
                {
                    "kind": "spending_guard",
                    "trigger": "before any big purchase",
                    "action": "pause 24 hours and check it against the month",
                    "cadence": "on purchase",
                }
            )
        if goal and goal != "not sure yet":
            standing_rules.append(
                {
                    "kind": "goal",
                    "trigger": "income lands",
                    "action": f"move a set slice toward '{goal}' first",
                    "cadence": "on inflow",
                }
            )

    evidence = [answers.get(q) for q in ("money_feelings", "liquidity_runway")]
    evidence = [e for e in evidence if e]

    summary = _summary_text(state_name, overlays_list, document_summary, goal)

    return {
        "diagnostic_state": state_name,
        "overlays": overlays_list,
        "steps": steps,
        "standing_rules": standing_rules,
        "automate": automate,
        "source": "onboarding_interview",
        "statement_summary": document_summary,
        "evidence": evidence,
        "summary": summary,
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
    text += "."
    return text

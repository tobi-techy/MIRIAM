"""Deterministic financial diagnosis engine (spec §9).

Miriam's job is not to describe a user's money, it is to *diagnose* it: name the
structural cause, rank the runners-up, and say how sure she is. This module owns
that read. It is pure, table-driven and testable -- the LLM narrates the result,
it never produces it (§2, §29).

Three inputs, all optional, combined in one pass:

  - the structured :class:`~miriam_agent.financial.profile.FinancialProfile`
    (what the user told us, with provenance)
  - a ledger-backed snapshot, which is run through the *existing*
    :func:`~miriam_agent.financial.intelligence.compute_financial_health`
    rather than re-derived here (§36: do not duplicate)
  - free text, which is run through the *existing* hypothesis engine
    (:mod:`miriam_agent.financial.hypotheses`) so spoken evidence sharpens the
    same ranking the numbers produce

A user may hold several problems at once, so the engine always returns a ranked
set: one ``primary_problem``, an ordered ``secondary_problems`` list, the
``scores`` behind them (auditable), the ``evidence`` that drove each, and the
concrete ``priorities`` the allocation engine should act on first.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from miriam_agent.financial.profile import (
    PROFILE_FIELDS,
    FinancialProfile,
    horizon_months,
)

# ---------------------------------------------------------------------------
# Vocabulary (spec §9)
# ---------------------------------------------------------------------------

PROBLEMS: tuple[str, ...] = (
    "no_financial_system",
    "cashflow_imbalance",
    "excessive_discretionary_spending",
    "inconsistent_saving",
    "lack_of_emergency_buffer",
    "high_debt_burden",
    "income_volatility",
    "idle_cash",
    "investment_gap",
    "unclear_financial_goals",
    "insufficient_financial_knowledge",
    "strong_financial_foundation",
    "investment_ready",
    "not_yet_investment_ready",
)

# States that are *good* news. They rank beside the problems (a user can be
# both "investment_ready" and "no_financial_system"), but the narrator has to be
# able to tell them apart, so the engine labels them.
POSITIVE_PROBLEMS: frozenset[str] = frozenset(
    {"strong_financial_foundation", "investment_ready", "idle_cash"}
)

# A problem below this score is not worth telling the user about.
SECONDARY_FLOOR = 0.35

# Action keys, one per problem, consumed by the allocation engine (§11) and the
# Rail-value moment (§12). The order a user reads them in matters.
PRIORITY_FOR_PROBLEM: dict[str, str] = {
    "no_financial_system": "create_default_allocation",
    "cashflow_imbalance": "fix_cashflow_gap",
    "excessive_discretionary_spending": "cap_discretionary",
    "inconsistent_saving": "automate_saving_on_inflow",
    "lack_of_emergency_buffer": "build_emergency_buffer",
    "high_debt_burden": "clear_high_cost_debt",
    "income_volatility": "smooth_irregular_income",
    "idle_cash": "put_idle_cash_to_work",
    "investment_gap": "start_investing_small",
    "unclear_financial_goals": "define_goal_and_horizon",
    "insufficient_financial_knowledge": "learn_the_basics",
    "strong_financial_foundation": "keep_the_system_simple",
    "investment_ready": "automate_the_investment",
    "not_yet_investment_ready": "build_the_foundation_first",
}

# Spoken evidence -> the problem it corroborates. Mirrors the hypothesis
# catalogue in ``financial/hypotheses.py``; the hypothesis engine finds the
# cause in the user's words, this map lands it on the same ranking the numbers
# feed, so one problem is not scored twice under two names.
_HYPOTHESIS_TO_PROBLEM: dict[str, str] = {
    "lack_of_visibility": "no_financial_system",
    "lack_of_allocation_system": "no_financial_system",
    "overspending": "excessive_discretionary_spending",
    "lifestyle_inflation": "excessive_discretionary_spending",
    "impulse_spending": "excessive_discretionary_spending",
    "unexpected_expenses": "cashflow_imbalance",
    "family_obligations": "cashflow_imbalance",
    "high_fixed_expenses": "cashflow_imbalance",
    "broke_before_payday": "cashflow_imbalance",
    "insufficient_income": "cashflow_imbalance",
    "debt": "high_debt_burden",
    "irregular_income": "income_volatility",
}

# How much a confident spoken cue is worth on top of the numeric score. Capped
# by ``_MAX_SCORE`` so words alone never reach certainty.
_TEXT_BOOST = 0.25

_MONTHLY_FACTOR = {"daily": 30.4375, "weekly": 4.345, "monthly": 1.0, "yearly": 1 / 12}

# A goal this thin is not a goal yet (spec §13: "I want to save more" is not a
# goal until we know what it buys, when, and how much).
_VAGUE_GOALS = re.compile(
    r"^(save|saving|save more|more money|money|be rich|rich|wealth|"
    r"invest|investing|grow my money|get better|be better|budget|"
    r"financial freedom|freedom)\.?$",
    re.IGNORECASE,
)


class Diagnosis(BaseModel):
    """The ranked financial read (spec §9).

    ``scores`` is the full audit trail: every problem the engine considered and
    the number it earned. A user never sees it, but a test, an eval and a
    support engineer all can -- which is what makes the diagnosis
    falsifiable instead of a vibe.
    """

    model_config = ConfigDict(extra="forbid")

    primary_problem: str
    secondary_problems: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    is_positive: bool = False
    known_fields: int = 0
    assumptions: list[str] = Field(default_factory=list)

    def problems(self) -> list[str]:
        """Primary first, then the ranked secondaries."""
        return [self.primary_problem, *self.secondary_problems]

    def top_action(self) -> str:
        """The single action key Miriam should lead with."""
        return self.priorities[0] if self.priorities else ""

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class _Signals(BaseModel):
    """Everything the rules read, resolved once so every rule sees the same
    numbers (and so an absent value is a real ``None``, never a silent 0)."""

    income: float | None = None
    essentials: float | None = None
    discretionary: float | None = None
    debt: float | None = None
    savings: float | None = None
    buffer: float | None = None
    surplus: float | None = None
    surplus_ratio: float | None = None
    buffer_months: float | None = None
    debt_income_months: float | None = None
    volatile: bool = False
    has_goal: bool = False
    vague_goal: bool = False
    horizon: int | None = None
    goal_text: str = ""
    # Whether goal discovery has actually happened yet. Before FIRST_AHA the
    # flow has not asked (spec §4 orders GOAL_DISCOVERY *after* the aha), so a
    # missing goal is a gap in our knowledge, not a finding about the user.
    goal_expected: bool = True
    experience: str = ""
    spending_behavior: str = ""
    saving_behavior: str = ""
    investing_behavior: str = ""
    stressed: bool = False
    health_score: float | None = None
    health_status: str = ""
    savings_rate: float | None = None
    budget_status: str = ""
    hypotheses: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)


def _monthly(profile: FinancialProfile, field: str) -> float | None:
    """A money field normalized to a monthly figure.

    Profile amounts stay as the user said them ("500k a week" is stored
    weekly); every calculation downstream works monthly, so the conversion
    happens here once instead of in each rule.
    """
    amount = profile.money(field)
    if amount is None:
        return None
    frequency = profile.value("income_frequency")
    if field != "income_amount":
        return amount
    factor = _MONTHLY_FACTOR.get(str(frequency or "monthly").casefold(), 1.0)
    return amount * factor


def _s(value: Any) -> str:
    return str(value).strip().casefold() if value is not None else ""


def _resolve(
    profile: FinancialProfile,
    health: dict[str, Any] | None,
    *,
    goal_expected: bool = True,
) -> _Signals:
    sig = _Signals()
    sig.goal_expected = goal_expected
    sig.income = _monthly(profile, "income_amount")
    sig.essentials = _monthly(profile, "essential_expenses")
    sig.discretionary = _monthly(profile, "discretionary_expenses")
    sig.debt = profile.money("debt")
    sig.savings = profile.money("savings")
    sig.buffer = profile.money("emergency_fund")
    if sig.buffer is None and sig.savings is not None:
        sig.buffer = sig.savings
        sig.assumptions.append(
            "no separate emergency fund recorded; savings is treated as the buffer"
        )

    if sig.income is not None and sig.essentials is not None:
        sig.surplus = sig.income - sig.essentials
        if sig.income > 0:
            sig.surplus_ratio = sig.surplus / sig.income
    if sig.buffer is not None and sig.essentials and sig.essentials > 0:
        sig.buffer_months = sig.buffer / sig.essentials
    if sig.debt is not None and sig.income and sig.income > 0:
        sig.debt_income_months = sig.debt / sig.income

    sig.volatile = profile.volatile_income()
    goal = profile.value("financial_goal")
    sig.has_goal = bool(isinstance(goal, str) and goal.strip())
    sig.goal_text = str(goal).strip() if sig.has_goal else ""
    sig.vague_goal = bool(sig.has_goal and _VAGUE_GOALS.match(sig.goal_text))
    sig.horizon = horizon_months(profile)
    sig.experience = _s(profile.value("investment_experience"))
    sig.spending_behavior = _s(profile.value("spending_behavior"))
    sig.saving_behavior = _s(profile.value("saving_behavior"))
    sig.investing_behavior = _s(profile.value("investing_behavior"))
    sig.stressed = _s(profile.value("financial_stress")) == "high"

    if health:
        try:
            sig.health_score = float(health.get("score"))
        except (TypeError, ValueError):
            sig.health_score = None
        sig.health_status = _s(health.get("status"))
        try:
            sig.savings_rate = float(health.get("savings_rate_pct"))
        except (TypeError, ValueError):
            sig.savings_rate = None
        sig.budget_status = _s(health.get("budget_status"))
    return sig


# ---------------------------------------------------------------------------
# Rules
#
# One evaluator per §9 problem. Each returns ``(score, evidence)`` where score
# is 0..1 and evidence is the plain-language reason -- the same sentences the
# narrator is allowed to use, so every claim in Miriam's reply traces back to a
# signal the user actually gave (spec §10, "never fabricate numbers").
# ---------------------------------------------------------------------------

_MAX_SCORE = 1.0

_FOUNDATION_SURPLUS = 0.15
_FOUNDATION_BUFFER_MONTHS = 3.0
_FOUNDATION_DEBT_MONTHS = 3.0


def _pct(part: float, whole: float) -> str:
    return f"{(part / whole) * 100:.0f}%"


def _money(value: float) -> str:
    return f"{value:,.0f}"


def _clamp(value: float) -> float:
    return round(max(0.0, min(_MAX_SCORE, value)), 3)


def _foundation_ok(s: _Signals) -> bool:
    """The three conditions readiness (§15) and the positive states share:
    a real surplus, a real buffer, and no crushing debt."""
    if s.surplus_ratio is None or s.surplus_ratio < _FOUNDATION_SURPLUS:
        return False
    if s.buffer_months is None or s.buffer_months < _FOUNDATION_BUFFER_MONTHS:
        return False
    if (
        s.debt_income_months is not None
        and s.debt_income_months >= _FOUNDATION_DEBT_MONTHS
    ):
        return False
    return True


def _rule_no_financial_system(s: _Signals) -> tuple[float, list[str]]:
    score, ev = 0.0, []
    if s.saving_behavior in ("", "inconsistent", "none"):
        score += 0.3
        ev.append("no consistent saving habit recorded")
    if s.spending_behavior == "untracked":
        score += 0.25
        ev.append("spending is untracked")
    if s.buffer_months is None:
        score += 0.2
        ev.append("no buffer on record")
    elif s.buffer_months < 1:
        score += 0.25
        ev.append(f"the buffer covers {s.buffer_months:.1f} months")
    if not s.has_goal and s.goal_expected:
        score += 0.15
        ev.append("no financial goal set")
    return score, ev


def _rule_cashflow_imbalance(s: _Signals) -> tuple[float, list[str]]:
    if s.surplus is None or s.income is None or s.essentials is None:
        return 0.0, []
    if s.surplus <= 0:
        return 0.95, [
            f"monthly outflow ({_money(s.essentials)}) meets or beats "
            f"income ({_money(s.income)})"
        ]
    ratio = s.surplus_ratio or 0.0
    if ratio < 0.05:
        return 0.9, [f"only {_pct(s.surplus, s.income)} of income is left unspoken for"]
    if ratio < 0.15:
        return 0.65, [f"only {_pct(s.surplus, s.income)} of income is left unspoken for"]
    if ratio < 0.25:
        return 0.35, [f"{_pct(s.surplus, s.income)} of income is left unspoken for"]
    return 0.1, []


def _rule_excessive_discretionary_spending(s: _Signals) -> tuple[float, list[str]]:
    score, ev = 0.0, []
    if s.spending_behavior == "discretionary_heavy":
        score += 0.6
        ev.append("they described their own spending as overspending")
    if s.discretionary is not None and s.income:
        share = s.discretionary / s.income
        if share > 0.35:
            score += 0.35
            ev.append(f"discretionary spending is {share * 100:.0f}% of income")
        elif share > 0.25:
            score += 0.2
            ev.append(f"discretionary spending is {share * 100:.0f}% of income")
    if s.budget_status == "over_budget":
        score += 0.3
        ev.append("the spending budget is already over")
    return score, ev


def _rule_inconsistent_saving(s: _Signals) -> tuple[float, list[str]]:
    if s.saving_behavior == "automatic":
        return 0.05, ["saving already runs automatically"]
    score, ev = 0.0, []
    if s.saving_behavior == "inconsistent":
        score += 0.65
        ev.append("saving is described as inconsistent")
    elif not s.saving_behavior:
        score += 0.25
        ev.append("no saving habit recorded")
    if s.savings_rate is not None and s.savings_rate < 10:
        score += 0.3
        ev.append(f"the measured savings rate is {s.savings_rate:.0f}%")
    if s.savings is not None and s.income and s.savings < s.income * 0.1:
        score += 0.2
        ev.append("savings are under a month's income")
    return score, ev


def _rule_lack_of_emergency_buffer(s: _Signals) -> tuple[float, list[str]]:
    months = s.buffer_months
    if months is None:
        if s.buffer is None:
            return 0.6, ["no emergency fund recorded"]
        return 0.4, [
            "a buffer exists, but essentials are unknown so months cannot be computed"
        ]
    target = 6.0 if s.volatile else 3.0
    if months < 1:
        return 0.95, [f"the buffer covers {months:.1f} months"]
    if months < 3:
        return 0.7, [f"the buffer covers {months:.1f} months"]
    if months < target:
        return 0.4, [
            f"the buffer covers {months:.1f} months, and volatile income wants "
            f"{target:.0f}"
        ]
    return 0.05, [f"the buffer covers {months:.1f} months"]


def _rule_high_debt_burden(s: _Signals) -> tuple[float, list[str]]:
    months = s.debt_income_months
    if months is None:
        if s.debt and s.debt > 0:
            return 0.4, [
                "debt is recorded, but income is unknown so the burden cannot be sized"
            ]
        return 0.0, []
    if months >= 12:
        return 0.95, [f"debt is {months:.0f}x monthly income"]
    if months >= 6:
        return 0.8, [f"debt is {months:.0f}x monthly income"]
    if months >= 3:
        return 0.6, [f"debt is {months:.0f}x monthly income"]
    if months >= 1:
        return 0.3, [f"debt is {months:.1f}x monthly income"]
    return 0.05, []


def _rule_income_volatility(s: _Signals) -> tuple[float, list[str]]:
    if s.volatile:
        return 0.85, ["income is not steady"]
    return 0.05, []


def _rule_idle_cash(s: _Signals) -> tuple[float, list[str]]:
    if (
        s.buffer_months is not None
        and s.buffer_months >= 6
        and s.experience in ("", "none")
    ):
        return 0.75, [
            f"the buffer covers {s.buffer_months:.1f} months and nothing is invested"
        ]
    if (
        s.investing_behavior == "none"
        and s.savings_rate is not None
        and s.savings_rate >= 30
    ):
        return 0.7, [
            f"a {s.savings_rate:.0f}% savings rate is piling up uninvested cash"
        ]
    return 0.0, []


def _rule_investment_gap(s: _Signals) -> tuple[float, list[str]]:
    if s.experience not in ("", "none") or not _foundation_ok(s):
        return 0.1, []
    if s.horizon == 6:
        return 0.3, ["the foundation is there, but the goal is too near-term to invest"]
    return 0.7, ["the foundation is in place and nothing is being invested yet"]


def _rule_unclear_financial_goals(s: _Signals) -> tuple[float, list[str]]:
    if not s.has_goal:
        if not s.goal_expected:
            # The aha comes before goal discovery (spec §4), so not having a goal
            # yet is not a finding -- it is simply the next question.
            return 0.2, ["the goal has not been set yet"]
        return 0.7, ["no financial goal is set"]
    if s.vague_goal:
        return 0.5, ["the goal is too vague to plan against"]
    if s.horizon is None:
        return 0.3, ["the goal has no timeframe"]
    return 0.05, []


def _rule_insufficient_financial_knowledge(s: _Signals) -> tuple[float, list[str]]:
    score, ev = 0.0, []
    if s.stressed:
        score += 0.3
        ev.append("money worry is running high")
    if s.spending_behavior == "untracked":
        score += 0.25
        ev.append("spending is untracked")
    if s.saving_behavior in ("", "inconsistent"):
        score += 0.2
        ev.append("no saving habit recorded")
    if s.experience in ("", "none"):
        score += 0.15
        ev.append("no investing experience")
    # Deliberately capped below the concrete leaks: a knowledge gap is real, but
    # it is never the reason money is disappearing, so it must never outrank a
    # specific, fixable cause.
    return min(score, 0.6), ev


def _rule_strong_financial_foundation(s: _Signals) -> tuple[float, list[str]]:
    score, ev = 0.0, []
    if s.surplus_ratio is not None:
        if s.surplus_ratio >= 0.25:
            score += 0.35
            ev.append(f"surplus is {s.surplus_ratio * 100:.0f}% of income")
        elif s.surplus_ratio >= _FOUNDATION_SURPLUS:
            score += 0.2
            ev.append(f"surplus is {s.surplus_ratio * 100:.0f}% of income")
    if s.buffer_months is not None and s.buffer_months >= 3:
        score += 0.3
        ev.append(f"the buffer covers {s.buffer_months:.1f} months")
    if s.debt_income_months is None or s.debt_income_months < 1:
        score += 0.2
        ev.append("no meaningful debt")
    if s.has_goal and not s.vague_goal:
        score += 0.15
        ev.append("a clear goal is set")
    if s.saving_behavior == "automatic":
        score += 0.1
        ev.append("saving already runs automatically")
    if s.health_score is not None and s.health_score >= 80:
        score += 0.15
        ev.append(f"the ledger health score is {s.health_score:.0f}")
    return score, ev


def _rule_investment_ready(s: _Signals) -> tuple[float, list[str]]:
    if not _foundation_ok(s):
        return 0.0, []
    if s.horizon == 6:
        return 0.2, ["investing is affordable, but the horizon is short"]
    score = 0.6
    ev = ["surplus, buffer and horizon line up for investing"]
    if s.horizon in (24, 60):
        score += 0.2
    if s.saving_behavior == "automatic":
        score += 0.15
    return min(score, 0.9), ev


def _rule_not_yet_investment_ready(s: _Signals) -> tuple[float, list[str]]:
    interested = (
        s.experience in ("intermediate", "advanced")
        or s.investing_behavior == "active"
        or "invest" in s.goal_text.casefold()
    )
    if not interested or _foundation_ok(s):
        return 0.0, []
    return 0.75, ["they want to invest, but the foundation is not in place yet"]


# The full catalogue, in tie-break order: when two problems score identically,
# the earlier one is the more fundamental cause (a missing system beats a
# spending leak; a cashflow gap beats a missing goal).
_RULES: tuple[tuple[str, Callable[[_Signals], tuple[float, list[str]]]], ...] = (
    ("no_financial_system", _rule_no_financial_system),
    ("cashflow_imbalance", _rule_cashflow_imbalance),
    ("high_debt_burden", _rule_high_debt_burden),
    ("lack_of_emergency_buffer", _rule_lack_of_emergency_buffer),
    ("excessive_discretionary_spending", _rule_excessive_discretionary_spending),
    ("inconsistent_saving", _rule_inconsistent_saving),
    ("income_volatility", _rule_income_volatility),
    ("unclear_financial_goals", _rule_unclear_financial_goals),
    ("insufficient_financial_knowledge", _rule_insufficient_financial_knowledge),
    ("investment_gap", _rule_investment_gap),
    ("idle_cash", _rule_idle_cash),
    ("not_yet_investment_ready", _rule_not_yet_investment_ready),
    ("investment_ready", _rule_investment_ready),
    ("strong_financial_foundation", _rule_strong_financial_foundation),
)

_TIE_BREAK: dict[str, int] = {code: i for i, (code, _) in enumerate(_RULES)}


# ---------------------------------------------------------------------------
# The diagnosis
# ---------------------------------------------------------------------------


def _text_boosts(text: str) -> dict[str, float]:
    """Corroboration from what the user said, via the existing hypothesis
    engine. Returns problem -> confidence, already mapped onto the §9
    vocabulary, so one cause is never scored twice under two names."""
    if not text or not text.strip():
        return {}
    try:
        from miriam_agent.financial.hypotheses import rank_hypotheses

        ranked = rank_hypotheses(text)
    except Exception:
        return {}
    boosts: dict[str, float] = {}
    for hypothesis in ranked:
        problem = _HYPOTHESIS_TO_PROBLEM.get(hypothesis.code)
        if not problem:
            continue
        boosts[problem] = max(boosts.get(problem, 0.0), hypothesis.confidence)
    return boosts


def diagnose(
    profile: FinancialProfile,
    *,
    snapshot: dict[str, Any] | None = None,
    health: dict[str, Any] | None = None,
    text: str = "",
    max_secondary: int = 3,
    goal_expected: bool = True,
) -> Diagnosis:
    """Rank the user's financial problems (spec §9).

    Order of operations, all deterministic:

      1. Resolve one set of signals from the profile (+ health, + spoken text).
      2. Score every §9 problem with its own rule, collecting evidence.
      3. Add corroboration from the user's own words (bounded), so a clearly
         described leak is not outranked by a thin numeric read -- and so words
         alone can never reach certainty.
      4. Rank, take the top as ``primary_problem``, the next ones above
         :data:`SECONDARY_FLOOR` as ``secondary_problems``.
      5. Derive ``confidence`` from how much we actually know *and* how far the
         winner is ahead of the runner-up.

    ``snapshot`` is an optional ledger-backed snapshot; when given it is run
    through the existing health engine rather than re-derived here.

    ``goal_expected`` says whether goal discovery has already happened. The
    flow is ordered ``FINANCIAL_DIAGNOSIS -> FIRST_AHA -> GOAL_DISCOVERY``
    (spec §4), so at the aha the goal legitimately does not exist yet and its
    absence must not dominate the read -- pass ``False`` for the first
    diagnosis and ``True`` afterwards.
    """
    if snapshot is not None and health is None:
        try:
            from miriam_agent.financial.intelligence import compute_financial_health

            health = compute_financial_health(snapshot)
        except Exception:
            health = None

    signals = _resolve(profile, health, goal_expected=goal_expected)
    signals.hypotheses = _text_boosts(text)

    scores: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}
    for code, rule in _RULES:
        try:
            score, reasons = rule(signals)
        except Exception:
            score, reasons = 0.0, []
        if code in signals.hypotheses and score > 0:
            score = min(_MAX_SCORE, score + _TEXT_BOOST * signals.hypotheses[code])
            reasons = [
                *reasons,
                "they described this themselves in the conversation",
            ]
        scores[code] = _clamp(score)
        evidence[code] = [r for r in reasons if r][:3]

    ranked = sorted(
        scores.items(),
        key=lambda item: (-item[1], _TIE_BREAK.get(item[0], len(_RULES))),
    )
    primary_code, primary_score = ranked[0]
    secondaries = [
        code
        for code, score in ranked[1 : max_secondary + 1]
        if score >= SECONDARY_FLOOR
    ]

    known = len(profile.known())
    coverage = known / len(PROFILE_FIELDS)
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    # Confidence rewards knowing a lot AND a clear winner: a diagnosis nobody
    # could disagree with because it is separated from the runner-up is worth
    # more than a coin-flip between two equal reads.
    separation = min(1.0, max(0.0, primary_score - runner_up) / 0.5)
    confidence = round(
        max(0.3, min(0.95, 0.45 * coverage + 0.3 * primary_score + 0.25 * separation)),
        2,
    )

    priorities: list[str] = []
    for code in [primary_code, *secondaries]:
        action = PRIORITY_FOR_PROBLEM.get(code)
        if action and action not in priorities:
            priorities.append(action)

    return Diagnosis(
        primary_problem=primary_code,
        secondary_problems=secondaries,
        confidence=confidence,
        evidence=evidence.get(primary_code, []),
        priorities=priorities,
        scores=scores,
        is_positive=primary_code in POSITIVE_PROBLEMS,
        known_fields=known,
        assumptions=signals.assumptions,
    )


# The minimum the engine needs for a useful read (spec §8: ask the least that
# produces a diagnosis). Income + outflow + goal is enough for the first aha.
MINIMUM_FIELDS: tuple[str, ...] = ("income_amount", "essential_expenses")


def missing_for_diagnosis(profile: FinancialProfile) -> list[str]:
    """What we still need before the diagnosis is worth showing a user."""
    return profile.missing(MINIMUM_FIELDS)


def ready_to_diagnose(profile: FinancialProfile) -> bool:
    """True when there is enough to say something true and specific."""
    return not missing_for_diagnosis(profile)
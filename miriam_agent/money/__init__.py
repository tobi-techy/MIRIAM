"""Miriam's money pipeline: intake -> diagnose -> safety -> cashflow -> book ->
Glider decision -> MoneyPlan.

Deterministic by construction. Python computes every number; the LLM layer in
``agent.py`` only writes prose about numbers it was handed, and is clamped if it
tries to introduce one.

Rules live in ``docs/MONEY-RULES.md``; the output contract lives in
``docs/MONEY-PLAN-CONTRACT.md``. Scope: read, diagnose, recommend. Money movement
belongs to RAIL_BACKEND, and enrollment is user-signed and two-stage.
"""

from miriam_agent.money.agent import (
    NarrationReport,
    build_and_narrate,
    check_plan_contradiction,
    narrate,
    narrate_with_report,
)
from miriam_agent.money.allocation import Capacity, build_book, score_capacity
from miriam_agent.money.cashflow import build_cashflow, render_split
from miriam_agent.money.diagnose import Diagnosis, diagnose
from miriam_agent.money.glider import build_draft, decide_glider, drift_from_target
from miriam_agent.money.intake import (
    AccountsSnapshot,
    Debt,
    Goal,
    IntakeProfile,
    apply_accounts,
    from_financial_profile,
    from_mapping,
    from_onboarding_state,
)
from miriam_agent.money.plan import (
    GliderState,
    Pipeline,
    build_money_plan,
    build_plan,
    coerce_profile,
    explain_money_plan,
    render_plan,
    render_spoken,
)
from miriam_agent.money.reference import (
    CountryReference,
    ReferenceStatus,
    available_countries,
    lookup,
    reference_from_env,
    reference_status,
    resolve_thresholds,
)
from miriam_agent.money.safety import SafetyStack, assess_safety
from miriam_agent.money.schema import (
    PROBLEM_TYPES,
    Action,
    AllocationBook,
    BufferPlan,
    CashflowSplit,
    DebtAction,
    DraftWeight,
    GliderAction,
    GliderDraft,
    MoneyPlan,
)
from miriam_agent.money.templates import (
    MIRIAM_BUILD,
    MIRIAM_CORE,
    MIRIAM_PRESERVE,
    StrategyTemplate,
    allocation_payload,
    select_template,
    template_weights,
    validate_weights,
)

__all__ = [
    "PROBLEM_TYPES",
    "AccountsSnapshot",
    "Action",
    "AllocationBook",
    "BufferPlan",
    "Capacity",
    "CashflowSplit",
    "CountryReference",
    "Debt",
    "DebtAction",
    "Diagnosis",
    "DraftWeight",
    "GliderAction",
    "GliderDraft",
    "GliderState",
    "Goal",
    "IntakeProfile",
    "MIRIAM_BUILD",
    "MIRIAM_CORE",
    "MIRIAM_PRESERVE",
    "MoneyPlan",
    "NarrationReport",
    "Pipeline",
    "ReferenceStatus",
    "SafetyStack",
    "StrategyTemplate",
    "allocation_payload",
    "apply_accounts",
    "assess_safety",
    "available_countries",
    "build_and_narrate",
    "build_book",
    "build_cashflow",
    "build_draft",
    "build_money_plan",
    "build_plan",
    "check_plan_contradiction",
    "coerce_profile",
    "decide_glider",
    "diagnose",
    "drift_from_target",
    "explain_money_plan",
    "from_financial_profile",
    "from_mapping",
    "from_onboarding_state",
    "lookup",
    "narrate",
    "narrate_with_report",
    "reference_from_env",
    "reference_status",
    "render_plan",
    "render_spoken",
    "render_split",
    "resolve_thresholds",
    "score_capacity",
    "select_template",
    "template_weights",
    "validate_weights",
]

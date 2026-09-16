"""Typed contracts for the onboarding conductor and the state it persists.

Top agentic-company practice: the LLM's output surface and the state we persist
are versioned pydantic models, not ad-hoc dicts. Every model is
``extra="forbid"`` so a model reply that invents fields fails loudly at the
boundary instead of silently corrupting Redis state.

Two layers live here:

  - :class:`ConductorOutcome` / :class:`PresentPlanOutcome`: the LLM turn
    surface. The driver validates what the model returned (via the
    ``emit_conductor_outcome`` / ``emit_plan_presentation`` tool calls, or the
    free-text JSON fallback) through these before anything touches state.

  - :class:`MoneyMomentMeta` / :class:`GoalMeta` / :class:`ConversationState`
    / :class:`Plan`: the structured state the service persists. The service
    re-validates on every write so what goes into Redis is always a known
    schema, and schema drift breaks tests, not production.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

# The one intent the conductor may report per turn. The stage whitelist in the
# driver stays the hard gate on top of this: an intent that is legal in the
# spec but not in the current stage is dropped for the stage's safe default.
Intent = Literal[
    "interview",
    "request_statement",
    "present_plan",
    "consent_yes",
    "consent_no",
    "adjust",
    "done_adjusting",
    "abandon",
]

DiagnosticState = Literal[
    "Stability Seeker",
    "Volatile Earner",
    "Wealth Builder",
    "Financial Beginner",
]

Severity = Literal["high", "medium", "low"]

TOOL_NAME_CONDUCTOR = "emit_conductor_outcome"
TOOL_NAME_PRESENT = "emit_plan_presentation"

_MAX_TAPS = 4


class ConductorOutcome(BaseModel):
    """The structured turn output from the conductor LLM.

    Bounds (taps, facts, reply length) are enforced deterministically by the
    driver sanitizers so a well-behaved tool always rounds-trips; the contract
    here pins the *shape*.
    """

    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1)
    suggested_replies: list[str] = Field(default_factory=list)
    facts: dict[str, str] = Field(default_factory=dict)
    intent: str = "interview"
    adjustment: str = ""
    reaction: str = ""


class PresentPlanOutcome(BaseModel):
    """The structured output from the plan-presenter LLM turn."""

    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1)
    reaction: str = ""


class MoneyMomentMeta(BaseModel):
    """spec v1.2 §6: the structured read of the money moment, lifted from the
    reserved fact keys. Persisted only when non-default (exclude_defaults)."""

    model_config = ConfigDict(extra="forbid")

    emotion: str = ""
    suspected_problem: str = ""
    confidence: float | None = None


class GoalMeta(BaseModel):
    """spec v1.2 §7: the structured read of the goal, lifted from the reserved fact
    keys. Persisted only when non-default (exclude_defaults)."""

    model_config = ConfigDict(extra="forbid")

    target_date: str = ""
    estimated_cost: str = ""
    priority: str = ""
    funding_status: str = ""


class ConversationState(BaseModel):
    """spec v1.2 §29: the deterministic, backend-owned read of where the
    conversation is. Mirrors the state seeds in :mod:`state` plus the internal
    money script (for steering only, never shown)."""

    model_config = ConfigDict(extra="forbid")

    current_topic: str = ""
    user_goal: str = ""
    current_problem: str = ""
    confidence: float | None = None
    last_insight: str = ""
    pending_action: str = ""
    user_sentiment: str = ""
    relationship_stage: str = "new"
    directness_level: int = Field(default=1, ge=1, le=4)
    money_script: str = ""


class RecommendedAction(BaseModel):
    """The one next move the insight points to (amount stays null until real
    account data exists)."""

    model_config = ConfigDict(extra="forbid")

    type: str
    amount: float | None = None
    timing: str = ""


class FinancialInsight(BaseModel):
    """spec v1.2 §21: one structured financial insight per plan."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["financial_insight"] = "financial_insight"
    category: str
    title: str
    summary: str = ""
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    financial_impact: float | None = None
    evidence: list[str] = Field(default_factory=list)
    recommended_action: RecommendedAction


class Step(BaseModel):
    """One move in the deterministic plan."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    title: str
    detail: str = ""


class StandingRule(BaseModel):
    """One consent-gated automation bullet (only exists for hands-on users)."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    trigger: str
    action: str
    cadence: str


class Plan(BaseModel):
    """The full deterministic plan: diagnosis, overlays, insight, steps,
    consent-gated standing rules, and the copy. Pure builder output, so the
    service validates it at the write boundary and persists the model dump."""

    model_config = ConfigDict(extra="forbid")

    diagnostic_state: DiagnosticState
    overlays: list[str] = Field(default_factory=list)
    insight: FinancialInsight
    steps: list[Step] = Field(default_factory=list)
    standing_rules: list[StandingRule] = Field(default_factory=list)
    automate: bool = False
    source: str = "onboarding_interview"
    statement_summary: str | None = None
    evidence: list[str] = Field(default_factory=list)
    summary: str = ""
    adjustments: list[str] = Field(default_factory=list)


def _function_schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    """Build an OpenAI-style function tool schema for one structured call."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def conductor_tool() -> dict[str, Any]:
    """The one tool the conductor may call to emit its turn outcome."""
    return _function_schema(
        TOOL_NAME_CONDUCTOR,
        "Emit the turn's structured outcome: Miriam's reply, any tap options, "
        "the facts newly learned, the intent, and any plan adjustment.",
        {
            "reply": {
                "type": "string",
                "description": "Miriam's reply to the user, in her own voice.",
            },
            "suggested_replies": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": _MAX_TAPS,
                "description": (
                    "Optional short tap options, or empty when a full message "
                    "matters."
                ),
            },
            "facts": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Only what is NEWLY learned or corrected this turn.",
            },
            "intent": {
                "type": "string",
                "enum": list(get_args(Intent)),
                "description": "The intent this turn earns (stage-safe).",
            },
            "adjustment": {
                "type": "string",
                "description": "What to change when intent is adjust; otherwise empty.",
            },
            "reaction": {
                "type": "string",
                "description": (
                    "Optional tapback on the user's message. Only the six "
                    "universal tapbacks are allowed: ❤️ 👍 👎 😂 ‼️ ❓ "
                    "(or empty for none)."
                ),
            },
        },
        ["reply", "intent"],
    )


def present_plan_tool() -> dict[str, Any]:
    """The one tool the plan-presenter may call to emit its spoken plan."""
    return _function_schema(
        TOOL_NAME_PRESENT,
        "Emit Miriam's spoken presentation of the deterministic plan.",
        {
            "reply": {
                "type": "string",
                "description": (
                    "Her presentation of the plan, ending with the one "
                    "consent question."
                ),
            },
            "reaction": {
                "type": "string",
                "description": (
                    "Optional tapback on the user's message. Only the six "
                    "universal tapbacks are allowed: ❤️ 👍 👎 😂 ‼️ ❓ "
                    "(or empty for none)."
                ),
            },
        },
        ["reply"],
    )

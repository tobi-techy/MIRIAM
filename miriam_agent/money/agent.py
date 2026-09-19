"""The LLM layer: it writes words about numbers it was handed.

Two entry points, and the split is the entire point:

    build_money_plan(...)   the pipeline. Pure, deterministic, no LLM.
    narrate(plan)           the LLM rewrites the diagnosis sentence and the
                            90-day actions, and nothing else.

The model is given the computed plan as JSON and asked for prose. It cannot
change a figure, because by the time it sees the plan every figure is already
decided -- and because of the clamp below, which is the part that actually
enforces it.

**The clamp.** After narration, every number in the new prose is checked against
the numbers the plan already contained. Introducing a figure the pipeline never
computed voids the narration and the deterministic text stands. A prompt
instruction is a request; this is a check. The onboarding flow uses the same
adversarial pattern for the same reason, and it is the reason a confidently
worded wrong number never reaches a user.

Failure is fail-open, like the rest of the repo: if the model is unavailable the
plan is returned exactly as computed, which is a perfectly good plan.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from miriam_agent.agents.llm import ChatMessage, LLMProvider, get_llm_provider
from miriam_agent.money.plan import build_money_plan
from miriam_agent.money.reference import CountryReference
from miriam_agent.money.schema import MoneyPlan

logger = logging.getLogger(__name__)

# Any number at or above this is a claim about money and must be traceable to the
# plan. Below it, numbers are structural ("3 months", "two debts") and blocking
# them would make the prose unusable for no safety gain.
_MATERIAL_NUMBER = Decimal("100")

_NUMBER_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)(\s*%)?")

_SYSTEM_PROMPT = """You are Miriam, a money operator writing to one person about \
one plan that has already been computed.

The plan below is final. Every figure in it was calculated by a deterministic \
engine, not by you. Your only job is to rewrite two things so a person can read \
them:

1. `diagnosis` — one or two sentences naming the real problem. Blunt and true.
   If the situation is bad, say it is bad. No encouragement, no "you got this",
   no hustle language, no exclamation marks.
2. `actions` — the 90-day plan, one string per action, each starting with its
   timing ("This week:", "This payday:", "This month:").

Hard rules:
- NEVER introduce a number that is not already in the plan. If the plan does not
  contain it, you do not know it. This is checked and your output will be thrown
  away if you break it.
- Do not invent yields, returns, APYs, or market expectations. None exist here.
- Do not soften a refusal. If the plan says investing is not available, say so
  plainly and say why.
- Specific amounts and specific timing, or say nothing.
- Do not add sections, greetings, or sign-offs.

Return your answer through the emit_money_plan_narrative tool.
"""


class MoneyNarrative(BaseModel):
    """The only things the model is allowed to produce."""

    model_config = ConfigDict(extra="forbid")

    diagnosis: str = Field(min_length=1)
    actions: list[str] = Field(default_factory=list)


class NarrationReport(BaseModel):
    """What happened, for tests and tracing."""

    model_config = ConfigDict(extra="forbid")

    status: str  # applied | rejected_invented_numbers | unavailable | invalid
    violations: list[str] = Field(default_factory=list)
    detail: str = ""


def _narrative_tool() -> dict[str, Any]:
    """The single structured-output tool, mirroring the onboarding conductor."""
    return {
        "type": "function",
        "function": {
            "name": "emit_money_plan_narrative",
            "description": (
                "Return the rewritten diagnosis and 90-day actions for a "
                "pre-computed money plan. Use only figures already present in the "
                "plan."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "diagnosis": {
                        "type": "string",
                        "description": "One or two blunt sentences naming the problem",
                    },
                    "actions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "90-day actions, each prefixed with its timing",
                    },
                },
                "required": ["diagnosis", "actions"],
            },
        },
    }


def _parse_arguments(raw: Any) -> dict[str, Any] | None:
    """Tool arguments arrive as a JSON string; parse defensively."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# The clamp
# ---------------------------------------------------------------------------


def _normalize_number(raw: str) -> str:
    text = raw.replace(",", "").strip()
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _numbers_in(text: str) -> set[str]:
    return {_normalize_number(m.group(1)) for m in _NUMBER_RE.finditer(text or "")}


def _material_numbers(text: str) -> set[str]:
    """Numbers in ``text`` that constitute a claim about money or a rate."""
    found: set[str] = set()
    for match in _NUMBER_RE.finditer(text or ""):
        raw = _normalize_number(match.group(1))
        is_pct = match.group(2) is not None
        if is_pct:
            found.add(raw)
            continue
        try:
            if Decimal(raw) >= _MATERIAL_NUMBER:
                found.add(raw)
        except InvalidOperation:
            continue
    return found


def _decimal_forms(value: Decimal) -> set[str]:
    """Every reasonable written form of a Decimal the plan holds."""
    forms = {_normalize_number(str(value))}
    for places in ("1", "0.1", "0.01"):
        try:
            forms.add(_normalize_number(str(value.quantize(Decimal(places)))))
        except (ArithmeticError, InvalidOperation):
            continue
    try:
        forms.add(_normalize_number(str(value.to_integral_value())))
    except (ArithmeticError, InvalidOperation):
        pass
    return forms


def plan_vocabulary(plan: MoneyPlan) -> set[str]:
    """Every number the plan already contains, from its figures *and* its prose.

    Using the plan's own text as well as its numbers means the rule is exactly
    "do not introduce a figure the plan did not already state", which holds
    whether the figure came from a computed field or from the deterministic
    wording.
    """
    allowed: set[str] = set()

    structured: list[Decimal] = [
        plan.monthly_take_home,
        plan.surplus_monthly,
        plan.cashflow.fixed,
        plan.cashflow.debt,
        plan.cashflow.savings,
        plan.cashflow.investments,
        plan.cashflow.guilt_free,
        plan.buffer.target_months,
        plan.buffer.target_amount,
        plan.buffer.current_amount,
        plan.buffer.gap,
        plan.book.growth_pct,
        plan.book.defensive_pct,
        *(Decimal(str(v)) for v in plan.cashflow.shares.values()),
        *(Decimal(str(v)) for v in plan.glider.drift.values()),
    ]
    if plan.buffer.months_to_fill is not None:
        structured.append(plan.buffer.months_to_fill)
    for debt in plan.debts:
        structured.extend(
            [debt.balance, debt.apr_pct, debt.minimum_monthly, debt.extra_monthly]
        )

    for value in structured:
        allowed |= _decimal_forms(value)

    text_fields: list[str] = [
        plan.diagnosis,
        plan.cashflow.note,
        plan.buffer.vehicle,
        plan.buffer.location,
        plan.book.reason,
        plan.glider.blocked_reason,
        *plan.book.overrides,
        *plan.book.growth_sleeve,
        *plan.book.defensive_sleeve,
        *[a.what for a in plan.actions_90d],
        *[a.how for a in plan.actions_90d],
        *plan.automation_rules,
        *plan.kill_switches,
        *plan.assumptions,
        *plan.what_would_change,
        *[d.label for d in plan.debts],
        *[d.reason for d in plan.debts],
        *plan.glider.risks,
    ]
    for text in text_fields:
        allowed |= _numbers_in(text)
    return allowed


def clamp_violations(plan: MoneyPlan, narrative: MoneyNarrative) -> list[str]:
    """Figures in the narration that the plan never contained."""
    allowed = plan_vocabulary(plan)
    violations: list[str] = []

    chunks = [narrative.diagnosis, *narrative.actions]
    for chunk in chunks:
        for number in sorted(_material_numbers(chunk)):
            if number not in allowed:
                violations.append(number)
    return sorted(set(violations))


# Language that asserts the user is investing. If the plan refused, none of these
# words may appear unless they are negated.
_INVESTING_LANGUAGE: tuple[str, ...] = (
    "invest",
    "portfolio",
    "allocation",
    "70/30",
    "glider",
    "onchain",
    "on-chain",
    "crypto",
    "stocks",
    "shares",
    "the market",
    "put it in",
    "put it all in",
)

# How much text before the cue to search for a negation. Wide enough for
# "you are not ready to invest" and "nothing goes onchain yet".
_NEGATION_WINDOW = 40

_NEGATIONS: tuple[str, ...] = (
    "not",
    "no ",
    "never",
    "nothing",
    "cannot",
    "can't",
    "cant",
    "won't",
    "wont",
    "isn't",
    "isnt",
    "don't",
    "dont",
    "hold off",
    "instead of",
    "rather than",
    "until",
)


def check_plan_contradiction(plan: MoneyPlan, text: str) -> list[str]:
    """Words that imply investing when the plan refused to invest.

    The schema already makes a wrong *plan* impossible. This catches the other
    failure: a plan that correctly refuses, and prose that talks as if it did
    not. Returns the offending phrases so the caller can regenerate from the
    plan object instead of shipping the contradiction.

    Negated uses pass, because "nothing goes onchain yet" is exactly what a
    refusal should say.
    """
    if plan.is_investing() or plan.surplus_monthly > 0:
        return []
    if plan.glider.kind != "none":
        return []

    lowered = (text or "").casefold()
    violations: list[str] = []
    for cue in _INVESTING_LANGUAGE:
        start = lowered.find(cue)
        while start != -1:
            before = lowered[max(0, start - _NEGATION_WINDOW) : start]
            if not any(negation in before for negation in _NEGATIONS):
                violations.append(cue)
                break
            start = lowered.find(cue, start + 1)
    return sorted(set(violations))


# ---------------------------------------------------------------------------
# Narration
# ---------------------------------------------------------------------------


async def narrate_with_report(
    plan: MoneyPlan, *, provider: LLMProvider | None = None
) -> tuple[MoneyPlan, NarrationReport]:
    """Rewrite the diagnosis and actions, and report what happened.

    Returns the plan unchanged whenever the narration is unavailable, invalid, or
    caught introducing an unsupported figure.
    """
    try:
        llm = provider or get_llm_provider()
    except Exception as e:  # noqa: BLE001
        logger.info("narration unavailable: %s", e)
        return plan, NarrationReport(status="unavailable", detail=str(e))

    messages = [
        ChatMessage(role="system", content=_SYSTEM_PROMPT),
        ChatMessage(role="user", content=_plan_brief(plan)),
    ]
    try:
        response = await llm.complete(
            messages,
            tools=[_narrative_tool()],
            temperature=0.4,
            max_tokens=1200,
        )
    except Exception as e:  # noqa: BLE001
        logger.info("narration call failed: %s", e)
        return plan, NarrationReport(status="unavailable", detail=str(e))

    payload = _tool_payload(response)
    if payload is None:
        return plan, NarrationReport(
            status="invalid", detail="no structured narrative returned"
        )
    try:
        narrative = MoneyNarrative.model_validate(payload)
    except Exception as e:  # noqa: BLE001
        return plan, NarrationReport(status="invalid", detail=str(e))

    violations = clamp_violations(plan, narrative)
    if violations:
        logger.warning("narration rejected, ungrounded figures: %s", violations)
        return plan, NarrationReport(
            status="rejected_invented_numbers",
            violations=violations,
            detail=(
                "the narration contained figures the plan did not compute, so the "
                "deterministic text stands"
            ),
        )

    # Second gate: the figures are grounded, but the prose may still describe
    # investing the plan refused. Rejected for the same reason as an invented
    # number -- the user would act on it.
    contradictions = check_plan_contradiction(
        plan, " ".join([narrative.diagnosis, *narrative.actions])
    )
    if contradictions:
        logger.warning("narration rejected, contradicts the plan: %s", contradictions)
        return plan, NarrationReport(
            status="rejected_contradicts_plan",
            violations=contradictions,
            detail=(
                "the narration described investing while the plan refused it, so "
                "the deterministic text stands"
            ),
        )

    updated = plan.model_copy(
        update={
            "diagnosis": narrative.diagnosis,
            "actions_90d": _apply_actions(plan, narrative.actions),
        }
    )
    return updated, NarrationReport(status="applied")


async def narrate(plan: MoneyPlan, *, provider: LLMProvider | None = None) -> MoneyPlan:
    """Rewrite the diagnosis and actions. Returns the plan unchanged on failure."""
    narrated, _report = await narrate_with_report(plan, provider=provider)
    return narrated


def _apply_actions(plan: MoneyPlan, actions: list[str]) -> list[Any]:
    """Map rewritten action strings back onto the structured actions.

    The model supplies prose; the amounts and currency stay whatever the pipeline
    computed. If it returns a different number of actions, the extra or missing
    ones keep their computed form -- a shorter list is a style choice, not a
    reason to lose an action.
    """
    if not actions:
        return plan.actions_90d
    out: list[Any] = []
    for index, action in enumerate(plan.actions_90d):
        if index < len(actions):
            out.append(action.model_copy(update={"what": actions[index]}))
        else:
            out.append(action)
    return out


def _tool_payload(response: Any) -> dict[str, Any] | None:
    for call in getattr(response, "tool_calls", None) or []:
        function = call.get("function") if isinstance(call, dict) else None
        if not function:
            continue
        if function.get("name") != "emit_money_plan_narrative":
            continue
        return _parse_arguments(function.get("arguments"))
    return None


def _plan_brief(plan: MoneyPlan) -> str:
    """The computed plan, as the only facts the model is allowed to use."""
    return (
        "The computed plan (the only source of figures you may use):\n\n"
        + json.dumps(plan.model_dump(mode="json"), indent=2, default=str)
        + "\n\nRewrite the diagnosis and the 90-day actions."
    )


async def build_and_narrate(
    user_profile: Any,
    accounts: Any = None,
    glider_state: Any = None,
    *,
    reference: CountryReference | None = None,
    provider: LLMProvider | None = None,
) -> MoneyPlan:
    """Compute the plan, then narrate it. The one call most callers want."""
    plan = build_money_plan(
        user_profile,
        accounts,
        glider_state,
        reference=reference,
    )
    return await narrate(plan, provider=provider)

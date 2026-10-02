"""Gate functions that own Miriam's control-flow decisions.

A gate is code. TypeSafe answers typed questions; the gate composes those
answers with if-statements over thresholds and returns a typed decision that
the request path branches on. This module never generates user-facing text
beyond a handful of fixed, policy-safe templates for the short-circuit paths.

Fail modes are per-gate:

* ingress -- deterministic local safety (obvious PII) always runs. If TypeSafe
  is unavailable, ordinary turns continue to the generator in a degraded state
  instead of taking the whole chat path down.
* tool    -- fail closed (mutating actions block, reads still run).
* egress  -- fail open (never block the reply path; the generator prompt is
  the residual safety net).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from typesafe_sdk import NoulAnswer

from miriam_agent.judgment.answers import choice_margin
from miriam_agent.judgment.client import enabled
from miriam_agent.judgment.egress_claims import (
    build_claim_catalog,
    unsupported_claims,
)
from miriam_agent.judgment.policy import POLICY
from miriam_agent.judgment.questions import EGRESS, INGRESS, TOOL
from miriam_agent.judgment.schemas import (
    EgressJudgment,
    IngressJudgment,
    JudgmentState,
    ToolJudgment,
)
from miriam_agent.judgment.service import JudgmentUnavailableError, evaluate
from miriam_agent.judgment.state import (  # noqa: F401
    build_ingress_state,
    build_state,
    detect_pii,
)

logger = logging.getLogger(__name__)


class Branch(StrEnum):
    REFUSE = "refuse"
    CLARIFY = "clarify"
    ESCALATE = "escalate"
    PLANNER = "planner"
    GENERATOR = "generator"


class ToolBranch(StrEnum):
    ALLOW = "allow"
    REJECT = "reject"
    CONFIRM = "confirm"
    BLOCK = "block"


class EgressBranch(StrEnum):
    SEND = "send"
    DISCARD = "discard"
    REGENERATE = "regenerate"


@dataclass
class RoutingDecision:
    """What the request path should do next (ingress).

    ``reply`` is set only for branches that short-circuit the generator
    (refuse / clarify). Other branches pass through to the generator and exist
    to be logged and, later, wired to a planner or a human queue.
    """

    branch: Branch
    reply: str | None = None
    refusal_reason: str | None = None
    judgment: IngressJudgment | None = None
    degraded: bool = False

    @property
    def short_circuits(self) -> bool:
        return self.reply is not None


@dataclass
class ToolDecision:
    """What to do with a proposed tool call (tool gate)."""

    branch: ToolBranch
    reason: str | None = None
    judgment: ToolJudgment | None = None
    degraded: bool = False


@dataclass
class EgressDecision:
    """What to do with a draft reply (egress gate)."""

    branch: EgressBranch
    reply: str | None = None
    judgment: EgressJudgment | None = None
    degraded: bool = False
    # Soft signal from the tone question: never flips a send, but the caller
    # appends it to the regenerate instruction so a retry fixes tone as well.
    tone_note: str | None = None


# Fixed templates. Deliberately short and policy-safe: the refuse/clarify ones
# run before the generator and must never echo a jailbreak attempt or a pasted
# secret. The egress fallback replaces a discarded draft.
_REFUSE_JAILBREAK = (
    "I can't help with that. If there's something about your money you want to "
    "sort out, tell me plainly and I'll do my best."
)
_REFUSE_PII = (
    "Please don't paste anything sensitive here. I've set that aside. Tell me "
    "what you're trying to do and I'll help without needing that detail."
)
_CLARIFY = "Tell me a bit more about what you're after and I'll sort it out."
EGRESS_SAFE_FALLBACK = (
    "I can't send that as-is. Let me take another look and get back to you."
)
EGRESS_DONT_KNOW = (
    "I don't have that reliably yet. Let me check what I actually know before "
    "I answer."
)
EGRESS_REGENERATE_INSTRUCTION = (
    "Revise your last reply. State only facts you can point to in the "
    "conversation or the tool results, answer the user's question directly, and "
    "keep the tone warm and appropriate. If you cannot, say you don't know."
)


def local_safety_decision(message: str | None) -> RoutingDecision | None:
    """The deterministic part of ingress, independent of TypeSafe availability.

    This is deliberately small: it refuses a secret the local scanner can see,
    before any model or provider can receive it. It is not a replacement for
    the TypeSafe judgment layer; it is the residual guard for the disabled and
    unavailable paths.
    """
    if detect_pii(message):
        return RoutingDecision(
            branch=Branch.REFUSE, reply=_REFUSE_PII, refusal_reason="pii"
        )
    return None


# ---------------------------------------------------------------------------
# Ingress
# ---------------------------------------------------------------------------


async def ingress_gate(
    state: JudgmentState,
    *,
    client=None,
) -> RoutingDecision:
    """Run the ingress gate for one user turn, before the generator."""
    # Local safety is unconditional. A missing TypeSafe key or flag must not
    # turn the deterministic secret check off with it. Check the current turn
    # here as well as trusting ``pii_detected``, which also covers history and
    # callers that did not build the state through ``build_ingress_state``.
    local = local_safety_decision(state.turn.user_text)
    if state.pii_detected or local is not None:
        logger.info("ingress local PII refuse")
        return local or RoutingDecision(
            branch=Branch.REFUSE, reply=_REFUSE_PII, refusal_reason="pii"
        )

    if not enabled():
        logger.info("typesafe disabled; skipping model ingress gate")
        return RoutingDecision(branch=Branch.GENERATOR, degraded=True)

    try:
        judgment = cast(IngressJudgment, await evaluate(state, INGRESS, client=client))
    except JudgmentUnavailableError as exc:
        logger.error(
            "typesafe ingress unavailable; continuing degraded",
            extra={"error": str(exc)},
        )
        return RoutingDecision(branch=Branch.GENERATOR, degraded=True)

    if _needs_second_opinion(judgment):
        try:
            second = cast(
                IngressJudgment, await evaluate(state, INGRESS, client=client)
            )
            judgment = _merge_ingress_second_opinion(judgment, second)
        except JudgmentUnavailableError as exc:
            # The first answer is still usable; the repeat is an extra guard,
            # not a second point of failure.
            logger.warning("typesafe ingress repeat unavailable: %s", exc)

    decision = decide_ingress(judgment)
    _log_ingress_decision(judgment, decision)
    return decision


async def safe_ingress_gate(
    *,
    user_id: str,
    message: str,
    registry=None,
    history: list[dict] | None = None,
    user_context: dict | None = None,
    memory_facts: list[dict] | None = None,
    financial_plan: dict | None = None,
    **_: object,
) -> RoutingDecision | None:
    """Build and run ingress without letting an unexpected bug 500 chat.

    Ordinary TypeSafe outages continue degraded inside ``ingress_gate``. This
    wrapper covers unexpected state-builder or gate failures and still runs the
    local secret check before returning ``None`` to the caller.
    """
    try:
        return await ingress_gate(
            build_ingress_state(
                user_id=user_id,
                message=message,
                history=history,
                user_context=user_context,
                registry=registry,
                memory_facts=memory_facts,
                financial_plan=financial_plan,
            )
        )
    except Exception:
        logger.exception("ingress gate failed; checking local safety before fallback")
        return local_safety_decision(message)


_REPEAT_NOUL_FIELDS = (
    ("jailbreak", "jailbreak_review", "jailbreak_block"),
    ("requests_disallowed", "jailbreak_review", "requests_disallowed_block"),
    ("exposes_pii", "pii_review_reply", "pii_block_reply"),
    ("wants_human", "escalation_urgency_min", "wants_human_escalate"),
    ("is_urgent", "escalation_urgency_min", "urgency_escalate"),
    ("needs_tools", "needs_tools_review", "needs_tools_planner"),
)


def _needs_second_opinion(judgment: IngressJudgment) -> bool:
    """Whether a hazard answer sits in the review band between thresholds."""
    for field, low_name, high_name in _REPEAT_NOUL_FIELDS:
        answer = getattr(judgment, field, None)
        if answer is None:
            continue
        low = getattr(POLICY, low_name)
        high = getattr(POLICY, high_name)
        if low <= answer.noul < high:
            return True
    return False


def _merge_ingress_second_opinion(
    first: IngressJudgment, second: IngressJudgment
) -> IngressJudgment:
    """Take the more cautious Noul when two samples disagree.

    More mass means the hazard is more likely. This is the safety direction for
    every field in the repeat battery; it cannot turn a high first answer into
    a green light.
    """
    updates = {}
    for field, _, _ in _REPEAT_NOUL_FIELDS:
        left = getattr(first, field, None)
        right = getattr(second, field, None)
        if left is None or right is None:
            continue
        updates[field] = NoulAnswer.model_construct(noul=max(left.noul, right.noul))
    return first.model_copy(update=updates)


def decide_ingress(judgment: IngressJudgment) -> RoutingDecision:
    """Compose the ingress answers into a routing branch (pure, testable).

    Order matters and is deliberate: hard safety, then escalation, then
    ambiguity, then planning. A clarify never gets to mask a jailbreak, a
    pasted secret, or an urgent cry for help.
    """
    # (a) Hard safety. A jailbreak, a disallowed ask, or a pasted secret is
    # refused outright and is never masked by a softer branch below.
    if (
        judgment.jailbreak.noul >= POLICY.jailbreak_block
        or judgment.requests_disallowed.noul >= POLICY.requests_disallowed_block
        or (
            judgment.intent.choice == "jailbreak_or_probe"
            and judgment.intent.confidence >= POLICY.intent_min_confidence
        )
    ):
        return RoutingDecision(
            branch=Branch.REFUSE,
            reply=_REFUSE_JAILBREAK,
            refusal_reason="jailbreak_or_disallowed",
            judgment=judgment,
        )
    if (
        judgment.jailbreak.noul >= POLICY.jailbreak_review
        or judgment.requests_disallowed.noul >= POLICY.jailbreak_review
    ):
        return RoutingDecision(
            branch=Branch.CLARIFY,
            reply=_CLARIFY,
            refusal_reason="safety_review",
            judgment=judgment,
        )
    if judgment.exposes_pii.noul >= POLICY.pii_review_reply:
        return RoutingDecision(
            branch=Branch.REFUSE,
            reply=_REFUSE_PII,
            refusal_reason="pii",
            judgment=judgment,
        )
    # (b) An explicit request for a human always escalates.
    if judgment.wants_human.noul >= POLICY.wants_human_escalate:
        return RoutingDecision(branch=Branch.ESCALATE, judgment=judgment)
    # (c) Urgency on its own escalates when the turn is a distress report, not
    # a concrete action: a time-sensitive complaint or cry for help is a
    # human's job even if the user sounds calm (anger is NOT required). A task
    # the user wants done ("pay my rent by Friday") is planned rather than
    # escalated, so actionable intents are excluded here and reach (f).
    if (
        judgment.is_urgent.noul >= POLICY.urgency_escalate
        and judgment.intent.choice
        not in ("perform_task", "lookup_data", "change_account")
    ):
        return RoutingDecision(branch=Branch.ESCALATE, judgment=judgment)
    # (d) Anger plus urgency (urgency in the 0.7-0.85 band) still escalates.
    if (
        judgment.frustration.score >= POLICY.frustration_escalate
        and judgment.is_urgent.noul >= POLICY.escalation_urgency_min
    ):
        return RoutingDecision(branch=Branch.ESCALATE, judgment=judgment)
    # (e) Genuine ambiguity asks one question instead of guessing.
    if judgment.intent.choice == "other" or (
        POLICY.low_confidence_clarify
        and (
            judgment.intent.confidence < POLICY.intent_min_confidence
            or choice_margin(judgment.intent) < POLICY.intent_min_margin
        )
    ):
        return RoutingDecision(branch=Branch.CLARIFY, reply=_CLARIFY, judgment=judgment)
    # (f) A concrete task that needs tools goes to the planner.
    if (
        judgment.needs_tools.noul >= POLICY.needs_tools_planner
        and judgment.intent.choice in ("perform_task", "lookup_data")
    ):
        return RoutingDecision(branch=Branch.PLANNER, judgment=judgment)
    # (g) Everything else is answerable from context.
    return RoutingDecision(branch=Branch.GENERATOR, judgment=judgment)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


async def tool_gate(
    state: JudgmentState,
    *,
    client=None,
) -> ToolDecision:
    """Run the tool gate for one proposed tool call, before it executes."""
    if not enabled():
        return ToolDecision(branch=ToolBranch.ALLOW, degraded=True)

    # A malformed/empty proposal is never a green light. There is nothing to
    # judge, so fail closed with an explicit reason rather than evaluating a
    # nameless tool.
    if state.proposed_tool is None or not state.proposed_tool.name:
        logger.warning("typesafe tool gate: no proposed tool to judge")
        return ToolDecision(branch=ToolBranch.BLOCK, reason="no_proposed_tool")

    try:
        judgment = cast(ToolJudgment, await evaluate(state, TOOL, client=client))
    except JudgmentUnavailableError as exc:
        # Fail closed on the side-effect axis: reads still run, anything that
        # mutates is blocked until judgment is available.
        logger.error("typesafe tool fail-closed", extra={"error": str(exc)})
        tool_name = state.proposed_tool.name if state.proposed_tool else ""
        if _side_effects(state, tool_name) == "read":
            return ToolDecision(branch=ToolBranch.ALLOW, degraded=True)
        return ToolDecision(
            branch=ToolBranch.BLOCK, reason="judgment_unavailable", degraded=True
        )

    decision = decide_tool(judgment)
    tool_name = state.proposed_tool.name if state.proposed_tool else ""
    _log_tool_decision(judgment, decision, tool_name)
    return decision


def decide_tool(judgment: ToolJudgment) -> ToolDecision:
    """Compose the tool answers into an execute/confirm/block/reject branch.

    An ordinary payment the user asked for is **costly but expected**: it lands
    in the confirm band, not a block. Blocking is reserved for a user who is
    not entitled to the action at all, and for a genuinely destructive action
    that always needs a human. Evidence is evaluated in this order:

    1. relevant + args match + required args complete? no -> reject
    2. outside the user's entitlement? -> block
    3. destructive (irreversible beyond the block bar)? -> block
    4. costly/irreversible in the confirm band? -> execute if the user already
       confirmed this exact action, else confirm
    5. otherwise -> execute
    """
    irrelevant = (
        judgment.tool_is_relevant.noul < POLICY.tool_relevance_reject_below
        or judgment.args_match_request.noul < POLICY.args_match_reject_below
    )
    incomplete = judgment.args_look_complete.noul < POLICY.args_complete_reject_below
    unauthorized = (
        judgment.exceeds_user_authority.noul >= POLICY.exceeds_authority_block
    )
    destructive = judgment.irreversible.noul >= POLICY.tool_risk_block
    in_confirm_band = (
        judgment.costly.noul >= POLICY.tool_costly_confirm
        or judgment.irreversible.noul >= POLICY.tool_risk_confirm
    )
    confirmed = judgment.user_confirmed_this_action.noul >= POLICY.user_confirmed_allow

    # Relevance first. A tool the user never asked for is *rejected*, not
    # blocked: the model raises `exceeds_user_authority` for plainly
    # mismatched tools (buy_asset on "what's my balance?"), and calling that a
    # block would mislabel a routing miss as an entitlement problem. The
    # reason string still records the authority/destructiveness signals, so a
    # reject never hides an authority block.
    if irrelevant or incomplete:
        reasons = ["irrelevant_or_mismatched"] if irrelevant else []
        if incomplete:
            reasons.append("args_incomplete")
        if unauthorized:
            reasons.append("authority_also_exceeded")
        if destructive:
            reasons.append("irreversible_also_high")
        return ToolDecision(
            branch=ToolBranch.REJECT, reason="+".join(reasons), judgment=judgment
        )

    # Authorization and hard-destructive actions block regardless of
    # confirmation: a user cannot consent away an entitlement rule, and a
    # delete/destroy never rides the confirm band.
    if unauthorized:
        return ToolDecision(
            branch=ToolBranch.BLOCK, reason="exceeds_user_authority", judgment=judgment
        )
    if destructive:
        return ToolDecision(
            branch=ToolBranch.BLOCK, reason="irreversible", judgment=judgment
        )

    if in_confirm_band:
        if confirmed:
            return ToolDecision(
                branch=ToolBranch.ALLOW,
                reason="user_confirmed_this_action",
                judgment=judgment,
            )
        return ToolDecision(
            branch=ToolBranch.CONFIRM, reason="needs_confirmation", judgment=judgment
        )
    return ToolDecision(branch=ToolBranch.ALLOW, judgment=judgment)


def _side_effects(state: JudgmentState, tool_name: str) -> str:
    """The declared side-effects of a tool, or a conservative default."""
    for tool in state.tools:
        if tool.name == tool_name:
            return tool.side_effects
    return "write"


# ---------------------------------------------------------------------------
# Egress
# ---------------------------------------------------------------------------


async def egress_gate(
    state: JudgmentState,
    *,
    client=None,
) -> EgressDecision:
    """Run the egress gate on a draft reply, before it is sent."""
    if not (state.draft_reply or "").strip():
        return EgressDecision(branch=EgressBranch.REGENERATE)
    if not enabled():
        return EgressDecision(branch=EgressBranch.SEND, degraded=True)

    try:
        judgment = cast(EgressJudgment, await evaluate(state, EGRESS, client=client))
    except JudgmentUnavailableError as exc:
        # Fail open: the generator's own prompt is the residual safety net and
        # blocking the reply path on egress is explicitly discouraged.
        logger.error("typesafe egress fail-open", extra={"error": str(exc)})
        return EgressDecision(branch=EgressBranch.SEND, degraded=True)

    decision = decide_egress(judgment)
    if decision.branch is EgressBranch.SEND:
        claim_decision = await _claim_check_decision(
            state=state,
            client=client,
            holistic_judgment=judgment,
        )
        if claim_decision is not None:
            _log_egress_decision(judgment, claim_decision)
            return claim_decision
    _log_egress_decision(judgment, decision)
    return decision


async def _claim_check_decision(
    *,
    state: JudgmentState,
    client,
    holistic_judgment: EgressJudgment,
) -> EgressDecision | None:
    """Run per-claim verification only after the holistic gate says send.

    Claim verification is a quality amplifier, not a fail-closed control. If the
    dynamic catalog cannot run, the holistic verdict stands and the decision is
    marked degraded so the failure is visible.
    """
    catalog, checks = build_claim_catalog(state.draft_reply or "")
    if not checks:
        return None
    try:
        claim_judgment = await evaluate(state, catalog, client=client)
    except Exception as exc:  # noqa: BLE001 - egress quality must fail open
        logger.warning("egress claim check unavailable: %s", exc)
        return EgressDecision(
            branch=EgressBranch.SEND,
            judgment=holistic_judgment,
            degraded=True,
        )

    unsupported = unsupported_claims(
        claim_judgment,
        checks,
        threshold=POLICY.claim_unsupported_regenerate,
    )
    if unsupported:
        logger.info(
            "egress claim regeneration",
            extra={"unsupported_claims": unsupported},
        )
        return EgressDecision(
            branch=EgressBranch.REGENERATE, judgment=holistic_judgment
        )
    return None


def _tone_note(score: float) -> str | None:
    """Map the 0..2 tone score to a soft hint. None means tone is fine."""
    if score <= 0.6:
        return "cold"
    if score >= 1.6:
        return "sloppy"
    return None


def decide_egress(judgment: EgressJudgment) -> EgressDecision:
    """Compose the egress answers into a send/discard/regenerate branch.

    discard owns hard policy/secret failures (including a literal echo of a
    user secret -- the model already had the secret, so a retry risks repeating
    it). regenerate owns fixable quality misses: invented facts, or a reply
    that does not answer, retried once by the caller and then fallen back.
    """
    tone = _tone_note(judgment.tone_fit.score)
    if (
        judgment.policy_violation.noul >= POLICY.policy_violation_discard
        or judgment.leaks_system.noul >= POLICY.leaks_system_discard
        or judgment.repeats_pii.noul >= POLICY.repeats_pii_discard
        or judgment.echoes_user_secret.noul >= POLICY.echoes_user_secret_discard
    ):
        return EgressDecision(
            branch=EgressBranch.DISCARD,
            reply=EGRESS_SAFE_FALLBACK,
            judgment=judgment,
            tone_note=tone,
        )
    if (
        judgment.invents_facts.noul >= POLICY.invents_facts_regenerate
        or judgment.answers_the_ask.noul < POLICY.egress_grounded_min
    ):
        return EgressDecision(
            branch=EgressBranch.REGENERATE,
            judgment=judgment,
            tone_note=tone,
        )
    return EgressDecision(
        branch=EgressBranch.SEND,
        judgment=judgment,
        tone_note=tone,
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log_ingress_decision(judgment: IngressJudgment, decision: RoutingDecision) -> None:
    logger.info(
        "typesafe ingress decision",
        extra={
            "branch": decision.branch.value,
            "refusal_reason": decision.refusal_reason,
            "degraded": decision.degraded,
            "intent": judgment.intent.choice,
            "intent_confidence": round(judgment.intent.confidence, 3),
            "intent_margin": round(choice_margin(judgment.intent), 3),
            "jailbreak": round(judgment.jailbreak.noul, 3),
            "requests_disallowed": round(judgment.requests_disallowed.noul, 3),
            "exposes_pii": round(judgment.exposes_pii.noul, 3),
            "needs_tools": round(judgment.needs_tools.noul, 3),
            "is_urgent": round(judgment.is_urgent.noul, 3),
            "frustration": round(judgment.frustration.score, 3),
            "wants_human": round(judgment.wants_human.noul, 3),
        },
    )


def _log_tool_decision(
    judgment: ToolJudgment, decision: ToolDecision, tool_name: str
) -> None:
    logger.info(
        "typesafe tool decision",
        extra={
            "branch": decision.branch.value,
            "reason": decision.reason,
            "degraded": decision.degraded,
            "tool": tool_name,
            "tool_is_relevant": round(judgment.tool_is_relevant.noul, 3),
            "args_match_request": round(judgment.args_match_request.noul, 3),
            "args_look_complete": round(judgment.args_look_complete.noul, 3),
            "costly": round(judgment.costly.noul, 3),
            "irreversible": round(judgment.irreversible.noul, 3),
            "exceeds_user_authority": round(judgment.exceeds_user_authority.noul, 3),
            "user_confirmed_this_action": round(
                judgment.user_confirmed_this_action.noul, 3
            ),
        },
    )


def _log_egress_decision(judgment: EgressJudgment, decision: EgressDecision) -> None:
    logger.info(
        "typesafe egress decision",
        extra={
            "branch": decision.branch.value,
            "degraded": decision.degraded,
            "tone_note": decision.tone_note,
            "answers_the_ask": round(judgment.answers_the_ask.noul, 3),
            "invents_facts": round(judgment.invents_facts.noul, 3),
            "leaks_system": round(judgment.leaks_system.noul, 3),
            "repeats_pii": round(judgment.repeats_pii.noul, 3),
            "echoes_user_secret": round(judgment.echoes_user_secret.noul, 3),
            "tone_fit": round(judgment.tone_fit.score, 3),
            "policy_violation": round(judgment.policy_violation.noul, 3),
        },
    )

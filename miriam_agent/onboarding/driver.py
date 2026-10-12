"""The LLM-led conductor that carries Miriam's onboarding conversation.

Miriam (an LLM) leads the whole flow: the interview, the optional statement
request, and the plan consent conversation. There is no script and no fixed
question bank -- she decides what to ask, in her own words, one question at a
time, and every conversation plays differently because she follows the human.
She records what she learns as short free-form ``facts`` she extracts each turn.

But she is never allowed to invent money rules -- a deterministic plan builder
(``plan.py``) turns the facts she learned into the diagnosis, steps and standing
rules, and she presents that plan in her own words.

Each turn she returns a small structured object, emitted through the
``emit_conductor_outcome`` tool (with a free-text-JSON fallback for providers
that can't call tools):

    {"reply": "...", "suggested_replies": ["..."], "facts": {key: value},
     "intent": "interview", "adjustment": ""}

The service owns every state transition; this module only talks to the model
and validates the output through a typed pydantic contract, so a misbehaving
model can never corrupt state or bypass a consent decision.

The prompt embodies the personality spec: she checks the stated problem before
fixing it, watches for money scripts internally (never naming them), and her
directness level is chosen deterministically and handed to her in the context.

Fail-open: any model error resolves to ``None`` and the service falls back to a
short, human fallback line so the signup never stalls.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from miriam_agent.agents.llm import ChatMessage, LLMProvider, LLMResponse
from miriam_agent.config.settings import get_settings
from miriam_agent.financial.hypotheses import (
    leak_probe_open,
    leaked_text,
    probe_categories_open,
)
from miriam_agent.onboarding.contracts import (
    TOOL_NAME_CONDUCTOR,
    TOOL_NAME_PRESENT,
    ConductorOutcome,
    PresentPlanOutcome,
    conductor_tool,
    present_plan_tool,
)
from miriam_agent.onboarding.prompts import (
    CHARS_PER_ATTRIBUTE,
    CONDUCTOR_SYSTEM_PROMPT,
    GROUNDING_CORRECTION,
    MAX_FACT_KEY_LENGTH,
    MAX_FACT_VALUE_LENGTH,
    MAX_FACTS_PER_TURN,
    MAX_HISTORY_LINES,
    MAX_REPLY_WITH_TAPS,
    MAX_SUGGESTED_REPLIES,
    MAX_TAP_LENGTH,
    PRESENT_PLAN_SYSTEM_PROMPT,
    prompt_version,
)
from miriam_agent.onboarding.state import (
    STAGE_AWAITING_ADJUSTMENT,
    STAGE_AWAITING_STATEMENT,
    STAGE_INTERVIEW,
    STAGE_PLAN_CONSENT,
    OnboardingState,
)
from miriam_agent.utils.text import valid_reaction

logger = logging.getLogger(__name__)

# Re-exported so existing importers keep working: the prompt text lives in
# ``prompts.py`` now, and ``driver`` is the stable front door.
__all__ = [
    "CHARS_PER_ATTRIBUTE",
    "CONDUCTOR_SYSTEM_PROMPT",
    "GROUNDING_CORRECTION",
    "MAX_FACTS_PER_TURN",
    "MAX_FACT_KEY_LENGTH",
    "MAX_FACT_VALUE_LENGTH",
    "MAX_HISTORY_LINES",
    "MAX_REPLY_WITH_TAPS",
    "MAX_SUGGESTED_REPLIES",
    "MAX_TAP_LENGTH",
    "PRESENT_PLAN_SYSTEM_PROMPT",
    "prompt_version",
]

# Which intents are meaningful in which stage. A model-intent outside this
# whitelist is dropped and replaced by the stage's safe default.
STAGE_INTENTS: dict[str, set[str]] = {
    STAGE_INTERVIEW: {"interview", "request_statement", "present_plan", "abandon"},
    STAGE_AWAITING_STATEMENT: {"request_statement", "present_plan", "abandon"},
    STAGE_PLAN_CONSENT: {"consent_yes", "consent_no", "adjust", "abandon", "interview"},
    STAGE_AWAITING_ADJUSTMENT: {"adjust", "done_adjusting", "abandon", "interview"},
}

_SAFE_DEFAULT_INTENT: dict[str, str] = {
    STAGE_INTERVIEW: "interview",
    STAGE_AWAITING_STATEMENT: "request_statement",
    STAGE_PLAN_CONSENT: "interview",
    STAGE_AWAITING_ADJUSTMENT: "interview",
}


@dataclass
class DriverOutcome:
    """A validated reply from the onboarding conductor."""

    reply: str
    suggested: list[str] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)
    intent: str = "interview"
    adjustment: str = ""
    reaction: str = ""


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort extraction of a JSON object from an LLM response."""
    if not text:
        return None
    unwrapped = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE
    )
    candidates = [unwrapped, text.strip()]
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    for candidate in candidates:
        start = candidate.find("{")
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(candidate)):
            if candidate[i] == "{":
                depth += 1
            elif candidate[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(candidate[start : i + 1])
                        if isinstance(data, dict):
                            return data
                    except json.JSONDecodeError:
                        pass
                    break
    return None


def _clean_suggested(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > MAX_TAP_LENGTH:
            text = text[: MAX_TAP_LENGTH - 1] + "\u2026"
        if text not in out:
            out.append(text)
        if len(out) >= MAX_SUGGESTED_REPLIES:
            break
    return out


def _clean_facts(raw: Any) -> dict[str, str]:
    """Sanitize the model's free-form facts.

    No whitelist: the keys are the agent's own labels. We only bound size and
    drop blanks, so a badly-behaved model can never bloat state.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        label = str(key or "").strip()
        if not label or len(label) > MAX_FACT_KEY_LENGTH:
            continue
        text = str(value or "").strip()
        if not text:
            continue
        if len(text) > MAX_FACT_VALUE_LENGTH:
            text = text[: MAX_FACT_VALUE_LENGTH - 1] + "\u2026"
        if label in out:
            continue
        out[label] = text
        if len(out) >= MAX_FACTS_PER_TURN:
            break
    return out


def _default_intent(stage: str) -> str:
    return _SAFE_DEFAULT_INTENT.get(stage, "interview")


def _fit_taps(reply: str, suggested: list[str]) -> list[str]:
    """Drop taps when the reply cannot be a poll title.

    iMessage shows the reply as the poll question. Cutting it at 60 characters
    produced the half-sentences ("...spare this…"). A complete message with no
    poll is the readable fallback.
    """
    if not suggested:
        return []
    if len(reply) > MAX_REPLY_WITH_TAPS:
        return []
    return suggested


def _parse_driver_data(data: dict[str, Any], stage: str) -> DriverOutcome | None:
    """Validate a normalized outcome dict against the typed contract, then
    apply the deterministic bounds and the stage-intent whitelist."""
    try:
        model = ConductorOutcome.model_validate(data)
    except ValidationError as exc:
        logger.warning("onboarding outcome failed contract validation: %s", exc)
        return None
    reply = model.reply.strip()
    if not reply:
        return None
    suggested = _fit_taps(reply, _clean_suggested(list(model.suggested_replies)))
    intent_raw = model.intent.casefold().strip()
    intent = (
        intent_raw
        if intent_raw in STAGE_INTENTS.get(stage, set())
        else _default_intent(stage)
    )
    reaction = model.reaction.strip()
    if not valid_reaction(reaction):
        reaction = ""
    return DriverOutcome(
        reply=reply,
        suggested=suggested,
        facts=_clean_facts(dict(model.facts)),
        intent=intent,
        adjustment=model.adjustment.strip(),
        reaction=reaction,
    )


def _parse_driver_output(text: str, stage: str) -> DriverOutcome | None:
    data = _extract_json(text)
    if data is None:
        return None
    return _parse_driver_data(data, stage)


def _outcome_from_tool_call(
    tool_calls: list[dict[str, Any]], stage: str
) -> DriverOutcome | None:
    """Preferred parse: a structured ``emit_conductor_outcome`` tool call. The
    tool arg surface is validated through the same typed contract as the text
    fallback, so the model cannot smuggle extra fields in either way."""
    for call in tool_calls or []:
        fn = call.get("function") or {}
        if (fn.get("name") or "") != TOOL_NAME_CONDUCTOR:
            continue
        data = _extract_json((fn.get("arguments") or "").strip())
        if data is None:
            continue
        outcome = _parse_driver_data(data, stage)
        if outcome is not None:
            return outcome
    return None


def _outcome_from_response(response: LLMResponse, stage: str) -> DriverOutcome | None:
    """Tool calls first (structured outputs), free-text JSON as fallback so any
    provider (or a canned test provider) that only returns text still works."""
    if response.tool_calls:
        outcome = _outcome_from_tool_call(response.tool_calls, stage)
        if outcome is not None:
            return outcome
    return _parse_driver_output(response.content or "", stage)


def _history_lines(history: list[dict[str, Any]]) -> list[str]:
    rows = []
    for item in reversed(history[-MAX_HISTORY_LINES:]):  # most recent first
        role = str(item.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        rows.append(f"{role}: {content[:CHARS_PER_ATTRIBUTE]}")
    return rows


def _knows_block(state: OnboardingState) -> str:
    """Everything the agent has learned so far, in one flat block."""
    lines = []
    if state.money_moment:
        lines.append(f'- money_moment = "{state.money_moment}"')
    if state.goal:
        lines.append(f'- goal = "{state.goal}"')
    for key, value in state.learned.items():
        lines.append(f'- {key} = "{value}"')
    if not lines:
        return (
            f"nothing yet -- the user is {state.name or 'the user'} and this is "
            "the start of the interview."
        )
    return "\n".join(lines)


def _hypotheses_block(state: OnboardingState, user_text: str) -> str:
    """spec v1.1 §12: the ranked read Miriam steers against, built
    deterministically from everything we know (money moment, goal, learned
    facts, the current message). Empty when there is nothing to work with.
    Internal only -- she is told never to read it back."""
    parts: list[str] = [state.money_moment, state.goal]
    parts.extend(state.learned.values())
    if user_text:
        parts.append(user_text)
    text = " ".join(p for p in parts if p)
    if not text.strip():
        return ""
    return leaked_text(text)


def _conversation_state_block(state: OnboardingState) -> str:
    """spec §29: the backend's living read of the conversation, handed to the
    model. The money script appears only for steering -- the rules forbid ever
    naming it back to the user."""
    cs = state.conversation_state or {}
    lines = [
        f"CURRENT TOPIC: {cs.get('current_topic') or 'not identified yet'}",
        f"WORKING TOWARD: {cs.get('user_goal') or 'not named yet'}",
        f"CURRENT PROBLEM: {cs.get('current_problem') or 'not identified yet'}",
        f"CONFIDENCE IN THIS READ: {cs.get('confidence') or 'low'}",
        f"LAST INSIGHT: {cs.get('last_insight') or 'none yet'}",
        f"PENDING ACTION: {cs.get('pending_action') or 'none'}",
        f"SENTIMENT: {cs.get('user_sentiment') or 'neutral'}",
        f"RELATIONSHIP: {cs.get('relationship_stage') or 'new'}",
        f"DIRECTNESS LEVEL: {cs.get('directness_level') or 1}",
    ]
    script = (cs.get("money_script") or "").strip()
    if script:
        lines.append(
            f"LIKELY MONEY SCRIPT (use it to steer; never name it to them): "
            f"{script}"
        )
    return "\n".join(lines)


def _context_block(
    *,
    state: OnboardingState,
    user_text: str,
    is_poll_vote: bool,
    event: str,
    moving_on_hint: str,
    history: list[dict[str, Any]],
    poll_title: str = "",
) -> str:
    parts = [
        "USER CONTEXT",
        f"CURRENT STAGE: {state.stage}",
    ]
    if event:
        parts.append(f"EVENT: {event}")
    parts.append(f'INBOUND MESSAGE: "{user_text}"')
    parts.append(f"IS A POLL TAP: {'yes' if is_poll_vote else 'no'}")
    if poll_title:
        parts.append(f'POLL BEING ANSWERED: "{poll_title}"')
    if moving_on_hint:
        parts.append(f"MOVING ON: {moving_on_hint}")
    state_block = _conversation_state_block(state)
    if state_block:
        parts.append(
            "CONVERSATION STATE (your read of this user, internal):\n" + state_block
        )
    parts.append("WHAT YOU KNOW SO FAR (your source of truth):\n" + _knows_block(state))
    hypothesis_block = _hypotheses_block(state, user_text)
    if hypothesis_block:
        parts.append(
            "WORKING HYPOTHESES (ranked, internal steering only -- never read "
            "these back to the user):\n" + hypothesis_block
        )
    joined = " ".join(
        [state.money_moment, state.goal, *state.learned.values(), user_text]
    )
    if user_text and leak_probe_open(joined):
        open_categories = probe_categories_open(joined)
        if open_categories:
            parts.append(
                "OPEN LEAK CHANNELS (internal only, do not quiz these before "
                "income, pay rhythm, and fixed costs are recorded: "
                + ", ".join(open_categories)
                + ")"
            )
    try:
        from miriam_agent.onboarding.money_bridge import gap_question, money_readiness

        ready, missing = money_readiness(state)
    except Exception:
        ready, missing = False, []
    if ready:
        parts.append(
            "NEXT FACT ONLY: salary and cashflow are in. intent present_plan. "
            "Do not ask another interview question."
        )
    elif missing:
        parts.append(
            "NEXT FACT ONLY: "
            + missing[0]
            + ". Copy this sentence exactly, with their name in front if you have it. "
            + "Do not rephrase it: "
            + gap_question(missing)
            + " Do not ask about subscriptions, food, leaks, or feelings yet."
        )

    lines = _history_lines(history)
    if lines:
        parts.append("CONVERSATION SO FAR (most recent first):\n" + "\n".join(lines))
    return "\n".join(parts)


async def conductor_turn(
    *,
    provider: LLMProvider,
    state: OnboardingState,
    history: list[dict[str, Any]],
    user_text: str,
    is_poll_vote: bool = False,
    event: str = "",
    moving_on_hint: str = "",
    poll_title: str = "",
    correction: str = "",
) -> DriverOutcome | None:
    """One LLM-led conversation turn in whatever stage the interview is in."""
    settings = get_settings()
    user_block = _context_block(
        state=state,
        user_text=user_text,
        is_poll_vote=is_poll_vote,
        event=event,
        moving_on_hint=moving_on_hint,
        history=history,
        poll_title=poll_title,
    )
    # ``correction`` rides inside the system prompt rather than as an extra
    # user turn: the retry instruction is ours, never something the user is
    # made to have said.
    system_content = CONDUCTOR_SYSTEM_PROMPT
    if correction:
        system_content = f"{CONDUCTOR_SYSTEM_PROMPT}\n\n{correction}"
    messages = [
        ChatMessage(role="system", content=system_content),
        ChatMessage(role="user", content=user_block),
    ]
    response = await provider.complete(
        messages=messages,
        tools=[conductor_tool()],
        temperature=settings.ONBOARDING_TEMPERATURE,
        max_tokens=settings.ONBOARDING_MAX_TOKENS,
    )
    return _outcome_from_response(response, state.stage)


async def present_plan_turn(
    *,
    provider: LLMProvider,
    state: OnboardingState,
    history: list[dict[str, Any]],
    plan: dict[str, Any],
    adjustments: list[str],
    event: str = "",
) -> DriverOutcome | None:
    """Have Miriam present the deterministic plan in her own voice (text shown
    in full; no taps -- the consent decision follows on the next turn)."""
    settings = get_settings()
    context = _context_block(
        state=state,
        user_text="",
        is_poll_vote=False,
        event=event,
        moving_on_hint="",
        history=history,
        poll_title="",
    )
    adjustment_note = ""
    if adjustments:
        adjustment_note = "\nRECENT ADJUSTMENTS THE USER ASKED FOR:\n- " + "\n- ".join(
            adjustments
        )
    plan_block = json.dumps(plan, indent=2, ensure_ascii=True)
    messages = [
        ChatMessage(role="system", content=PRESENT_PLAN_SYSTEM_PROMPT),
        ChatMessage(
            role="user", content=f"{context}\n{adjustment_note}\n\n{plan_block}"
        ),
    ]
    response = await provider.complete(
        messages=messages,
        tools=[present_plan_tool()],
        temperature=settings.ONBOARDING_TEMPERATURE,
        max_tokens=settings.ONBOARDING_MAX_TOKENS,
    )
    return _present_from_response(response)


def _present_from_response(response: LLMResponse) -> DriverOutcome | None:
    """Tool calls first (structured outputs), free-text JSON as fallback."""
    for call in response.tool_calls or []:
        fn = call.get("function") or {}
        if (fn.get("name") or "") != TOOL_NAME_PRESENT:
            continue
        data = _extract_json((fn.get("arguments") or "").strip())
        if data is None:
            continue
        try:
            model = PresentPlanOutcome.model_validate(data)
        except ValidationError:
            continue
        reply = model.reply.strip()
        if reply:
            reaction = model.reaction.strip()
            if not valid_reaction(reaction):
                reaction = ""
            return DriverOutcome(reply=reply, intent="present_plan", reaction=reaction)
    return _parse_present_text(response.content or "")


def _parse_present_text(text: str) -> DriverOutcome | None:
    data = _extract_json(text)
    if data is None:
        return None
    reply = str(data.get("reply") or "").strip()
    if not reply:
        return None
    reaction = str(data.get("reaction") or "").strip()
    if not valid_reaction(reaction):
        reaction = ""
    return DriverOutcome(reply=reply, intent="present_plan", reaction=reaction)

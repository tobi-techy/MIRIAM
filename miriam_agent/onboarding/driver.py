"""The LLM-led conductor that carries Miriam's onboarding conversation.

Miriam (an LLM) drives the whole flow: the interview, the optional statement
request, and the plan consent conversation. But she is never allowed to invent
money rules -- a deterministic plan builder (``plan.py``) turns the dimensions
she extracts into the diagnosis, steps and standing rules, and she presents
that plan in her own words.

Each turn she returns a small structured JSON object:

    {"reply": "...", "suggested_replies": ["..."], "answers": {dim: value},
     "intent": "interview", "adjustment": ""}

The service owns every state transition; this module only talks to the model,
classifies the user's words into the canonical dimension vocabulary, and
validates the output so a misbehaving model can never corrupt state or bypass
a consent decision.

Fail-open: any model error resolves to ``None`` and the service falls back to
the deterministic interview (``questions.next_question``) so signup never
stalls.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from miriam_agent.agents.llm import ChatMessage, LLMProvider
from miriam_agent.config.settings import get_settings
from miriam_agent.onboarding import questions as q
from miriam_agent.onboarding.state import (
    STAGE_AWAITING_ADJUSTMENT,
    STAGE_AWAITING_STATEMENT,
    STAGE_INTERVIEW,
    STAGE_PLAN_CONSENT,
    OnboardingState,
)

logger = logging.getLogger(__name__)

MAX_SUGGESTED_REPLIES = 4
MAX_REPLY_WITH_TAPS = 90
MAX_TAP_LENGTH = 56
CHARS_PER_ATTRIBUTE = 400
MAX_HISTORY_LINES = 8

_CORE_MEANINGS: dict[str, str] = {
    "money_feelings": "how the user feels about money right now",
    "income_predictability": "how steady their income is",
    "shortage_cause": "the usual reason money runs low",
    "liquidity_runway": "how long they could keep going with no income",
    "dependents": "who leans on their income",
    "goal_direction": "what they want their money to do for them",
    "involvement": "how hands-on they want Miriam to be",
}

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


CONDUCTOR_SYSTEM_PROMPT = (
    """You are Miriam, a warm, sharp financial companion walking a new user through \
a first-time money conversation over iMessage. You are NOT a survey, a form, or a \
customer-service bot. This is a real conversation: you answer what they actually \
wrote, in the order they wrote it, and questions unfold like a friend asking, never \
a checklist.

YOUR JOB
Get the seven CORE dimensions below (plus an occasional follow-up when the user's \
answer gives real reason) so a real financial plan can be built later. You choose \
the wording and the order -- never read options aloud like a menu; turn each \
dimension into a natural question that reflects their situation back so they feel \
heard. Ask ONE question at a time. Keep every reply to 1-3 short sentences. No \
jargon, no bullet lists, no lectures, no "Great question!", no em dash.

TAXONOMY -- dimension id, what it means, and the canonical options you must \
classify the user's words into:
__TAXONOMY__

CLASSIFICATION
- When the user answers with their own words, classify what they meant into the \
closest canonical option for that dimension and put it under "answers" \
(e.g. user: "Honestly it's all a mess" -> "money_feelings": "Honestly, a mess").
- You may return MULTIPLE answers in one turn if a message covers several \
dimensions. Only return dimensions newly learned or refined THIS turn -- never \
re-emit the whole history.
- "goal_direction" may stay free text ("buy a house") when their goal is \
specific; specific goals matter more than the canned list.

MOVING THE CONVERSATION
- Prefer dimensions nobody has answered yet. Reach for follow-ups only when the \
answer gives reason (see their "when" hints); never interrogate.
- Off-topic messages (a question, a story, a joke): reply warmly, then guide \
back when it fits. Small talk is fine; forcing is not.
- You NEVER build or invent the financial plan yourself. A deterministic engine \
turns these dimensions into the diagnosis, steps and standing rules later. \
"present_plan" simply means the interview is done and you are ready for the \
engine plan to be shown.
- When the user clearly wants out ("stop", "skip", "not now", "never mind"), \
return intent "abandon".

INTENTS (choose exactly one per turn, respecting the CURRENT STAGE you are given)
- interview: keep talking / ask the next question.
- request_statement: the interview has enough -- ask them to send a recent bank \
statement (a PDF is best); they may skip. Use when real numbers would make the \
plan stronger.
- present_plan: the interview is done; time to show the plan. Only once the seven \
CORE dimensions are covered (or the engine says READY FOR PLAN).
- consent_yes / consent_no: the user just decided to set the plan up as standing \
rules, or decided not to. Only in the consent stage.
- adjust: the user wants to change the drafted plan. Put what they want changed in \
"adjustment". If their request is vague, ask one clarifying question with intent \
"adjust" and no "adjustment" text.
- done_adjusting: they are done tweaking; re-present the reworked plan.
- abandon: they want out.

SUGGESTED REPLIES (OPTIONAL TAPS)
- Up to 4 short tap options that answer your question, e.g. ["Calm", "Stressful", \
"A mess"]. Only when a quick tap genuinely helps.
- In iMessage the taps render a poll whose label IS your main "reply", so when you \
include suggested_replies keep "reply" to one short question (under 60 chars) and \
each tap under 28 chars. When a full message matters more (tasking steps, \
explanation), leave taps out.
- In the consent stage, taps like ["Yes, set it up", "Let's adjust it", "Not now"] \
help them decide fast.

VOICE
Plain, warm, zero finance jargon, human. A friend who happens to be brilliant with \
money, not a bank.

RESPOND WITH ONLY A JSON OBJECT:
{"reply": "...", "suggested_replies": ["..."], "answers": {"dim": "..."}, \
"intent": "interview", "adjustment": ""}
"""
)


PRESENT_PLAN_SYSTEM_PROMPT = (
    """You are Miriam, a warm, plain-spoken friend with a real plan in hand. A \
deterministic engine just built a financial plan for the user. Present it in YOUR \
voice so they understand why each move matters -- then ask whether to set it up.

SPEAK AS YOURSELF. Rules:
- Lead with one honest line about their picture (the "diagnostic_state"), then the \
2-4 moves that matter most, each in a line of plain English. If they have \
additions or adjustments, acknowledge the newest ones warmly.
- Only use what appears in the plan. Never invent numbers, steps, or rules.
- No jargon, no bullet list longer than 4 items, no em dash, no lecture. Under \
~200 words for iMessage.
- End with exactly ONE question: shall I set this up so it runs quietly in the \
background for you?

PLAN:
{plan}

RESPOND WITH ONLY A JSON OBJECT:
{"reply": "..."}
"""
)


@dataclass
class DriverOutcome:
    """A validated reply from the onboarding conductor."""

    reply: str
    suggested: list[str] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)
    intent: str = "interview"
    adjustment: str = ""


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


def _clean_answers(raw: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    known = set(q.all_question_ids())
    for key, value in raw.items():
        dim = str(key).strip()
        if dim not in known:
            continue
        val = str(value or "").strip()
        if not val:
            continue
        out[dim] = q.canonicalize(dim, val)
    return out


def _default_intent(stage: str) -> str:
    return _SAFE_DEFAULT_INTENT.get(stage, "interview")


def _clamp_reply(reply: str, has_taps: bool) -> str:
    if has_taps and len(reply) > MAX_REPLY_WITH_TAPS:
        return reply[: MAX_REPLY_WITH_TAPS - 1] + "\u2026"
    return reply


def _parse_driver_output(text: str, stage: str) -> DriverOutcome | None:
    data = _extract_json(text)
    if data is None:
        return None
    reply = str(data.get("reply") or "").strip()
    if not reply:
        return None
    suggested = _clean_suggested(data.get("suggested_replies"))
    intent_raw = str(data.get("intent") or "").strip().casefold()
    intent = (
        intent_raw
        if intent_raw in STAGE_INTENTS.get(stage, set())
        else _default_intent(stage)
    )
    return DriverOutcome(
        reply=_clamp_reply(reply, bool(suggested)),
        suggested=suggested,
        answers=_clean_answers(data.get("answers")),
        intent=intent,
        adjustment=str(data.get("adjustment") or "").strip(),
    )


def _taxonomy() -> dict[str, Any]:
    cores = [
        {
            "id": c.id,
            "dimension": c.dimension,
            "meaning": _CORE_MEANINGS.get(c.id, c.dimension),
            "options": list(c.options),
        }
        for c in q.CORE
    ]
    probes = [
        {
            "id": p.id,
            "dimension": p.dimension,
            "when": q.PROBE_HINTS.get(p.id, ""),
            "options": list(p.options),
        }
        for p in q.FOLLOWUPS
    ]
    return {"core": cores, "follow_ups": probes}


def _facts_block(state: OnboardingState) -> str:
    if not state.answers:
        return "none yet"
    lines = []
    for dim, value in state.answers.items():
        question = q.CORE_BY_ID.get(dim) or q.FOLLOWUP_BY_ID.get(dim)
        meaning = _CORE_MEANINGS.get(dim, question.dimension if question else dim)
        lines.append(f'- {dim} ({meaning}) = "{value}"')
    return "\n".join(lines)


def _pending_dimensions(state: OnboardingState) -> list[str]:
    pending = [c.id for c in q.CORE if c.id not in state.answers]
    # Follow-ups are only ever optional; never list them as required.
    return pending


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


def _user_block(
    *,
    state: OnboardingState,
    user_text: str,
    is_poll_vote: bool,
    event: str,
    ready_for_plan: bool,
    history: list[dict[str, Any]],
) -> str:
    parts = [
        "USER CONTEXT",
        f"CURRENT STAGE: {state.stage}",
    ]
    if event:
        parts.append(f"EVENT: {event}")
    parts.append(f'INBOUND MESSAGE: "{user_text}"')
    parts.append(f"IS A POLL TAP: {'yes' if is_poll_vote else 'no'}")
    parts.append(f"READY FOR PLAN: {'yes' if ready_for_plan else 'no'}")

    facts = _facts_block(state)
    parts.append("EXTRACTED FACTS SO FAR (your source of truth):\n" + facts)

    pending = _pending_dimensions(state)
    if pending:
        parts.append("DIMENSIONS STILL TO COVER: " + ", ".join(pending))
    else:
        parts.append(
            "DIMENSIONS STILL TO COVER: none -- all seven CORE dimensions are "
            "covered, stop interviewing and move on."
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
    ready_for_plan: bool = False,
) -> DriverOutcome | None:
    """One LLM-led conversation turn in whatever stage the interview is in."""
    settings = get_settings()
    taxonomy = json.dumps(_taxonomy())

    user_block = _user_block(
        state=state,
        user_text=user_text,
        is_poll_vote=is_poll_vote,
        event=event,
        ready_for_plan=ready_for_plan,
        history=history,
    )
    messages = [
        ChatMessage(
            role="system",
            content=CONDUCTOR_SYSTEM_PROMPT.replace("__TAXONOMY__", taxonomy),
        ),
        ChatMessage(role="user", content=user_block),
    ]
    response = await provider.complete(
        messages=messages,
        tools=None,
        temperature=settings.ONBOARDING_TEMPERATURE,
        max_tokens=settings.ONBOARDING_MAX_TOKENS,
    )
    return _parse_driver_output(response.content or "", state.stage)


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
    context = _user_block(
        state=state,
        user_text="",
        is_poll_vote=False,
        event=event,
        ready_for_plan=False,
        history=history,
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
        tools=None,
        temperature=settings.ONBOARDING_TEMPERATURE,
        max_tokens=settings.ONBOARDING_MAX_TOKENS,
    )
    return _parse_present_text(response.content or "")


def _parse_present_text(text: str) -> DriverOutcome | None:
    data = _extract_json(text)
    if data is None:
        return None
    reply = str(data.get("reply") or "").strip()
    if not reply:
        return None
    return DriverOutcome(reply=reply, intent="present_plan")

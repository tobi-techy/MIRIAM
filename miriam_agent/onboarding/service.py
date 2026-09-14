"""The onboarding turn driver: one function per inbound message.

Miriam leads a real conversation, not a form:

    first message        -> Miriam (the LLM) greets, learns their name, and
                            opens with the Money Moment question
    interview turns      -> Miriam talks with them, one question at a time,
                            recording short free-form "facts"; there is no
                            question bank and no fixed vocabulary
    interview done       -> Miriam asks for a bank statement (optional), then
                            the deterministic plan builder runs on the facts
    plan present         -> Miriam presents the engine's plan in her voice
    consent              -> deterministic consent mapping (yes -> standing
                            rules, no -> draft, adjust -> rework)
    complete             -> persist plan + standing rules as memory facts

The LLM decides WHAT to say and what she learns; every state transition and
every money rule is deterministic. Polls stay as optional tap suggestions (the
LLM attaches ``suggested_replies``), and free text is always accepted. A guard
rail caps the interview so it can never drag. If the LLM is unreachable or
returns garbage, the flow degrades to short, warm fallback lines that keep the
conversation moving instead of stalling it.

The shape of the state also carries the personality spec's structured layers:
a money-moment read and a goal read (emotion/problem, target/cost/priority),
mandatory money-script awareness (internal only), a deterministic
conversation_state (spec §29: topic, standing problem, pending action,
relationship stage, directness level) that escalates directness with
relationship depth + severity (spec §12), and an ``aha_generated`` metric when
the plan (with its financial_insight, spec §21) first lands (spec §27).

All state transitions live in one explicit machine (``_TRANSITIONS``): the
model only reports an intent, and the table -- never the LLM -- decides where
that moves the conversation (spec §30). The plan, the meta reads and the
conversation state are typed pydantic contracts validated at every write
boundary.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from miriam_agent.agents.llm import LLMProvider, get_llm_provider
from miriam_agent.config.settings import get_settings
from miriam_agent.onboarding import driver
from miriam_agent.onboarding import plan as plan_builder
from miriam_agent.onboarding.contracts import (
    ConversationState,
    GoalMeta,
    MoneyMomentMeta,
    Plan,
)
from miriam_agent.onboarding.quality import EvalMeta, evaluate_reply
from miriam_agent.onboarding.state import (
    STAGE_AWAITING_ADJUSTMENT,
    STAGE_AWAITING_STATEMENT,
    STAGE_COMPLETE,
    STAGE_GREETING,
    STAGE_INTERVIEW,
    STAGE_PLAN_CONSENT,
    OnboardingState,
    get_onboarding_state_store,
)
from miriam_agent.onboarding.trace import (
    TraceRecord,
    get_onboarding_trace_store,
)

logger = logging.getLogger(__name__)

# Clear money-action intents that bypass the interview so a first message like
# "send 5k to Tola" or "what's my balance" is honored immediately instead.
_ACTION_INTENT = re.compile(
    r"\b(send|transfer|pay|withdraw|invest|buy|sell|swap|exchange|balance)\b",
    re.IGNORECASE,
)

# Mid-flow money detection.  The first-message gate (_ACTION_INTENT) is
# deliberately broad -- there is no flow to protect.  Later, the interview,
# statement and consent answers must never be hijacked, so a request only
# counts as a money move when it is a concrete, now-directed action: an
# execution verb followed (in the near window) by an amount or a concrete
# money beneficiary (a person, a bill, an account, an instrument) -- or a
# balance read.  "send it" (a statement answer) and "pay attention to X"
# never trip it.
_MONEY_EXECUTE_VERB = re.compile(
    r"\b(send|transfer|pay|withdraw|invest|buy|sell|swap|exchange|deposit|"
    r"put|move)\b",
    re.IGNORECASE,
)
_MONEY_BENEFICIARY = re.compile(
    r"\b(mom|mum|mother|dad|father|parent|family|brother|sister|friend|"
    r"him|her|them|landlord|shop|car|house|rent|bill|bills|account|card|"
    r"bank|debt|btc|bitcoin|eth|ethereum|usdc|sol|etf|stock|stocks|"
    r"fund|funds|tuition|school)\b",
    re.IGNORECASE,
)
# "pay off / pay down X" is a debt-reduction goal, not a do-it-now transfer.
_MONEY_DEBT_REFRAME = re.compile(r"\bpay\s+(off|down)\b", re.IGNORECASE)
_MONEY_BALANCE_READ = re.compile(r"\bbalance\b", re.IGNORECASE)
# Habitual/scheduled/future phrasing ("I pay the rent every month", "I'll buy
# etf tomorrow") is the user describing their life, not instructing a transfer
# right now -- so a beneficiary match in that framing never hijacks the chat.
_HABIT_OR_FUTURE = re.compile(
    r"\b(every\s+\w+|each\s+\w+|monthly|weekly|biweekly|yearly|always|usually|"
    r"normally|sometimes|tomorrow|next\s+\w+|on\s+the\s+(first|1st|"
    r"[0-9]+(?:st|nd|rd|th)))\b",
    re.IGNORECASE,
)


def _wants_money_action(text: str) -> bool:
    """True when the user is asking to move money *now* (or read a balance) --
    the one thing the onboarding flow must never swallow, so it hands off to
    the general agent with its own safety net instead of folding the request
    into the plan chat.

    Polar without being reckless: a bare "pay" or "invest" (a goal or a
    thanks) is not a move; "send 500 to mom" and "what's my balance" are.
    Amounts asked for live ("send 500") always count; a beneficiary match only
    counts when the request is not habitual/scheduled/future phrasing.
    """
    t = _lower(text)
    if _MONEY_DEBT_REFRAME.search(t):
        return False
    if _MONEY_BALANCE_READ.search(t):
        return True
    m = _MONEY_EXECUTE_VERB.search(t)
    if not m:
        return False
    tail = t[m.end() : m.end() + 24]
    if any(ch.isdigit() for ch in tail):
        return True
    if _HABIT_OR_FUTURE.search(tail) or _HABIT_OR_FUTURE.search(t):
        return False
    return _MONEY_BENEFICIARY.search(tail) is not None


# Deterministic fallback vocabulary when the LLM is unavailable.
_ABANDON = {
    "skip",
    "skip it",
    "stop",
    "stop this",
    "never mind",
    "forget it",
    "not now",
    "later",
    "quit",
    "i'm done",
    "im done",
}
_YES_PHRASES = {
    "yes",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "go",
    "go for it",
    "set it up",
    "do it",
    "agree",
    "looks good",
    "sounds good",
    "autopilot",
}
_NO_PHRASES = {"no", "nope", "nah", "not now", "later", "skip it", "no thanks"}
_ADJUST_HINTS = ("adjust", "change", "tweak", "edit", "rework", "instead")

# Phrases that restart an already-finished/abandoned interview. Deliberately
# narrow: post-completion casual chit-chat ("let's go", "try again") must never
# wipe a finished interview, so only unambiguous redo/revision phrasings count.
_REENTER = re.compile(
    r"\b(redo|restart|start\s+over|start\s+again|from\s+scratch|"
    r"revisit\s+(my\s+)?plan|rework\s+(my\s+)?plan|change\s+(my\s+)?plan)\b",
    re.IGNORECASE,
)

# Explicit name introductions are safe to honor mid-interview ("call me Tobi");
# a bare "Tobi" is already a legitimate answer, so we never guess names from
# free text without one of these markers.
_VOLUNTEERED_NAME = re.compile(
    r"(?:\bmy name\s+(?:is|'s)\b|\bcall me\b|\bpeople call me\b|\bi am\b|\bi'm\b)",
    re.IGNORECASE,
)

# Free-form fact keys the agent reserves for the two first-class fields. When a
# fact carries one of these, the service lifts it onto the state's dedicated
# field so the plan and the prompt always see the money moment and the goal.
_MONEY_MOMENT_KEYS = frozenset(
    {"money_moment", "money_moment_description", "the_money_moment", "whats_bothering"}
)
_GOAL_KEYS = frozenset(
    {
        "goal",
        "concrete_goal",
        "the_goal",
        "rich_life",
        "desired_life",
        "money_goal",
        "goal_detail",
    }
)

# spec §6/§7: reserved keys that carry the structured reads of the money moment
# and the goal. Each is lifted off the free-form facts onto the structured
# fields (emotion/suspected problem/confidence, target date/cost/priority).
_MONEY_MOMENT_META_KEYS = {
    "money_moment_emotion": "emotion",
    "money_moment_suspected_problem": "suspected_problem",
    "money_moment_confidence": "confidence",
}
_GOAL_META_KEYS = {
    "goal_target_date": "target_date",
    "goal_estimated_cost": "estimated_cost",
    "goal_priority": "priority",
    "goal_funding_status": "funding_status",
}

# spec §22/§29: quiet conversation-state signals the agent may report. They are
# lifted into conversation_state (the money script is remembered, never shown).
_SENTIMENT_KEYS = frozenset({"sentiment", "user_sentiment"})
_MONEY_SCRIPT_KEYS = frozenset({"money_script", "script"})

# Structured values are short labels, never long prose.
_META_VALUE_MAX = 64

_REWORK_ASK = "Sure. What should we change? Tell me in a sentence and I'll rework it."
_TELL_ME_ASK = "Tell me what to change in a sentence and I'll rework it."


@dataclass(frozen=True)
class _Transition:
    """One legal (stage, intent) edge in the onboarding state machine."""

    to: str
    action: str


# Explicit state machine (spec §30): the model only reports an intent; this
# table -- never the LLM -- decides where that moves the conversation. Each
# intent above is one edge from the stage it is legal in.
_ACT_STAY = "stay"
_ACT_PRESENT_PLAN = "present_plan"
_ACT_REQUEST_STATEMENT = "request_statement"
_ACT_COMPLETE_AUTOMATED = "complete_automated"
_ACT_COMPLETE_DRAFT = "complete_draft"
_ACT_ABANDON = "abandon"
_ACT_ADJUST = "adjust"

_TRANSITIONS: dict[tuple[str, str], _Transition] = {
    (STAGE_INTERVIEW, "interview"): _Transition(STAGE_INTERVIEW, _ACT_STAY),
    (STAGE_INTERVIEW, "request_statement"): _Transition(
        STAGE_AWAITING_STATEMENT, _ACT_REQUEST_STATEMENT
    ),
    (STAGE_INTERVIEW, "present_plan"): _Transition(
        STAGE_PLAN_CONSENT, _ACT_PRESENT_PLAN
    ),
    (STAGE_INTERVIEW, "abandon"): _Transition(STAGE_COMPLETE, _ACT_ABANDON),
    (STAGE_AWAITING_STATEMENT, "request_statement"): _Transition(
        STAGE_AWAITING_STATEMENT, _ACT_REQUEST_STATEMENT
    ),
    (STAGE_AWAITING_STATEMENT, "present_plan"): _Transition(
        STAGE_PLAN_CONSENT, _ACT_PRESENT_PLAN
    ),
    (STAGE_AWAITING_STATEMENT, "abandon"): _Transition(STAGE_COMPLETE, _ACT_ABANDON),
    (STAGE_PLAN_CONSENT, "consent_yes"): _Transition(
        STAGE_COMPLETE, _ACT_COMPLETE_AUTOMATED
    ),
    (STAGE_PLAN_CONSENT, "consent_no"): _Transition(
        STAGE_COMPLETE, _ACT_COMPLETE_DRAFT
    ),
    (STAGE_PLAN_CONSENT, "adjust"): _Transition(STAGE_AWAITING_ADJUSTMENT, _ACT_ADJUST),
    (STAGE_PLAN_CONSENT, "abandon"): _Transition(STAGE_COMPLETE, _ACT_ABANDON),
    (STAGE_PLAN_CONSENT, "interview"): _Transition(STAGE_PLAN_CONSENT, _ACT_STAY),
    (STAGE_AWAITING_ADJUSTMENT, "adjust"): _Transition(
        STAGE_AWAITING_ADJUSTMENT, _ACT_ADJUST
    ),
    (STAGE_AWAITING_ADJUSTMENT, "done_adjusting"): _Transition(
        STAGE_PLAN_CONSENT, _ACT_PRESENT_PLAN
    ),
    (STAGE_AWAITING_ADJUSTMENT, "abandon"): _Transition(STAGE_COMPLETE, _ACT_ABANDON),
    (STAGE_AWAITING_ADJUSTMENT, "interview"): _Transition(
        STAGE_AWAITING_ADJUSTMENT, _ACT_STAY
    ),
}

# Any (stage, intent) the table does not enumerate: stay in place and reply.
_DEFAULT_TRANSITION = _Transition("", _ACT_STAY)


@dataclass
class PollSpec:
    title: str
    options: list[str]


STATEMENT_POLL = PollSpec(
    title="Send me a bank statement for a real deep dive?",
    options=["Yes, send it now", "Skip for now"],
)
CONSENT_POLL = PollSpec(
    title="Set this up as standing rules so you don't have to think about it?",
    options=["Yes, set it up", "Let's adjust it first", "Not now"],
)


@dataclass
class OnboardingTurn:
    """What the handler should do with this turn.

    ``took_over`` True means the onboarding flow handled it: return
    ``response`` (persist it as the assistant message) and, when ``poll`` is
    set, surface the poll. False means Miriam's normal agent should run.
    """

    took_over: bool = False
    response: str = ""
    poll: dict[str, Any] | None = None
    conversation_id: str = field(default="")
    stage: str = ""
    completed: bool = False
    automated: bool = False
    name: str = ""

    def to_payload(self, conversation_id: str) -> dict[str, Any]:
        return {
            "response": self.response,
            "conversation_id": self.conversation_id or conversation_id,
            "requires_confirmation": False,
            "cards": [],
            "poll": self.poll,
            "onboarding": {
                "stage": self.stage,
                "completed": self.completed,
                "automated": self.automated,
            },
            "name": self.name or "",
        }


def _match_option(text: str, options: list[str] | tuple[str, ...]) -> str | None:
    """Match user text (a poll vote or typed answer) to an option."""
    c = text.strip().casefold()
    if not c:
        return None
    for o in options:
        if c == o.casefold():
            return o
    if len(c) >= 3:
        for o in options:
            oc = o.casefold()
            if c in oc:
                return o
    return None


def _lower(text: str) -> str:
    return text.strip().casefold()


class OnboardingService:
    """Advance the onboarding conversation for one user, one message at a time."""

    def __init__(
        self,
        memory_store: Any,
        state_store: Any | None = None,
        provider: LLMProvider | None = None,
        trace_store: Any | None = None,
    ) -> None:
        self._memory = memory_store
        self._state_store = state_store or get_onboarding_state_store()
        self._trace = trace_store or get_onboarding_trace_store()
        self._settings = get_settings()
        self._provider = provider

    # -- public -------------------------------------------------------------

    async def handle_turn(
        self,
        user: Any,
        *,
        message: str,
        is_poll_vote: bool = False,
        poll_title: str = "",
        document: dict[str, Any] | None = None,
    ) -> OnboardingTurn:
        del poll_title  # taps arrive as plain text; the LLM reads the option
        if not self._settings.ONBOARDING_ENABLED:
            return OnboardingTurn(conversation_id=message or "")
        text = (message or "").strip()
        conversation_id = f"onboarding:{user.id}"
        doc_summary = self._doc_summary(document)

        state = await self._state_store.get_state(user.id)
        if state is not None and state.complete:
            # An explicit redo/restart intent re-opens the interview from scratch
            # (completion is not a locked door; giving up never is either).
            if not is_poll_vote and not doc_summary and _REENTER.search(text):
                state = OnboardingState()
                state.stage = STAGE_INTERVIEW  # skip the greeting on a redo
                await self._state_store.clear(user.id)
                self._emit(user.id, "restarted")
                return await self._conductor_turn(user.id, state, conversation_id, text)
            return OnboardingTurn(conversation_id=conversation_id)

        if doc_summary:
            # A fresh bank statement jump-starts (or resumes) the real-numbers
            # plan. The very first message being a statement doesn't skip the
            # interview: save the scan, and Miriam acknowledges it on the way in.
            if state is None:
                state = OnboardingState()
                state.stage = STAGE_INTERVIEW  # a statement is an answer, not a hello
                state.document_summary = doc_summary
                await self._state_store.save_state(user.id, state)
                self._emit(user.id, "statement_provided")
                return await self._conductor_turn(
                    user.id,
                    state,
                    conversation_id,
                    text,
                    event=f"the user just shared a bank statement: {doc_summary}",
                )
            if state.stage in (
                STAGE_INTERVIEW,
                STAGE_AWAITING_STATEMENT,
                STAGE_AWAITING_ADJUSTMENT,
            ):
                state.document_summary = doc_summary
                await self._remember(
                    user.id,
                    "statement",
                    "liquidity",
                    "shared a bank statement",
                    is_a_vote=True,
                )
                self._emit(user.id, "statement_provided")
                return await self._present_plan(user.id, state, conversation_id)
            if state.stage == STAGE_PLAN_CONSENT:
                state.document_summary = doc_summary
                state.plan = self._validated_plan(
                    plan_builder.build_plan(
                        state.learned,
                        doc_summary,
                        goal=state.goal,
                        money_moment=state.money_moment,
                        adjustments=state.adjustments,
                    )
                )
                await self._state_store.save_state(user.id, state)
                return self._turn(
                    "Thanks - I've updated the plan with your real numbers. "
                    "Where would you like to take it?",
                    stage=state.stage,
                )

        if state is None:
            # First message. Honored action intents skip the interview.
            if not is_poll_vote and _ACTION_INTENT.search(text):
                return OnboardingTurn(conversation_id=conversation_id)
            state = OnboardingState()
            state.stage = STAGE_GREETING
            await self._state_store.save_state(user.id, state)
            self._emit(user.id, "greeting")
            return self._greeting_turn()

        # Greeting exchange first: learn their name before any question. More
        # person than form, same as the guest brain's opening arc. "Skip" (or
        # an action intent) politely skips the name so it never blocks.
        if state.stage == STAGE_GREETING:
            if not is_poll_vote and (
                _ACTION_INTENT.search(text) or self._SKIP_GREETING.search(text)
            ):
                state.stage = STAGE_INTERVIEW
                await self._state_store.save_state(user.id, state)
                self._emit(user.id, "interview_started")
                return await self._conductor_turn(
                    user.id,
                    state,
                    conversation_id,
                    text,
                    event="the user skipped giving a name; start the interview "
                    "without one",
                )
            return await self._name_turn(user.id, state, conversation_id, text)

        # Never let the interview drag: the agent carries it, but the cap closes
        # it deterministically.
        if (
            state.stage == STAGE_INTERVIEW
            and state.interview_turns >= self._settings.ONBOARDING_MAX_QUESTIONS
        ):
            self._emit(user.id, "interview_finished")
            return await self._present_plan(user.id, state, conversation_id)

        # A money move (or balance read) is never absorbed into the plan chat,
        # whatever state the interview is in: hand it to the agent un-taken-over
        # so its own safety net decides. Poll votes stay in the flow -- a tap on
        # "Yes, send it now" is a statement answer, not a transfer.
        if not is_poll_vote and _wants_money_action(text):
            return OnboardingTurn(conversation_id=conversation_id)

        if not text:
            return OnboardingTurn(conversation_id=conversation_id)

        return await self._conductor_turn(
            user.id, state, conversation_id, text, is_poll_vote=is_poll_vote
        )

    # -- LLM-led turns ------------------------------------------------------

    def _llm(self) -> LLMProvider:
        if self._provider is not None:
            return self._provider
        return get_llm_provider()

    async def _history(self, conversation_id: str) -> list[dict[str, Any]]:
        try:
            rows: list[dict[str, Any]] = await self._memory.get_conversation_history(
                conversation_id
            )
            return rows
        except Exception:
            logger.warning("onboarding history read failed (continuing blind)")
            return []

    def _moving_on_hint(self, state: OnboardingState) -> str:
        """Gently nudge the agent to hand off once the interview has run a few
        turns; the hard cap still closes it no matter what."""
        cap = self._settings.ONBOARDING_MAX_QUESTIONS
        if state.stage != STAGE_INTERVIEW or state.interview_turns < max(cap - 2, 0):
            return ""
        return (
            f"You have had {state.interview_turns} turns now. If you have enough "
            'to build a real plan, move on: intent "present_plan" (or '
            '"request_statement" for their real numbers) instead of exploring '
            "further."
        )

    # -- spec §29/§12: the living conversation state -------------------------

    def _problem_read(self, state: OnboardingState) -> tuple[str, str]:
        """A deterministic read of the standing problem from what the agent has
        learned, until the plan (and its insight) exists."""
        txt = plan_builder._text(state.learned, state.goal, state.money_moment)
        if plan_builder._short_runway(txt):
            return "cash_flow", "the buffer runs out before the month does"
        if plan_builder._income_unpredictable(txt) or plan_builder._leans_on_credit(
            txt
        ):
            return "income_volatility", "income arrives unevenly but spending is steady"
        if plan_builder._debt_heavy(txt):
            return "debt", "debt is eating the margin"
        if plan_builder._spending_leak(txt):
            return "spending", "spending leaks past the plan"
        if state.money_moment:
            return "cash_flow", "money runs out before the plan does"
        return "money", "map the day-to-day money flow first"

    def _directness_level(self, state: OnboardingState) -> int:
        """spec §12: directness follows relationship depth + pattern confidence
        + severity -- never the model's arbitrary tone. Clamped 1..4."""
        if state.stage in (
            STAGE_AWAITING_STATEMENT,
            STAGE_PLAN_CONSENT,
            STAGE_AWAITING_ADJUSTMENT,
            STAGE_COMPLETE,
        ):
            level = 3
        elif state.stage == STAGE_INTERVIEW and state.interview_turns >= 4:
            level = 2
        else:
            level = 1
        plan = state.plan or {}
        if plan.get("insight", {}).get("severity") == "high":
            level += 1
        return min(level, 4)

    def _update_conversation_state(self, state: OnboardingState) -> None:
        """Recompute the living read (spec §29) for the next turn: what the
        conversation is about, the standing problem, the pending action, the
        relationship stage and the directness level. Purely deterministic."""
        cs = state.conversation_state
        plan = state.plan or {}
        if plan and plan.get("insight"):
            cs["current_topic"] = plan["insight"].get("category", "")
            cs["current_problem"] = plan["insight"].get("title", "")
            cs["last_insight"] = plan["insight"].get("title", "")
            cs["pending_action"] = (
                "; ".join(s["title"] for s in plan.get("steps", [])[:3]) or ""
            )
        else:
            topic, problem = self._problem_read(state)
            cs["current_topic"] = topic
            cs["current_problem"] = problem
            if state.stage == STAGE_AWAITING_STATEMENT:
                cs["pending_action"] = "real numbers via a bank statement"
            elif not cs.get("pending_action"):
                cs["pending_action"] = ""
        if state.goal:
            cs["user_goal"] = state.goal
        if state.stage == STAGE_GREETING:
            cs["relationship_stage"] = "new"
        elif state.stage == STAGE_INTERVIEW:
            cs["relationship_stage"] = "getting_to_know"
        else:
            cs["relationship_stage"] = "established"
        cs["directness_level"] = self._directness_level(state)
        if plan and plan.get("insight"):
            cs["confidence"] = plan["insight"].get("confidence", cs.get("confidence"))
        else:
            n = len(state.learned) + bool(state.money_moment) + bool(state.goal)
            cs["confidence"] = round(
                min(0.45 + 0.05 * n + (0.15 if state.document_summary else 0), 0.95),
                2,
            )
        # Typed at the write boundary: what persists is always the validated
        # conversation state dump (extra keys from a corrupted store fail loud).
        try:
            state.conversation_state = ConversationState.model_validate(cs).model_dump()
        except ValidationError as exc:
            logger.warning("conversation_state failed contract validation: %s", exc)
            # Fail sanitized: persist a fresh typed seed instead of the raw
            # (possibly corrupt) read.
            state.conversation_state = ConversationState().model_dump()

    # -- greeting (name first, before any question) ---------------------------

    _NAME_PREFIX = re.compile(
        r"^(?:my name is|i am|i'm|call me|it's|it is|this is)\s+", re.IGNORECASE
    )

    _SKIP_GREETING = re.compile(
        r"(?:(?:\bskip\b|\blater\b|\bnever\s*mind\b|\bjust start\b|\bget going\b|"
        r"\blet'?s go\b|no thanks))",
        re.IGNORECASE,
    )

    def _greeting_turn(self) -> OnboardingTurn:
        return OnboardingTurn(
            took_over=True,
            response=(
                "Hey, I'm Miriam! Before we dive in - what should I call you? "
                "Just your first name works."
            ),
            stage=STAGE_GREETING,
        )

    async def _name_turn(
        self, user_id: str, state: OnboardingState, conversation_id: str, text: str
    ) -> OnboardingTurn:
        name = self._extract_name(text)
        if not name:
            return self._turn(
                "No stress - just tell me your first name and we'll get going.",
                stage=STAGE_GREETING,
            )
        state.name = name
        state.stage = STAGE_INTERVIEW
        await self._remember(user_id, "name", "identity", name, is_a_vote=False)
        await self._state_store.save_state(user_id, state)
        self._emit(user_id, "interview_started")
        # The conductor takes it from here: Miriam welcomes them by name and
        # opens with the Money Moment question in her own words.
        return await self._conductor_turn(
            user_id,
            state,
            conversation_id,
            text,
            event=f"the user told Miriam their name is {name}; welcome them and "
            "open the conversation",
        )

    @classmethod
    def _extract_name(cls, text: str) -> str:
        """Pull a first name out of a greeting reply. Defensive: names are prose,
        and a miss must never block the interview. "I'm Tobi, nice to meet you"
        and "It's Tobi!" both resolve to "Tobi"; trailing courtesy words and
        punctuation can never leak into the stored name."""
        t = cls._NAME_PREFIX.sub("", (text or "").strip())
        if not t or not t[0].isalpha():
            return ""
        head = re.split(r"[,!?.;]", t, maxsplit=1)[0].strip()
        words = [w for w in head.split() if any(ch.isalpha() for ch in w)]
        if not words:
            return ""
        words = [w.strip(" '\"`") for w in words]
        stopped = {
            "nice",
            "pleased",
            "good",
            "great",
            "hello",
            "hi",
            "hey",
            "greetings",
            "to",
            "meet",
            "know",
            "you",
            "meeting",
            "thanks",
            "thank",
        }
        while words and words[-1].casefold() in stopped:
            words.pop()
        words = words[:2]
        name = " ".join(words).strip(" ,.'\"`")
        if len(name) < 2 or len(name) > 40:
            return ""
        return name.title()

    def _volunteered_name(self, state: OnboardingState, text: str) -> str:
        """Honor an explicit name introduction mid-interview ("my name is Tobi",
        "call me Tobi", "I'm Tobi") without guessing. Bare words are answers,
        not names - so a marker has to be present, and the tail after it has to
        look like a name and nothing else."""
        if (state.name or "").strip():
            return state.name
        t = (text or "").strip()
        if not t or not t[0].isalpha():
            return ""
        m = _VOLUNTEERED_NAME.search(t)
        if not m:
            return ""
        rest = t[m.end() :].strip()
        # "I'm easy on the details" is a reply; "I'm Tobiloba" is a name.
        if not rest or len(rest.split()) > 3:
            return ""
        return self._extract_name(rest)

    async def _conductor_turn(
        self,
        user_id: str,
        state: OnboardingState,
        conversation_id: str,
        text: str,
        *,
        is_poll_vote: bool = False,
        event: str = "",
    ) -> OnboardingTurn:
        self._update_conversation_state(state)
        history = await self._history(conversation_id)
        try:
            outcome = await driver.conductor_turn(
                provider=self._llm(),
                state=state,
                history=history,
                user_text=text,
                is_poll_vote=is_poll_vote,
                event=event,
                moving_on_hint=self._moving_on_hint(state),
            )
        except Exception:
            logger.exception("onboarding LLM turn failed for user %s", user_id)
            outcome = None
        if outcome is None:
            return await self._fallback_turn(user_id, state, conversation_id, text)
        return await self._apply_outcome(
            user_id,
            state,
            conversation_id,
            text,
            outcome,
            is_poll_vote=is_poll_vote,
        )

    def _dimension_for(self, key: str) -> str:
        """Map a free-form fact label to a broad memory dimension (for the
        memory store only; the plan builder reads the raw text)."""
        k = key.casefold()
        if "goal" in k or "rich" in k or "life" in k:
            return "goal"
        if "moment" in k or "bother" in k or "worry" in k:
            return "liquidity"
        if "income" in k or "cash" in k or "earn" in k:
            return "cashflow"
        if "oblig" in k or "family" in k or "dependent" in k or "support" in k:
            return "obligations"
        if "debt" in k or "loan" in k or "credit" in k or "owe" in k:
            return "debt"
        if "spend" in k or "leak" in k or "behavior" in k or "discipline" in k:
            return "behavior"
        if "runway" in k or "short" in k or "buffer" in k or "savings" in k:
            return "liquidity"
        if "hand" in k or "auto" in k or "involv" in k:
            return "behavior"
        return "onboarding"

    def _lift_first_class(self, state: OnboardingState) -> None:
        """Promote the money moment and the goal out of the free-form fact set
        onto their first-class fields (only when they are not set yet, so the
        first articulation wins and later refinements keep refining it)."""
        for key in list(state.learned):
            if key.casefold() in _MONEY_MOMENT_KEYS and not state.money_moment:
                state.money_moment = state.learned.pop(key)
            elif key.casefold() in _GOAL_KEYS and not state.goal:
                state.goal = state.learned.pop(key)

    @staticmethod
    def _meta_float(value: str) -> float | None:
        """The money-moment confidence is the one numeric structured value.
        Anything unparseable is not a confidence, so it is dropped rather than
        stored as a junk string (which would fail the typed contract)."""
        try:
            return float(value[:_META_VALUE_MAX])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _meta_string(value: str) -> str:
        return value[:_META_VALUE_MAX]

    def _lift_meta(self, state: OnboardingState) -> None:
        """spec §6/§7/§22/§29: lift the reserved meta fact keys onto the
        structured fields (money-moment read, goal read, sentiment, money
        script) and off the free-form fact set, so they never pollute the plan
        text. The agent's first read wins."""
        for key in list(state.learned):
            k = key.casefold()
            if k == "money_moment_confidence":
                # Unparseable confidence (e.g. the LLM emitting "high"): the
                # key is consumed but nothing junk is stored.
                parsed = self._meta_float(state.learned.pop(key))
                if parsed is not None:
                    state.money_moment_meta["confidence"] = parsed
            elif k in _MONEY_MOMENT_META_KEYS:
                state.money_moment_meta[_MONEY_MOMENT_META_KEYS[k]] = self._meta_string(
                    state.learned.pop(key)
                )
            elif k in _GOAL_META_KEYS:
                state.goal_meta[_GOAL_META_KEYS[k]] = self._meta_string(
                    state.learned.pop(key)
                )
            elif k in _SENTIMENT_KEYS and not state.conversation_state.get(
                "user_sentiment"
            ):
                state.conversation_state["user_sentiment"] = self._meta_string(
                    state.learned.pop(key)
                )
            elif k in _MONEY_SCRIPT_KEYS:
                state.conversation_state["money_script"] = self._meta_string(
                    state.learned.pop(key)
                )
        self._validate_meta(state)

    @staticmethod
    def _sanitized_meta(raw: dict[str, Any], model: type[BaseModel]) -> dict[str, Any]:
        """Validate a meta dict field-by-field so one corrupt value (a string
        where the contract wants a float, an out-of-range emotion) sanitizes
        itself instead of either (a) failing whole-model validation and keeping
        the raw bad dict in Redis, or (b) nuking the valid fields beside it."""
        cleaned: dict[str, Any] = {}
        for name in model.model_fields:
            if name not in raw:
                continue
            value = raw[name]
            if value is None or value == "":
                continue
            try:
                model.model_validate({name: value})
            except ValidationError:
                logger.warning(
                    "meta field '%s' failed contract validation; dropping it",
                    name,
                )
                continue
            cleaned[name] = value
        try:
            return model.model_validate(cleaned).model_dump(
                exclude_none=True, exclude_defaults=True
            )
        except ValidationError:
            return {}

    @classmethod
    def _validate_meta(cls, state: OnboardingState) -> None:
        """Typed at the write boundary: the structured reads persist as the
        validated meta dumps, defaults dropped so empty reads stay {}."""
        state.money_moment_meta = cls._sanitized_meta(
            state.money_moment_meta, MoneyMomentMeta
        )
        state.goal_meta = cls._sanitized_meta(state.goal_meta, GoalMeta)

    async def _apply_outcome(
        self,
        user_id: str,
        state: OnboardingState,
        conversation_id: str,
        text: str,
        outcome: driver.DriverOutcome,
        *,
        is_poll_vote: bool = False,
    ) -> OnboardingTurn:
        # Persist any new facts first: words become the source of truth. The
        # money script gets a stable memory label (it is internal, never shown).
        for key, value in outcome.facts.items():
            if state.learned.get(key) == value:
                continue
            state.learned[key] = value
            is_script = key.casefold() in _MONEY_SCRIPT_KEYS
            await self._remember(
                user_id,
                "money_script" if is_script else key,
                "behavior" if is_script else self._dimension_for(key),
                value,
                is_a_vote=is_poll_vote,
            )
        self._lift_first_class(state)
        self._lift_meta(state)

        # An explicit name intro surfaced mid-interview rides on the reply so
        # the deterministic executor records it (never re-asking downstream).
        name = self._volunteered_name(state, text)
        if name and not state.name:
            state.name = name
            await self._remember(user_id, "name", "identity", name, is_a_vote=False)
            await self._state_store.save_state(user_id, state)

        # The explicit state machine decides the move; the handlers only
        # implement the action. Fail-open: any (stage, intent) without an edge
        # stays in place and replies.
        source_stage = state.stage
        transition = self._resolve_transition(source_stage, outcome.intent)
        if transition.action != _ACT_STAY:
            state.stage = transition.to

        # Append every LLM-led turn to the trace (drift tooling pairs it with
        # the prompt version hash). Grounded against the user's own words.
        violations = self._spec_violations(
            state,
            outcome,
            grounded_extra=text,
            present=False,
        )
        await self._trace_turn(
            user_id,
            state,
            outcome,
            mode="conductor",
            stage=source_stage,
            prompt_version=driver.prompt_version("conductor"),
            grounded_extra=text,
            violations=violations,
        )
        return await self._dispatch(
            user_id,
            state,
            conversation_id,
            text,
            outcome,
            transition,
            is_poll_vote=is_poll_vote,
            source_stage=source_stage,
            name=name,
        )

    @staticmethod
    def _resolve_transition(stage: str, intent: str) -> _Transition:
        return _TRANSITIONS.get((stage, intent), _DEFAULT_TRANSITION)

    async def _dispatch(
        self,
        user_id: str,
        state: OnboardingState,
        conversation_id: str,
        text: str,
        outcome: driver.DriverOutcome,
        transition: _Transition,
        *,
        is_poll_vote: bool,
        source_stage: str,
        name: str,
    ) -> OnboardingTurn:
        del is_poll_vote
        if transition.action == _ACT_ABANDON:
            await self._abandon(user_id, state, conversation_id)
            return self._abandon_turn(
                "No stress, I'll drop it here. "
                "Whenever you're ready, just say the word."
            )
        if transition.action == _ACT_COMPLETE_AUTOMATED:
            return await self._complete_automated(user_id, state, conversation_id)
        if transition.action == _ACT_COMPLETE_DRAFT:
            return await self._complete_draft(user_id, state, conversation_id)
        if transition.action == _ACT_REQUEST_STATEMENT:
            if source_stage == STAGE_INTERVIEW:
                self._emit(user_id, "interview_finished")
            self._emit(user_id, "statement_requested")
            self._update_conversation_state(state)
            await self._state_store.save_state(user_id, state)
            return self._turn_with_suggestions(outcome, conversation_id, state.stage)
        if transition.action == _ACT_PRESENT_PLAN:
            if source_stage == STAGE_INTERVIEW:
                self._emit(user_id, "interview_finished")
            return await self._present_plan(user_id, state, conversation_id)
        if transition.action == _ACT_ADJUST:
            return await self._apply_adjustment(
                user_id,
                state,
                conversation_id,
                outcome,
                text,
                source_stage=source_stage,
            )
        # _ACT_STAY (including the table's default): reply in this stage. The
        # interview cap nudges the agent to hand off; it keeps ticking.
        if source_stage == STAGE_INTERVIEW and outcome.intent == "interview":
            state.interview_turns += 1
        self._update_conversation_state(state)
        await self._state_store.save_state(user_id, state)
        return self._turn_with_suggestions(
            outcome, conversation_id, state.stage, name=name
        )

    def _turn_with_suggestions(
        self,
        outcome: driver.DriverOutcome,
        conversation_id: str,
        stage: str,
        *,
        name: str = "",
    ) -> OnboardingTurn:
        poll = None
        if outcome.suggested:
            poll = {"title": outcome.reply, "options": list(outcome.suggested)}
        return OnboardingTurn(
            took_over=True,
            response=outcome.reply,
            poll=poll,
            conversation_id=conversation_id,
            stage=stage,
            name=name,
        )

    async def _apply_adjustment(
        self,
        user_id: str,
        state: OnboardingState,
        conversation_id: str,
        outcome: driver.DriverOutcome,
        text: str,
        *,
        source_stage: str,
    ) -> OnboardingTurn:
        del text  # the model transcribes the change into outcome.adjustment
        note = (outcome.adjustment or "").strip()
        if not note:
            # The model signaled "adjust" but no concrete change came through:
            # ask for one (or, if already clarifying, keep asking). The
            # source stage tells the two apart -- the state machine already
            # advanced the stage, so we can't read intent from state.stage.
            if source_stage == STAGE_AWAITING_ADJUSTMENT:
                return self._turn(_TELL_ME_ASK, stage=state.stage)
            state.stage = STAGE_AWAITING_ADJUSTMENT
            await self._state_store.save_state(user_id, state)
            self._emit(user_id, "adjusting")
            return self._turn(_REWORK_ASK, stage=state.stage)
        state.adjustments.append(note)
        await self._remember(
            user_id,
            "adjustment",
            "goal",
            f"plan adjustment: {note}",
            is_a_vote=False,
        )
        # Fold the change in and re-present the reworked plan for consent.
        return await self._present_plan(user_id, state, conversation_id)

    @staticmethod
    def _grounding_text(state: OnboardingState, extra: str = "") -> str:
        """The texts every number in a reply may legally appear in: the user's
        own words (money moment, goal, statement summary, learned facts,
        adjustments) plus any extra context (the current message, or the
        deterministic plan). A figure in a reply that matches none of these is
        an invented number."""
        parts: list[str] = []
        if state.money_moment:
            parts.append(state.money_moment)
        if state.goal:
            parts.append(state.goal)
        if state.document_summary:
            parts.append(state.document_summary)
        for value in state.learned.values():
            parts.append(value)
        if state.adjustments:
            parts.extend(state.adjustments)
        if extra:
            parts.append(extra)
        return " ".join(parts)

    def _spec_violations(
        self,
        state: OnboardingState,
        outcome: driver.DriverOutcome,
        *,
        grounded_extra: str,
        present: bool,
    ) -> list[str]:
        """Lint one LLM reply against the spec, grounding numbers in the
        user's own words (or the deterministic plan for presentations)."""
        ground = self._grounding_text(state, extra=grounded_extra)
        return evaluate_reply(
            outcome.reply,
            EvalMeta(
                intent=outcome.intent,
                has_taps=bool(outcome.suggested),
                present=present,
                grounded=ground,
            ),
        )

    async def _trace_turn(
        self,
        user_id: str,
        state: OnboardingState,
        outcome: driver.DriverOutcome,
        *,
        mode: str,
        stage: str,
        prompt_version: str,
        grounded_extra: str,
        violations: list[str],
        clamped: bool = False,
    ) -> None:
        """Append one LLM-led turn to the trace and log any drift. Always
        fail-open: observability must never break the conversation."""
        if violations:
            logger.warning(
                "onboarding reply drifted (stage=%s, intent=%s, rules=%s): %r",
                stage,
                outcome.intent,
                ",".join(violations),
                outcome.reply,
            )
        try:
            await self._trace.append(
                TraceRecord(
                    user_id=user_id,
                    mode=mode,
                    stage=stage,
                    intent=outcome.intent,
                    reply=outcome.reply,
                    prompt_version=prompt_version,
                    violations=list(violations),
                    clamped=clamped,
                    facts=dict(outcome.facts),
                    taps=list(outcome.suggested),
                    adjustment=outcome.adjustment,
                    grounded=grounded_extra,
                )
            )
        except Exception:
            logger.debug("onboarding trace append failed (non-blocking)")

    @classmethod
    def _validated_plan(cls, plan: dict[str, Any]) -> dict[str, Any]:
        """Contract-gate the deterministic engine's output at the write
        boundary: what persists is always the typed :class:`Plan` dump. If the
        builder ever drifts from the contract, log it and keep the raw dict so
        the flow still completes (fail-open)."""
        try:
            return Plan.model_validate(plan).model_dump()
        except ValidationError as exc:
            logger.warning("deterministic plan failed contract validation: %s", exc)
            return plan

    async def _present_plan(
        self,
        user_id: str,
        state: OnboardingState,
        conversation_id: str,
    ) -> OnboardingTurn:
        plan = self._validated_plan(
            plan_builder.build_plan(
                state.learned,
                state.document_summary,
                goal=state.goal,
                money_moment=state.money_moment,
                adjustments=state.adjustments,
            )
        )
        state.plan = plan
        state.stage = STAGE_PLAN_CONSENT
        fresh_present = not state.plan_presented
        state.plan_presented = True
        self._update_conversation_state(state)
        await self._remember(
            user_id,
            "plan",
            "goal",
            plan["summary"],
            is_a_vote=True,
            extra={
                "diagnostic_state": plan["diagnostic_state"],
                "overlays": plan["overlays"],
            },
        )
        await self._state_store.save_state(user_id, state)
        self._emit(user_id, "plan_presented")
        # spec §27: success is a real insight, not a completed form. The plan
        # reveal (with its deterministic financial_insight) is that "aha" --
        # and it fires once, on the first reveal, not on every re-work.
        if fresh_present:
            self._emit(user_id, "aha_generated")

        history = await self._history(conversation_id)
        try:
            outcome = await driver.present_plan_turn(
                provider=self._llm(),
                state=state,
                history=history,
                plan=plan,
                adjustments=list(state.adjustments),
            )
        except Exception:
            logger.exception("onboarding plan-present LLM call failed for %s", user_id)
            outcome = None
        if outcome is not None:
            # Adversarial clamp (spec §30, never invent numbers): the plan
            # presentation is the money-critical surface, so a reply that
            # reports a figure not in the deterministic plan is never shown;
            # the deterministic presentation only repeats plan numbers. The
            # turn is traced either way, with the prompt version that produced
            # the drift.
            ground = json.dumps(plan)
            violations = self._spec_violations(
                state,
                outcome,
                grounded_extra=ground,
                present=True,
            )
            await self._trace_turn(
                user_id,
                state,
                outcome,
                mode="present",
                stage=state.stage,
                prompt_version=driver.prompt_version("present"),
                grounded_extra=ground,
                violations=violations,
                clamped="R10" in violations,
            )
            if "R10" in violations:
                return self._plan_turn(user_id, state, conversation_id)
            return self._turn(outcome.reply, stage=state.stage)
        return self._plan_turn(user_id, state, conversation_id)

    # -- deterministic fallback (LLM down / garbage) -------------------------

    async def _fallback_turn(
        self,
        user_id: str,
        state: OnboardingState,
        conversation_id: str,
        text: str,
    ) -> OnboardingTurn:
        # Same name-capture contract as the LLM-led turn, so the fallback path
        # never loses a name the user volunteered.
        name = self._volunteered_name(state, text)
        if name and not state.name:
            state.name = name
            await self._remember(user_id, "name", "identity", name, is_a_vote=False)
            await self._state_store.save_state(user_id, state)
        self._update_conversation_state(state)
        try:
            if state.stage == STAGE_INTERVIEW:
                # No question bank: fall back to a short, warm open question that
                # still moves the conversation -- then gather the statement so
                # the numbers stay real even without the agent online. Each
                # fallback turn consumes the inbound reply into the next open
                # field, so it never asks the same thing twice.
                state.interview_turns += 1
                just_named = bool(state.name) and (
                    self._extract_name(text).casefold() == state.name.casefold()
                )
                if just_named:
                    await self._state_store.save_state(user_id, state)
                    return self._turn(
                        f"Nice to meet you, {state.name}! What's been on your "
                        "mind about money lately?",
                        stage=state.stage,
                    )
                if not state.money_moment:
                    reply = (text or "").strip()
                    if reply:
                        state.money_moment = reply[:400]
                        await self._remember(
                            user_id,
                            "money_moment",
                            "liquidity",
                            state.money_moment,
                            is_a_vote=False,
                        )
                        await self._state_store.save_state(user_id, state)
                        return self._turn(
                            "And what would having that sorted out actually "
                            "look like for you - what are you working toward?",
                            stage=state.stage,
                        )
                    await self._state_store.save_state(user_id, state)
                    return self._turn(
                        "Sorry, give me one sec - my connection's being weird. "
                        "What's been on your mind about money lately, in your "
                        "own words?",
                        stage=state.stage,
                    )
                if not state.goal:
                    reply = (text or "").strip()
                    if reply:
                        state.goal = reply[:400]
                        await self._remember(
                            user_id, "goal", "goal", state.goal, is_a_vote=False
                        )
                        state.stage = STAGE_AWAITING_STATEMENT
                        await self._state_store.save_state(user_id, state)
                        self._emit(user_id, "interview_finished")
                        return self._statement_ask(user_id, state, conversation_id)
                    await self._state_store.save_state(user_id, state)
                    return self._turn(
                        "And what would having that sorted out actually look "
                        "like for you - what are you working toward?",
                        stage=state.stage,
                    )
                state.goal = self._fold_reply(text, state.goal)
                await self._remember(
                    user_id, "goal", "goal", state.goal, is_a_vote=False
                )
                state.stage = STAGE_AWAITING_STATEMENT
                await self._state_store.save_state(user_id, state)
                return self._statement_ask(user_id, state, conversation_id)

            if state.stage == STAGE_AWAITING_STATEMENT:
                option = _match_option(text, STATEMENT_POLL.options)
                if option == "Yes, send it now" or _lower(text) in _YES_PHRASES:
                    return self._turn(
                        "Great - send it over here, a PDF works best.",
                        stage=state.stage,
                    )
                if option == "Skip for now" or _lower(text) in _NO_PHRASES | _ABANDON:
                    return await self._present_plan(user_id, state, conversation_id)
                return self._statement_ask(user_id, state, conversation_id)

            if state.stage == STAGE_PLAN_CONSENT:
                option = _match_option(text, CONSENT_POLL.options)
                if option == "Yes, set it up":
                    return await self._complete_automated(
                        user_id, state, conversation_id
                    )
                if option == "Not now":
                    return await self._complete_draft(user_id, state, conversation_id)
                if option == "Let's adjust it first" or any(
                    h in _lower(text) for h in _ADJUST_HINTS
                ):
                    state.stage = STAGE_AWAITING_ADJUSTMENT
                    await self._state_store.save_state(user_id, state)
                    self._emit(user_id, "adjusting")
                    return self._turn(_REWORK_ASK, stage=state.stage)
                lower = _lower(text)
                if lower in _YES_PHRASES:
                    return await self._complete_automated(
                        user_id, state, conversation_id
                    )
                if lower in _NO_PHRASES:
                    return await self._complete_draft(user_id, state, conversation_id)
                return self._consent_poll_turn(user_id, state, conversation_id)

            if state.stage == STAGE_AWAITING_ADJUSTMENT:
                note = text.strip()
                if not note:
                    return self._turn(
                        "Tell me what to change in a sentence and I'll rework it.",
                        stage=state.stage,
                    )
                state.adjustments.append(note)
                await self._remember(
                    user_id,
                    "adjustment",
                    "goal",
                    f"plan adjustment: {note}",
                    is_a_vote=False,
                )
                await self._state_store.save_state(user_id, state)
                return await self._present_plan(user_id, state, conversation_id)
        except Exception:
            logger.exception("onboarding fallback turn failed for user %s", user_id)
        return OnboardingTurn(conversation_id=conversation_id)

    @staticmethod
    def _fold_reply(reply: str, current: str) -> str:
        """Append a fallback answer to an existing note without losing either.
        The agent labels facts when online; offline we simply keep their words."""
        reply = (reply or "").strip()
        if not reply:
            return current
        if not current:
            return reply[:400]
        if reply.casefold() in current.casefold():
            return current
        return f"{current} — {reply}"[:400]

    # -- completion ----------------------------------------------------------

    async def _complete_automated(
        self, user_id: str, state: OnboardingState, conversation_id: str
    ) -> OnboardingTurn:
        await self._persist_rules(user_id, state, automate=True)
        state.stage = STAGE_COMPLETE
        state.completed_at = time.time()
        await self._state_store.save_state(user_id, state)
        self._emit(user_id, "completed_automated")
        plank = state.plan or {}
        bullets = [s["title"].lower() for s in plank.get("steps", [])][:4]
        body = "Done - this is now how I work for you:\n"
        for b in bullets:
            body += f"\u2022 {b} first\n"
        body += (
            "\nI keep an eye on it and bring things up when they deserve attention. "
            "You stay the one who decides."
        )
        return OnboardingTurn(
            took_over=True,
            response=body,
            conversation_id=conversation_id,
            stage=STAGE_COMPLETE,
            completed=True,
            automated=True,
        )

    async def _complete_draft(
        self, user_id: str, state: OnboardingState, conversation_id: str
    ) -> OnboardingTurn:
        await self._persist_rules(user_id, state, automate=False)
        state.stage = STAGE_COMPLETE
        state.completed_at = time.time()
        await self._state_store.save_state(user_id, state)
        self._emit(user_id, "completed_draft")
        return OnboardingTurn(
            took_over=True,
            response=(
                "No problem at all. I've saved the plan - ask me to put it into "
                "action anytime and there's no need to go through this again."
            ),
            conversation_id=conversation_id,
            stage=STAGE_COMPLETE,
            completed=True,
            automated=False,
        )

    async def _abandon(
        self, user_id: str, state: OnboardingState, conversation_id: str
    ) -> None:
        del conversation_id
        state.stage = STAGE_COMPLETE
        state.completed_at = time.time()
        await self._state_store.save_state(user_id, state)
        self._emit(user_id, "abandoned")

    # -- reply builders -------------------------------------------------------

    def _statement_ask(
        self, user_id: str, state: OnboardingState, conversation_id: str
    ) -> OnboardingTurn:
        self._emit(user_id, "statement_requested")
        return OnboardingTurn(
            took_over=True,
            response=STATEMENT_POLL.title,
            poll={
                "title": STATEMENT_POLL.title,
                "options": list(STATEMENT_POLL.options),
            },
            conversation_id=conversation_id,
            stage=state.stage,
        )

    def _plan_turn(
        self, user_id: str, state: OnboardingState, conversation_id: str
    ) -> OnboardingTurn:
        del user_id
        plan = state.plan or {}
        label = plan.get("diagnostic_state", "your picture")
        text = f"Here's your picture: {label}."
        if plan.get("overlays"):
            text += f" ({', '.join(plan['overlays']).replace('_', ' ')})"
        steps = [s["title"] for s in plan.get("steps", [])]
        if steps:
            text += " My plan, in order:\n"
            for i, title in enumerate(steps, 1):
                text += f"{i}. {title}\n"
        text += "\nShould I set this up so it runs without being asked?"
        return OnboardingTurn(
            took_over=True,
            response=text,
            conversation_id=conversation_id,
            stage=state.stage,
        )

    def _consent_poll_turn(
        self, user_id: str, state: OnboardingState, conversation_id: str
    ) -> OnboardingTurn:
        self._emit(user_id, "consent_poll")
        return OnboardingTurn(
            took_over=True,
            response=CONSENT_POLL.title,
            poll={"title": CONSENT_POLL.title, "options": list(CONSENT_POLL.options)},
            conversation_id=conversation_id,
            stage=state.stage,
        )

    def _turn(self, response: str, *, stage: str) -> OnboardingTurn:
        return OnboardingTurn(took_over=True, response=response, stage=stage)

    def _abandon_turn(self, response: str) -> OnboardingTurn:
        return OnboardingTurn(
            took_over=True,
            response=response,
            stage=STAGE_COMPLETE,
            completed=True,
        )

    # -- persistence -----------------------------------------------------------

    @staticmethod
    def _emit(user_id: str, event: str) -> None:
        """Record a funnel milestone. Telemetry must never break the flow."""
        try:
            from miriam_agent.observability.metrics import ONBOARDING_EVENTS

            ONBOARDING_EVENTS.labels(user_id=user_id, event=event).inc()
        except Exception:
            logger.debug("onboarding metric emit failed (non-blocking)")

    async def _remember(
        self,
        user_id: str,
        question_id: str,
        dimension: str,
        content: str,
        *,
        is_a_vote: bool,
        extra: dict[str, Any] | None = None,
    ) -> None:
        try:
            meta = {
                "question_id": question_id,
                "dimension": dimension,
                "phase": "onboarding",
                "vote": is_a_vote,
            }
            if extra:
                meta.update(extra)
            await self._memory.store_memory(
                user_id, "onboarding", content, metadata=meta
            )
        except Exception:
            logger.warning("failed to store onboarding answer (non-blocking)")

    async def _persist_rules(
        self, user_id: str, state: OnboardingState, *, automate: bool
    ) -> None:
        plan = state.plan or {}
        try:
            await self._memory.store_memory(
                user_id,
                "financial",
                plan.get("summary") or "",
                metadata={
                    "diagnostic_state": plan.get("diagnostic_state", ""),
                    "overlays": plan.get("overlays", []),
                    "source": "onboarding",
                    "automate": automate,
                },
            )
            steps = " | ".join(s["title"] for s in plan.get("steps", []))
            if steps:
                await self._memory.store_memory(
                    user_id, "goal", steps, metadata={"source": "onboarding"}
                )
            if automate:
                rules = plan.get("standing_rules", [])
                for rule in rules:
                    await self._memory.store_memory(
                        user_id,
                        "pattern",
                        f"{rule['trigger']} -> {rule['action']} ({rule['cadence']})",
                        metadata={"kind": rule["kind"], "source": "onboarding"},
                    )
            await self._memory.store_memory(
                user_id,
                "onboarding",
                "onboarding completed",
                metadata={
                    "plan": plan,
                    "automate": automate,
                    "adjustments": state.adjustments,
                },
            )
        except Exception:
            logger.warning("failed to persist onboarding plan (non-blocking)")

    @staticmethod
    def _doc_summary(document: dict[str, Any] | None) -> str:
        if not document:
            return ""
        summary = (document.get("summary") or "").strip()
        if summary:
            return summary
        name = (document.get("name") or "").strip()
        return f"shared {name}" if name else ""

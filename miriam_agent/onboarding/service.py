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
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from miriam_agent.agents.llm import LLMProvider, get_llm_provider
from miriam_agent.config.settings import get_settings
from miriam_agent.onboarding import driver
from miriam_agent.onboarding import plan as plan_builder
from miriam_agent.onboarding.completion import (
    automated_completion_text,
    draft_completion_text,
    resume_payload,
)
from miriam_agent.onboarding.contracts import (
    ConversationState,
    Plan,
)
from miriam_agent.onboarding.gap_questions import (
    _ABANDON,
    _ADJUST_HINTS,
    _NO_PHRASES,
    _REENTER,
    _VOLUNTEERED_NAME,
    _YES_PHRASES,
    CONSENT_POLL,
    STATEMENT_POLL,
    OnboardingTurn,
    _confirms_readback,
    _is_short_yes,
    _lower,
    _match_option,
    _same_poll,
    _taps_for_reply,
)
from miriam_agent.onboarding.money_bridge import (
    absorb_reply,
    build_money_plan_dict,
    cap_should_present,
    fact_readback,
    gap_question,
    gap_taps,
    mark_gap_asked,
    money_readiness,
    next_unasked_gap,
    pay_rhythm_known,
    plan_bubbles,
    remember_poll,
    render_money_plan_text,
    reply_has_money_fact,
)
from miriam_agent.onboarding.money_facts import (
    _ACTION_INTENT,
    _MONEY_SCRIPT_KEYS,
    _asks_for_own_data,
    _wants_money_action,
    dimension_for,
    fact_violations,
    grounding_text,
    lift_first_class,
    lift_meta,
    meta_float,
    meta_string,
    sanitized_meta,
    validate_meta,
)
from miriam_agent.onboarding.quality import EvalMeta, evaluate_reply, hard_violations
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
from miriam_agent.utils.aha import detect_aha
from miriam_agent.utils.text import bubble_sets, clean_text, lift_reaction

logger = logging.getLogger(__name__)


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
class OnboardingService:
    """Advance the onboarding conversation for one user, one message at a time."""

    def __init__(
        self,
        memory_store: Any,
        state_store: Any | None = None,
        provider: LLMProvider | None = None,
        trace_store: Any | None = None,
        supermemory: Any | None = None,
    ) -> None:
        self._memory = memory_store
        self._state_store = state_store or get_onboarding_state_store()
        self._trace = trace_store or get_onboarding_trace_store()
        self._settings = get_settings()
        self._provider = provider
        # Injected by the chat entrypoint; ``None`` means memory mirroring is
        # off (keyless runs and tests), never a hidden network call.
        self._supermemory = supermemory

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
        if not self._settings.ONBOARDING_ENABLED:
            return OnboardingTurn(conversation_id=message or "")
        text = (message or "").strip()
        conversation_id = f"onboarding:{user.id}"
        doc_summary = self._doc_summary(document)

        state = await self._state_store.get_state(user.id)
        if state is not None and text:
            # spec v1.1 §50-51: an "aha" surfacing in their reply is the whole
            # point of the plan reveal -- mark it, remember it, never block on it.
            await self._check_for_aha(user.id, state.stage, text)
        if state is not None and state.complete:
            # An explicit redo/restart intent re-opens the interview from scratch
            # (completion is not a locked door; giving up never is either).
            if is_poll_vote:
                # A tap on an old poll (a consent/statement option from an earlier
                # session) must never read as silence against a finished sign-up:
                # acknowledge and re-open the door, without re-running the
                # interview or handing a stale option to the general agent.
                return self._turn(
                    "You're already all set from our earlier chat - that "
                    "option was from an older message. Is there anything else "
                    "you'd like to look at?",
                    stage=state.stage,
                )
            if not doc_summary and _REENTER.search(text):
                state = OnboardingState()
                state.stage = STAGE_INTERVIEW  # skip the greeting on a redo
                await self._state_store.clear(user.id)
                self._emit(user.id, "restarted")
                return await self._conductor_turn(
                    user.id, state, conversation_id, text, poll_title=poll_title
                )
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
                    poll_title=poll_title,
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

        # Requests the onboarding conductor cannot serve are never absorbed,
        # whatever stage we are at. This has to sit BEFORE the greeting handshake:
        # at that stage "show me my transactions" was read as someone introducing
        # themselves (the name came out as "Show Me"), so a whole class of real
        # requests vanished into the interview. The conductor has no tools, so
        # handing these to the agent and resuming afterwards is the only correct
        # answer. Poll votes stay in the flow -- a tap on "Yes, send it now" is a
        # statement answer, not a transfer.
        if not is_poll_vote and (_wants_money_action(text) or _asks_for_own_data(text)):
            return OnboardingTurn(conversation_id=conversation_id)

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
                    poll_title=poll_title,
                )
            return await self._name_turn(user.id, state, conversation_id, text)

        # Salary, pay rhythm, and fixed costs are read off the message itself.
        # The model is not required to notice "$50", "1", or "biweekly".
        if state.stage == STAGE_INTERVIEW and text:
            if state.awaiting_fact_confirm and not _confirms_readback(text):
                title = gap_question(["fixed_costs"])
                lowered = text.casefold()
                if any(
                    word in lowered
                    for word in (
                        "earn",
                        "income",
                        "salary",
                        "take home",
                        "take-home",
                        "comes in",
                    )
                ):
                    title = gap_question(["income_amount"])
                absorb_reply(state, text, poll_title=title)
            else:
                absorb_reply(state, text, poll_title=poll_title)
            ready_now, missing_now = money_readiness(state)
            if ready_now and state.awaiting_fact_confirm and _confirms_readback(text):
                state.awaiting_fact_confirm = False
                state.facts_confirmed = True
                await self._state_store.save_state(user.id, state)
                self._emit(user.id, "interview_finished")
                return await self._present_plan(user.id, state, conversation_id)
            if (
                ready_now
                and not state.facts_confirmed
                and not pay_rhythm_known(state)
                and "income_frequency" not in set(state.asked_gaps)
            ):
                return await self._ask_gap(user.id, state, "income_frequency")
            if ready_now and not state.facts_confirmed:
                if state.awaiting_fact_confirm:
                    # The facts were already read back once. A redelivered or
                    # retried turn must not re-send the identical sentence (users
                    # saw it twice, verbatim) — re-ask briefly instead.
                    return self._turn(
                        "Say yes and I'll lock this in, or change a number.",
                        stage=state.stage,
                    )
                state.awaiting_fact_confirm = True
                await self._state_store.save_state(user.id, state)
                return self._turn(fact_readback(state), stage=state.stage)
            if (
                not ready_now
                and "income_amount" not in missing_now
                and reply_has_money_fact(text)
            ):
                gap_key = next_unasked_gap(state, missing_now)
                if gap_key:
                    return await self._ask_gap(user.id, state, gap_key)
            if ready_now and state.facts_confirmed:
                self._emit(user.id, "interview_finished")
                return await self._present_plan(user.id, state, conversation_id)

        # Never let the interview drag. Past the cap, ask a missing fact once.
        # The same question is never sent again, and its taps have to answer it.
        if (
            state.stage == STAGE_INTERVIEW
            and state.interview_turns >= self._settings.ONBOARDING_MAX_QUESTIONS
        ):
            _ready, _missing = money_readiness(state)
            gap_key = (
                ""
                if cap_should_present(state, text) or _ready
                else next_unasked_gap(state, _missing)
            )
            if gap_key:
                question = gap_question([gap_key])
                taps = list(gap_taps([gap_key]))
                mark_gap_asked(state, gap_key)
                if taps:
                    remember_poll(state, question, taps)
                await self._state_store.save_state(user.id, state)
                gap_turn = self._turn(question, stage=state.stage)
                if taps:
                    gap_turn.poll = {"title": question, "options": taps}
                return gap_turn
            self._emit(user.id, "interview_finished")
            return await self._present_plan(user.id, state, conversation_id)

        if not text:
            return OnboardingTurn(conversation_id=conversation_id)

        # "Okay" on the plan is a yes. The conductor was reading it as a
        # request to rework, which sent people back into another interview.
        if state.stage == STAGE_PLAN_CONSENT and _is_short_yes(text):
            return await self._complete_automated(user.id, state, conversation_id)

        if is_poll_vote and state.stage in (
            STAGE_AWAITING_STATEMENT,
            STAGE_PLAN_CONSENT,
        ):
            # A tap carries the chosen option verbatim, so resolve it
            # deterministically against the poll that rendered it instead of
            # trusting the LLM to re-derive a consent/statement decision from a
            # few words. The deterministic fallback already implements exactly
            # these two stages; run it as the primary path for taps.
            return await self._fallback_turn(user.id, state, conversation_id, text)

        return await self._conductor_turn(
            user.id,
            state,
            conversation_id,
            text,
            is_poll_vote=is_poll_vote,
            poll_title=poll_title,
        )

    # -- LLM-led turns ------------------------------------------------------

    def _llm(self) -> LLMProvider:
        if self._provider is not None:
            return self._provider
        return get_llm_provider()

    async def _history(self, conversation_id: str) -> list[dict[str, Any]]:
        # No user_id is passed because onboarding never accepts a conversation
        # id from a client: it mints ``onboarding:{user.id}`` itself, so the id
        # is already bound to the authenticated user. Client-supplied ids go
        # through api.chat._require_owned_conversation instead.
        try:
            rows: list[dict[str, Any]] = await self._memory.get_conversation_history(
                conversation_id
            )
            return rows
        except Exception:
            logger.warning("onboarding history read failed (continuing blind)")
            return []

    def _moving_on_hint(self, state: OnboardingState) -> str:
        """Nudge the agent to present once numbers exist; hard cap still closes."""
        cap = self._settings.ONBOARDING_MAX_QUESTIONS
        ready, _ = money_readiness(state)
        if ready and state.stage == STAGE_INTERVIEW:
            return (
                "You already hold enough numbers for a real savings plan "
                '(income captured). Stop exploring: intent "present_plan" now '
                "so the user gets amounts, not more questions."
            )
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
                "Hey, I'm Miriam. I help with your money. "
                "What should I call you? Your first name is enough."
            ),
            stage=STAGE_GREETING,
        )

    async def _name_turn(
        self, user_id: str, state: OnboardingState, conversation_id: str, text: str
    ) -> OnboardingTurn:
        name = self._extract_name(text)
        if not name:
            return self._turn(
                "Just tell me your first name, and we can start.",
                stage=STAGE_GREETING,
            )
        state.name = name
        state.stage = STAGE_INTERVIEW
        await self._remember(user_id, "name", "identity", name, is_a_vote=False)
        await self._state_store.save_state(user_id, state)
        self._emit(user_id, "interview_started")
        turn = await self._conductor_turn(
            user_id,
            state,
            conversation_id,
            text,
            event=f"the user told Miriam their name is {name}; welcome them and "
            "open the conversation",
        )
        turn.name = turn.name or name
        return turn

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
        poll_title: str = "",
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
                poll_title=poll_title,
                moving_on_hint=self._moving_on_hint(state),
            )
        except Exception:
            logger.exception("onboarding LLM turn failed for user %s", user_id)
            outcome = None
        if outcome is None:
            return await self._fallback_turn(user_id, state, conversation_id, text)

        # spec §30, never invent numbers. _present_plan has always dropped a
        # presentation that reports a figure nobody stated; the conversation
        # itself only recorded the drift in the trace and sent it anyway, which
        # is how a number the user was never told reached them.
        #
        # Linting before _apply_outcome matters for more than ordering: it runs
        # before this turn's extracted facts are merged into ``state.learned``,
        # so a reply cannot ground its own invented figure in the fact it just
        # made up beside it.
        violations = self._spec_violations(
            state, outcome, grounded_extra=text, present=False
        )
        violations.extend(self._fact_violations(state, outcome, grounded_extra=text))
        if hard_violations(violations):
            retry = await self._reground(
                state=state,
                history=history,
                text=text,
                is_poll_vote=is_poll_vote,
                event=event,
                poll_title=poll_title,
            )
            if retry is not None:
                outcome = retry
                violations = self._spec_violations(
                    state, outcome, grounded_extra=text, present=False
                )
                violations.extend(
                    self._fact_violations(state, outcome, grounded_extra=text)
                )
        if hard_violations(violations):
            # The retry drifted too, or there was no retry to be had. A
            # deterministic line is a worse conversation and a better answer
            # than a confident figure nobody gave.
            await self._trace_turn(
                user_id,
                state,
                outcome,
                mode="conductor",
                stage=state.stage,
                prompt_version=driver.prompt_version("conductor"),
                grounded_extra=text,
                violations=violations,
                clamped=True,
            )
            return await self._fallback_turn(user_id, state, conversation_id, text)
        return await self._apply_outcome(
            user_id,
            state,
            conversation_id,
            text,
            outcome,
            is_poll_vote=is_poll_vote,
        )

    async def _reground(
        self,
        *,
        state: OnboardingState,
        history: list[dict[str, Any]],
        text: str,
        is_poll_vote: bool,
        event: str,
        poll_title: str,
    ) -> driver.DriverOutcome | None:
        """One re-ask after a reply reported a figure nobody stated.

        Mirrors the egress gate's regenerate step: retry once with the drift
        named, then let the caller fall back. Returns ``None`` when the retry
        itself failed, so the caller keeps the original outcome for the trace
        and still refuses to send it.
        """
        try:
            return await driver.conductor_turn(
                provider=self._llm(),
                state=state,
                history=history,
                user_text=text,
                is_poll_vote=is_poll_vote,
                event=event,
                poll_title=poll_title,
                moving_on_hint=self._moving_on_hint(state),
                correction=driver.GROUNDING_CORRECTION,
            )
        except Exception:
            logger.exception("onboarding grounding retry failed for the turn")
            return None

    def _dimension_for(self, key: str) -> str:
        return dimension_for(key)

    def _lift_first_class(self, state: OnboardingState) -> None:
        lift_first_class(state)

    @staticmethod
    def _meta_float(value: str) -> float | None:
        return meta_float(value)

    @staticmethod
    def _meta_string(value: str) -> str:
        return meta_string(value)

    def _lift_meta(self, state: OnboardingState) -> None:
        lift_meta(state)

    @staticmethod
    def _sanitized_meta(raw: dict[str, Any], model: type[BaseModel]) -> dict[str, Any]:
        return sanitized_meta(raw, model)

    @classmethod
    def _validate_meta(cls, state: OnboardingState) -> None:
        validate_meta(state)

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
            # Proactive readiness override: if we now hold enough numbers for
            # a real savings plan, stop interviewing and present it. The LLM
            # never gets to chat forever when the numbers are already in.
            _ready_now, _ = money_readiness(state)
            if _ready_now:
                if source_stage == STAGE_INTERVIEW:
                    self._emit(user_id, "interview_finished")
                return await self._present_plan(user_id, state, conversation_id)
        self._update_conversation_state(state)
        turn = self._turn_with_suggestions(
            outcome, conversation_id, state.stage, name=name, state=state
        )
        await self._state_store.save_state(user_id, state)
        return turn

    def _turn_with_suggestions(
        self,
        outcome: driver.DriverOutcome,
        conversation_id: str,
        stage: str,
        *,
        name: str = "",
        state: OnboardingState | None = None,
    ) -> OnboardingTurn:
        reply = clean_text(outcome.reply or "").strip()
        main, extras = bubble_sets(reply)
        poll = None
        taps = _taps_for_reply(reply, list(outcome.suggested))
        if (
            taps
            and state is not None
            and _same_poll(state, (main or reply).strip(), taps)
        ):
            # The same poll on a new bubble is how the chat fills with repeats.
            taps = []
        if taps:
            title = (main or reply).strip()
            if not title:
                # Never emit a poll with an empty title: spectrum-ts
                # validates title >=1 and an empty poll would fail to cache
                # (ZodError) and break subsequent votes.
                title = clean_text(outcome.reply or "").strip() or "What's your pick?"
            title = title.strip()
            # Extra guard: if title still looks empty or just "…", fallback
            if not title or title == "…" or len(title.strip("… ")) == 0:
                title = "What's your pick?"
            if title.strip():
                # Log for debugging empty title issues
                import logging as _logging

                _logging.getLogger(__name__).info(
                    "poll created title=%r options=%r", title, list(taps)
                )
                poll = {"title": title.strip(), "options": list(taps)}
                if state is not None:
                    remember_poll(state, title.strip(), taps)
        return OnboardingTurn(
            took_over=True,
            response=main,
            poll=poll,
            conversation_id=conversation_id,
            stage=stage,
            name=name,
            messages=extras,
            reaction=outcome.reaction or lift_reaction(reply),
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
        return grounding_text(state, extra)

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
                prev_user=grounded_extra,
            ),
        )

    def _fact_violations(
        self,
        state: OnboardingState,
        outcome: driver.DriverOutcome,
        *,
        grounded_extra: str,
    ) -> list[str]:
        return fact_violations(state, outcome, grounded_extra=grounded_extra)

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
        # The real savings plan: deterministic MoneyPlan with amounts. Stored
        # on state so the consent turn, memory, and resume all share it. The
        # legacy ``plan`` above stays for back-compat narrators/tests.
        money_plan = build_money_plan_dict(state)
        if money_plan is not None:
            state.money_plan = money_plan
            state.money_ready = True
            state.money_gap = []
        else:
            _ready, _missing = money_readiness(state)
            state.money_gap = list(_missing)
            state.money_ready = False
        self._update_conversation_state(state)
        remember_extra: dict[str, Any] = {
            "diagnostic_state": plan["diagnostic_state"],
            "overlays": plan["overlays"],
        }
        if state.money_plan:
            try:
                remember_extra["money_plan"] = {
                    "diagnosis": state.money_plan.get("diagnosis"),
                    "problem_type": state.money_plan.get("problem_type"),
                    "currency": state.money_plan.get("currency"),
                    "surplus_monthly": str(state.money_plan.get("surplus_monthly")),
                    "confidence": state.money_plan.get("confidence"),
                }
                _cf = state.money_plan.get("cashflow") or {}
                remember_extra["cashflow_split"] = {
                    k: str(_cf.get(k))
                    for k in ("fixed", "debt", "savings", "investments", "guilt_free")
                    if _cf.get(k) is not None
                }
                remember_extra["automation_rules"] = list(
                    state.money_plan.get("automation_rules") or []
                )[:5]
            except Exception:
                pass
        await self._remember(
            user_id,
            "plan",
            "goal",
            plan["summary"],
            is_a_vote=True,
            extra=remember_extra,
        )
        await self._state_store.save_state(user_id, state)
        self._emit(user_id, "plan_presented")
        # spec §27: success is a real insight, not a completed form. The plan
        # reveal (with its deterministic financial_insight) is that "aha" --
        # and it fires once, on the first reveal, not on every re-work.
        if fresh_present:
            self._emit(user_id, "aha_generated")

        # Deterministic savings-plan line: when the money pipeline produced a
        # real plan with amounts, it is remembered as the first assistant
        # message so chat history, resume, and memory all share it. The LLM
        # narration below still carries the voice; the deterministic text is
        # the fallback if the LLM drifts or is down.
        if state.money_plan:
            try:
                await self._memory.add_message(
                    conversation_id,
                    "assistant",
                    render_money_plan_text(state.money_plan),
                )
            except Exception:
                pass
        share = self._plan_share(user_id)
        take_home = None
        if state.money_plan:
            from miriam_agent.onboarding.money_bridge import _dec

            take_home = _dec(state.money_plan.get("monthly_take_home"))
        if state.money_plan and take_home is not None and take_home > 0:
            parts = [
                clean_text(part).strip() for part in plan_bubbles(state.money_plan)
            ]
            parts = [part for part in parts if part]
            # Traced as a present turn: the trace records every plan
            # presentation, and the deterministic bubbles are shown verbatim,
            # so the drift tooling sees exactly what the user saw.
            await self._trace_turn(
                user_id,
                state,
                driver.DriverOutcome(
                    reply=parts[0] if parts else "",
                    intent="present_plan",
                ),
                mode="present",
                stage=state.stage,
                prompt_version=driver.prompt_version("present"),
                grounded_extra=json.dumps(plan),
                violations=[],
                clamped=False,
            )
            return OnboardingTurn(
                took_over=True,
                response=(
                    parts[0]
                    if parts
                    else "Want me to lock this in, or change a number?"
                ),
                conversation_id=conversation_id,
                stage=state.stage,
                messages=parts[1:3],
                share=share,
            )
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
        share = self._plan_share(user_id)
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
                return self._plan_turn(user_id, state, conversation_id, share=share)
            return self._turn(
                outcome.reply,
                stage=state.stage,
                reaction=outcome.reaction,
                share=share,
            )
        return self._plan_turn(user_id, state, conversation_id, share=share)

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
                        f"Nice to meet you, {state.name}! So - what's been on "
                        "your mind about money lately?",
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
                        "Send it over here - a PDF works best.",
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
        return f"{current}, {reply}"[:400]

    # -- completion ----------------------------------------------------------

    async def _complete_automated(
        self, user_id: str, state: OnboardingState, conversation_id: str
    ) -> OnboardingTurn:
        await self._persist_rules(user_id, state, automate=True)
        state.stage = STAGE_COMPLETE
        state.completed_at = time.time()
        await self._state_store.save_state(user_id, state)
        self._emit(user_id, "completed_automated")
        body = automated_completion_text(state)
        return OnboardingTurn(
            took_over=True,
            response=body,
            conversation_id=conversation_id,
            stage=STAGE_COMPLETE,
            completed=True,
            automated=True,
            name=state.name,
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
            response=draft_completion_text(),
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

    def resume(self, state: OnboardingState) -> dict[str, Any]:
        """Resume payload for API/proactive: stage, done, missing, next step."""
        return resume_payload(state)

    def _plan_share(self, user_id: str) -> dict[str, Any] | None:
        """A rich link to the user's plan page, when the deployment configures
        the share base URL. The Go executor only delivers shares whose host is
        allowlisted via MIRIAM_SHARE_ALLOWED_HOSTS, so a misconfigured base
        URL degrades to no share instead of leaking a link."""
        base = (get_settings().ONBOARDING_SHARE_BASE_URL or "").rstrip("/")
        if not base:
            return None
        return {
            "kind": "plan",
            "title": "Your plan",
            "url": f"{base}/{user_id}",
        }

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

    async def _ask_gap(
        self, user_id: str, state: OnboardingState, gap_key: str
    ) -> OnboardingTurn:
        """Ask one scripted question. The model does not get to rephrase it."""
        question = gap_question([gap_key])
        taps = list(gap_taps([gap_key]))
        mark_gap_asked(state, gap_key)
        state.interview_turns += 1
        # Remember the question even when it has no taps, so the next number
        # is stored against this question and not the previous poll.
        remember_poll(state, question, taps)
        await self._state_store.save_state(user_id, state)
        gap_turn = self._turn(question, stage=state.stage)
        if taps:
            gap_turn.poll = {"title": question, "options": taps}
        return gap_turn

    def _plan_turn(
        self,
        user_id: str,
        state: OnboardingState,
        conversation_id: str,
        *,
        share: dict[str, Any] | None = None,
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
        clean = clean_text(text)
        main, extras = bubble_sets(clean)
        return OnboardingTurn(
            took_over=True,
            response=main,
            conversation_id=conversation_id,
            stage=state.stage,
            messages=extras,
            reaction=lift_reaction(clean),
            share=share,
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

    def _turn(
        self,
        response: str,
        *,
        stage: str,
        reaction: str = "",
        share: dict[str, Any] | None = None,
    ) -> OnboardingTurn:
        clean = clean_text(response or "").strip()
        main, extras = bubble_sets(clean)
        return OnboardingTurn(
            took_over=True,
            response=main,
            stage=stage,
            messages=extras,
            reaction=reaction or lift_reaction(clean),
            share=share,
        )

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

            # No user_id label: unbounded cardinality. user_id stays in logs.
            ONBOARDING_EVENTS.labels(event=event).inc()
            logger.debug("onboarding event", extra={"event": event, "user_id": user_id})
        except Exception:
            logger.debug("onboarding metric emit failed (non-blocking)")

    async def _check_for_aha(self, user_id: str, stage: str, text: str) -> None:
        """spec v1.1 §50 metric 6 / §51: detect an aha landing in the user's
        reply, count it, and file the bright-spot memory. Detection is
        deterministic (``utils.aha``) so analytics never depends on the LLM
        volunteering anything. Telemetry and memory are never allowed to break
        the conversation."""
        signal = detect_aha(text)
        if signal is None:
            return
        try:
            from miriam_agent.observability.metrics import MIRIAM_AHA_DETECTED

            MIRIAM_AHA_DETECTED.labels(kind=signal.kind, stage=stage).inc()
        except Exception:
            logger.debug("aha metric emit failed (non-blocking)")
        await self._remember(
            user_id,
            "aha_moment",
            "pattern",
            f"aha ({signal.kind}): {signal.phrase}",
            is_a_vote=False,
            extra={"kind": signal.kind, "text": signal.text[:200]},
        )

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
        # SQL is the system of record for onboarding answers; the memory graph
        # is where Miriam can *recall* them. Mirror the structured fact too, or
        # the interview's findings never reach the person's long-term memory.
        await self._mirror_to_supermemory(user_id, question_id, dimension, content)

    async def _mirror_to_supermemory(
        self, user_id: str, question_id: str, dimension: str, content: str
    ) -> None:
        """Append one decided onboarding fact to the person's memory graph.

        The interview *turns* already reach Supermemory through the chat path.
        These are the facts the interview actually settled, appended as system
        notes to the same stable onboarding document so they strengthen the
        graph without spawning a new source document per answer. Best-effort:
        memory must never break onboarding.
        """
        content = (content or "").strip()
        if not content:
            return
        service = self._supermemory
        if service is None or not getattr(service, "enabled", False):
            return
        try:
            from miriam_agent.integrations.supermemory_client import (
                container_tag_for,
                conversation_scope_for,
            )

            container_tag = container_tag_for(user_id)
            await service.ingest_note(
                container_tag=container_tag,
                conversation_id=conversation_scope_for(user_id, "onboarding"),
                content=f"[{dimension}/{question_id}] {content}",
                metadata={
                    "channel": "onboarding",
                    "kind": dimension,
                    "source": "miriam",
                },
            )
            # A name is the one fact worth pinning as a permanent trait, so the
            # always-on profile carries it from then on.
            if question_id == "name":
                await service.remember_person_fact(
                    container_tag,
                    f"The person's name is {content}",
                    kind="identity",
                    is_static=True,
                )
        except Exception as exc:  # noqa: BLE001 - memory never breaks onboarding
            logger.info("onboarding memory mirror failed (non-blocking): %s", exc)

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
            completed_meta: dict[str, Any] = {
                "plan": plan,
                "automate": automate,
                "adjustments": state.adjustments,
            }
            if state.money_plan:
                try:
                    _cf = state.money_plan.get("cashflow") or {}
                    completed_meta["money_plan"] = {
                        "diagnosis": state.money_plan.get("diagnosis"),
                        "problem_type": state.money_plan.get("problem_type"),
                        "currency": state.money_plan.get("currency"),
                        "surplus_monthly": str(state.money_plan.get("surplus_monthly")),
                        "confidence": state.money_plan.get("confidence"),
                    }
                    completed_meta["cashflow_split"] = {
                        k: str(_cf.get(k))
                        for k in (
                            "fixed",
                            "debt",
                            "savings",
                            "investments",
                            "guilt_free",
                        )
                        if _cf.get(k) is not None
                    }
                    completed_meta["automation_rules"] = list(
                        state.money_plan.get("automation_rules") or []
                    )[:5]
                except Exception:
                    pass
            await self._memory.store_memory(
                user_id,
                "onboarding",
                "onboarding completed",
                metadata=completed_meta,
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

"""The onboarding turn driver: one function per inbound message.

Flow (LLM-led, deterministic tail):

    first message        -> Miriam (the LLM) greets and starts the interview,
                            or bypasses for clear money-action intents
    interview turns      -> Miriam converses, classifying answers into the
                            canonical 7-dimension vocabulary
    interview done       -> Miriam asks for a bank statement (optional), then
                            the deterministic plan builder runs on the answers
    plan present         -> Miriam presents the engine's plan in her voice
    consent              -> deterministic consent mapping (yes -> standing
                            rules, no -> draft, adjust -> rework)
    complete             -> persist plan + standing rules as memory facts

The LLM decides WHAT to say and classifies answers; every state transition and
every money rule is deterministic. Polls stay as optional tap suggestions (the
LLM attaches ``suggested_replies``), and free text is always accepted. If the
LLM is unreachable or returns garbage, the flow degrades to the deterministic
interview (``questions.next_question``) so signup never stalls.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from miriam_agent.agents.llm import LLMProvider, get_llm_provider
from miriam_agent.config.settings import get_settings
from miriam_agent.onboarding import driver
from miriam_agent.onboarding import plan as plan_builder
from miriam_agent.onboarding import questions as q
from miriam_agent.onboarding.state import (
    STAGE_AWAITING_ADJUSTMENT,
    STAGE_AWAITING_STATEMENT,
    STAGE_COMPLETE,
    STAGE_INTERVIEW,
    STAGE_PLAN_CONSENT,
    OnboardingState,
    get_onboarding_state_store,
)

logger = logging.getLogger(__name__)

# Clear money-action intents that bypass the interview so a first message like
# "send 5k to Tola" or "what's my balance" is honored immediately instead.
_ACTION_INTENT = re.compile(
    r"\b(send|transfer|pay|withdraw|invest|buy|sell|swap|exchange|balance)\b",
    re.IGNORECASE,
)

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

# Phrases that restart an already-finished/abandoned interview.
_REENTER = re.compile(
    r"\b(redo|restart|start over|start again|from scratch|try again|"
    r"revisit\s+(my\s+)?plan|rework\s+(my\s+)?plan|change\s+(my\s+)?plan|"
    r"let's\s+(go|do it|do this|start|restart|try again))\b",
    re.IGNORECASE,
)

_REWORK_ASK = "Sure. What should we change? Tell me in a sentence and I'll rework it."
_TELL_ME_ASK = "Tell me what to change in a sentence and I'll rework it."


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
            },
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
    """Advance the onboarding interview for one user, one message at a time."""

    def __init__(
        self,
        memory_store: Any,
        state_store: Any | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self._memory = memory_store
        self._state_store = state_store or get_onboarding_state_store()
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
                state.plan = plan_builder.build_plan(state.answers, doc_summary)
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
            return await self._conductor_turn(user.id, state, conversation_id, text)

        # Never let the interview drag: cover the cores or the cap closes it.
        if (
            state.stage == STAGE_INTERVIEW
            and self._ready_for_plan(state)
            and len(state.asked) >= self._settings.ONBOARDING_MAX_QUESTIONS
        ):
            self._emit(user.id, "interview_finished")
            return await self._present_plan(user.id, state, conversation_id)

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

    def _ready_for_plan(self, state: OnboardingState) -> bool:
        return set(q.CORE_BY_ID) <= set(q.answered(state.answers))

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
        history = await self._history(conversation_id)
        try:
            outcome = await driver.conductor_turn(
                provider=self._llm(),
                state=state,
                history=history,
                user_text=text,
                is_poll_vote=is_poll_vote,
                event=event,
                ready_for_plan=self._ready_for_plan(state),
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
        # Persist any new answers first: words become the source of truth.
        for dim, value in outcome.answers.items():
            previous = state.answers.get(dim)
            state.answers[dim] = value
            state.raw[dim] = text
            if dim not in state.asked:
                state.asked.append(dim)
            if dim in q.FOLLOWUP_BY_ID and dim not in state.follow_ups:
                state.follow_ups.append(dim)
            if previous != value:
                question = q.CORE_BY_ID.get(dim) or q.FOLLOWUP_BY_ID.get(dim)
                dimension = question.dimension if question else dim
                await self._remember(
                    user_id, dim, dimension, value, is_a_vote=is_poll_vote
                )

        stage = state.stage
        intent = outcome.intent

        if intent == "abandon":
            await self._abandon(user_id, state, conversation_id)
            return self._abandon_turn(
                "No stress, I'll drop it here. "
                "Whenever you're ready, just say the word."
            )

        if intent == "consent_yes" and stage == STAGE_PLAN_CONSENT:
            return await self._complete_automated(user_id, state, conversation_id)
        if intent == "consent_no" and stage == STAGE_PLAN_CONSENT:
            return await self._complete_draft(user_id, state, conversation_id)

        if intent == "request_statement" and stage in (
            STAGE_INTERVIEW,
            STAGE_AWAITING_STATEMENT,
        ):
            if stage == STAGE_INTERVIEW:
                self._emit(user_id, "interview_finished")
            state.stage = STAGE_AWAITING_STATEMENT
            self._emit(user_id, "statement_requested")
            await self._state_store.save_state(user_id, state)
            return self._turn_with_suggestions(outcome, conversation_id, state.stage)

        if intent == "present_plan" and stage in (
            STAGE_INTERVIEW,
            STAGE_AWAITING_STATEMENT,
        ):
            if stage == STAGE_INTERVIEW:
                self._emit(user_id, "interview_finished")
            return await self._present_plan(user_id, state, conversation_id)

        if intent == "adjust":
            return await self._apply_adjustment(
                user_id, state, conversation_id, outcome, text
            )

        if intent == "done_adjusting" and stage == STAGE_AWAITING_ADJUSTMENT:
            return await self._present_plan(user_id, state, conversation_id)

        # Everything else: stay in the stage, reply (optionally with taps).
        # Persist regardless of whether a dimension was answered, so a fresh
        # interview state survives even on a bare conversational reply.
        await self._state_store.save_state(user_id, state)
        return self._turn_with_suggestions(outcome, conversation_id, stage)

    def _turn_with_suggestions(
        self,
        outcome: driver.DriverOutcome,
        conversation_id: str,
        stage: str,
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
        )

    async def _apply_adjustment(
        self,
        user_id: str,
        state: OnboardingState,
        conversation_id: str,
        outcome: driver.DriverOutcome,
        text: str,
    ) -> OnboardingTurn:
        del text  # the model transcribes the change into outcome.adjustment
        note = (outcome.adjustment or "").strip()
        if not note:
            # The model signaled "adjust" but no concrete change came through:
            # ask for one (or, if already clarifying, keep asking).
            if state.stage == STAGE_AWAITING_ADJUSTMENT:
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

    async def _present_plan(
        self,
        user_id: str,
        state: OnboardingState,
        conversation_id: str,
    ) -> OnboardingTurn:
        plan = plan_builder.build_plan(state.answers, state.document_summary)
        state.plan = plan
        state.stage = STAGE_PLAN_CONSENT
        state.plan_presented = True
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
        try:
            if state.stage == STAGE_INTERVIEW:
                nxt = q.next_question(
                    state.answers,
                    state.asked,
                    max_followups=self._settings.ONBOARDING_MAX_FOLLOWUPS,
                )
                if nxt is None:
                    if self._ready_for_plan(state):
                        return await self._present_plan(user_id, state, conversation_id)
                    state.stage = STAGE_AWAITING_STATEMENT
                    await self._state_store.save_state(user_id, state)
                    self._emit(user_id, "interview_finished")
                    return self._statement_ask(user_id, state, conversation_id)
                state.asked.append(nxt.id)
                if q.is_followup(nxt.id):
                    state.follow_ups.append(nxt.id)
                state.stage = STAGE_INTERVIEW
                await self._state_store.save_state(user_id, state)
                return self._question_turn(state, nxt, conversation_id)

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

    def _question_turn(
        self, state: OnboardingState, question: q.Question, conversation_id: str
    ) -> OnboardingTurn:
        return OnboardingTurn(
            took_over=True,
            response=question.prompt,
            poll={"title": question.prompt, "options": list(question.options)},
            conversation_id=conversation_id,
            stage=state.stage,
        )

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

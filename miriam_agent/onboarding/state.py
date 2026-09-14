"""Per-user onboarding progress state.

Miriam's onboarding runs across many turns, so progress must survive process
restarts and multiple instances. This store persists the whole onboarding state
per user: what Miriam has learned so far (a free-form fact set she extracts as
she talks), the current stage, and the plan. Redis-backed with an in-process
fallback, exactly like the proactive state store (fail-open everywhere: a
missing Redis can never break a reply).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from miriam_agent.config.settings import get_settings

logger = logging.getLogger(__name__)

# How long one onboarding may sit idle before it silently resets. Days. The
# timer slides on every turn (each save refreshes it), so an actively-used
# onboarding never expires mid-flow. Configurable via ONBOARDING_STATE_TTL_DAYS.
_KEY_PREFIX = "miriam:onboarding:state"

# Stages of the flow, in order.
STAGE_GREETING = "greeting"
STAGE_INTERVIEW = "interview"
STAGE_AWAITING_STATEMENT = "awaiting_statement"
STAGE_PLAN_CONSENT = "plan_consent"
STAGE_AWAITING_ADJUSTMENT = "awaiting_adjustment"
STAGE_COMPLETE = "complete"

ALL_STAGES = (
    STAGE_GREETING,
    STAGE_INTERVIEW,
    STAGE_AWAITING_STATEMENT,
    STAGE_PLAN_CONSENT,
    STAGE_AWAITING_ADJUSTMENT,
    STAGE_COMPLETE,
)

# Schema version stamped on every persisted record. Bump it when the state
# shape changes and add a migrator below so old on-disk records are brought
# forward on load instead of being lost.
SCHEMA_VERSION = 2


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """v1 -> v2: normalize sloppy persisted values we never want.

    Older records could carry an empty-``""`` document summary (from the Go
    sync scan) or an empty ``completed_at``; both should be null. A record
    without ``started_at`` gets one so expiry math is always well-defined.
    """
    cleaned = dict(data)
    if not cleaned.get("document_summary"):
        cleaned["document_summary"] = None
    if not cleaned.get("completed_at"):
        cleaned["completed_at"] = None
    if not cleaned.get("started_at"):
        cleaned["started_at"] = time.time()
    cleaned["schema_version"] = 2
    return cleaned


_MIGRATIONS: dict[int, Any] = {1: _migrate_v1_to_v2}


def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Bring a persisted record forward to ``SCHEMA_VERSION``, step by step."""
    version = int(data.get("schema_version") or 1)
    if version > SCHEMA_VERSION:
        # Written by a newer build. Keep it as-is rather than dropping fields
        # we don't know about yet (fail-open; it may still decode fine).
        logger.warning(
            "Onboarding state version %s is newer than supported %s",
            version,
            SCHEMA_VERSION,
        )
        return data
    current = version
    while current < SCHEMA_VERSION:
        step = _MIGRATIONS.get(current)
        if step is None:
            logger.warning("No migrator for onboarding state version %s", current)
            break
        data = step(data)
        current = int(data.get("schema_version") or current + 1)
    return data


class OnboardingState:
    """The serializable per-user onboarding state.

    Miriam leads the conversation; she decides what to ask and how to respond.
    The agent is not scripted: there is no fixed question bank. Instead she
    records whatever she learns as short free-form ``learned`` facts (keys are
    her own natural labels, e.g. ``cashflow``, ``debt``, ``goal``), which also
    feed the deterministic plan built at the end.
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = _migrate(data or {})
        self.schema_version: int = int(data.get("schema_version") or 1)
        self.stage: str = data.get("stage", STAGE_GREETING)
        # their first name, captured in the greeting.
        self.name: str = str(data.get("name") or "")
        # the opening Money Moment ("what's been bothering you about money?").
        self.money_moment: str = str(data.get("money_moment") or "")
        # spec §6: a structured read of the money moment (emotion, suspected
        # problem, confidence), lifted from reserved fact keys or {}.
        self.money_moment_meta: dict[str, Any] = dict(
            data.get("money_moment_meta") or {}
        )
        # the desired-life goal she converged on ("Japan trip next year").
        self.goal: str = str(data.get("goal") or "")
        # spec §7: a structured read of the goal (target date, cost, priority,
        # funding status), lifted from reserved fact keys or {}.
        self.goal_meta: dict[str, Any] = dict(data.get("goal_meta") or {})
        # free-form facts the agent extracted from the conversation. key is the
        # agent's own label; value is a short concrete fact in her words.
        # Coerced at the read boundary: a corrupt/non-str persisted value must
        # never blow up the turn (fail-open, never 500).
        self.learned: dict[str, str] = {
            k: (v if isinstance(v, str) else str(v))
            for k, v in dict(data.get("learned", {})).items()
            if v is not None
        }
        # statement scan summary (from Go's sync scan) or None.
        self.document_summary: str | None = data.get("document_summary")
        self.plan: dict[str, Any] | None = data.get("plan")
        # Whether the consent poll is on screen (vs. the plan-text turn).
        self.plan_presented: bool = bool(data.get("plan_presented"))
        # adjustment notes typed during plan review.
        self.adjustments: list[str] = list(data.get("adjustments", []))
        # number of interview turns served; drives the never-drag guard rail.
        self.interview_turns: int = int(data.get("interview_turns") or 0)
        # spec §29: a living read of where the conversation is, recomputed
        # deterministically every turn (current topic, standing problem, the
        # pending action, sentiment, relationship stage, directness level).
        self.conversation_state: dict[str, Any] = {
            "current_topic": "",
            "user_goal": "",
            "current_problem": "",
            "confidence": None,
            "last_insight": "",
            "pending_action": "",
            "user_sentiment": "",
            "relationship_stage": "new",
            "directness_level": 1,
        }
        self.conversation_state.update(data.get("conversation_state") or {})
        self.started_at: float = float(data.get("started_at") or time.time())
        self.completed_at: float | None = data.get("completed_at")
        self.updated_at: float = data.get("updated_at") or time.time()

    @property
    def complete(self) -> bool:
        return self.stage == STAGE_COMPLETE

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "stage": self.stage,
            "name": self.name,
            "money_moment": self.money_moment,
            "money_moment_meta": self.money_moment_meta,
            "goal": self.goal,
            "goal_meta": self.goal_meta,
            "learned": self.learned,
            "document_summary": self.document_summary,
            "plan": self.plan,
            "plan_presented": self.plan_presented,
            "adjustments": self.adjustments,
            "interview_turns": self.interview_turns,
            "conversation_state": self.conversation_state,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "updated_at": self.updated_at,
        }


class OnboardingStateStore:
    """Loads and persists :class:`OnboardingState` per user id."""

    def __init__(self, redis_url: str | None = None, ttl_days: int | None = None):
        settings = get_settings()
        self._ttl = (ttl_days or settings.ONBOARDING_STATE_TTL_DAYS) * 24 * 60 * 60
        self._url = redis_url
        if self._url is None:
            self._url = settings.REDIS_URL or None
        self._redis: Any = None
        if self._url:
            try:
                from redis import asyncio as aioredis

                self._redis = aioredis.from_url(self._url, decode_responses=True)
            except Exception as e:  # pragma: no cover - import/env issues
                logger.warning(
                    "Onboarding state Redis unavailable, using in-process store: %s",
                    e,
                )
                self._redis = None
        self._local: dict[str, dict[str, Any]] = {}

    def _key(self, user_id: str) -> str:
        return f"{_KEY_PREFIX}:{user_id}"

    async def get_state(self, user_id: str) -> OnboardingState | None:
        key = self._key(user_id)
        try:
            if self._redis is not None:
                raw = await self._redis.get(key)
                if raw:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        state = OnboardingState(data)
                        if int(data.get("schema_version") or 1) < SCHEMA_VERSION:
                            # Persist the migrated shape now so re-reads are
                            # fast and the record only migrates once.
                            await self.save_state(user_id, state)
                        return state
        except Exception as e:
            logger.debug("Onboarding state Redis read failed: %s", e)
        record = self._local.get(key)
        if record is not None:
            # Local fallback borrows the same sliding TTL so a stale onboarding
            # left before a restart is not resurrected indefinitely.
            if time.time() - float(record.get("updated_at") or 0) > self._ttl:
                self._local.pop(key, None)
                return None
            state = OnboardingState(record)
            if int(record.get("schema_version") or 1) < SCHEMA_VERSION:
                self._local[key] = state.to_dict()
            return state
        return None

    async def save_state(self, user_id: str, state: OnboardingState) -> None:
        state.updated_at = time.time()
        key = self._key(user_id)
        record = state.to_dict()
        self._local[key] = record
        try:
            if self._redis is not None:
                await self._redis.set(key, json.dumps(record), ex=self._ttl)
        except Exception as e:
            logger.debug("Onboarding state Redis write failed (local kept): %s", e)

    async def clear(self, user_id: str) -> None:
        key = self._key(user_id)
        self._local.pop(key, None)
        try:
            if self._redis is not None:
                await self._redis.delete(key)
        except Exception as e:
            logger.debug("Onboarding state Redis delete failed: %s", e)


_store: OnboardingStateStore | None = None


def get_onboarding_state_store() -> OnboardingStateStore:
    """Process-wide store singleton."""
    global _store
    if _store is None:
        _store = OnboardingStateStore()
    return _store

"""Per-user onboarding progress state.

Miriam's interview runs across many turns, so progress must survive process
restarts and multiple instances. This store persists the whole interview state
per user: which questions were asked, the collected answers, and the current
stage. Redis-backed with an in-process fallback, exactly like the proactive
state store (fail-open everywhere: a missing Redis can never break a reply).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from miriam_agent.config.settings import get_settings

logger = logging.getLogger(__name__)

# How long one interview may sit idle before it silently resets. Days. The
# timer slides on every turn (each save refreshes it), so an actively-used
# interview never expires mid-flow. Configurable via ONBOARDING_STATE_TTL_DAYS.
_KEY_PREFIX = "miriam:onboarding:state"

# Stages of the flow, in order.
STAGE_INTERVIEW = "interview"
STAGE_AWAITING_STATEMENT = "awaiting_statement"
STAGE_PLAN_CONSENT = "plan_consent"
STAGE_AWAITING_ADJUSTMENT = "awaiting_adjustment"
STAGE_COMPLETE = "complete"

ALL_STAGES = (
    STAGE_INTERVIEW,
    STAGE_AWAITING_STATEMENT,
    STAGE_PLAN_CONSENT,
    STAGE_AWAITING_ADJUSTMENT,
    STAGE_COMPLETE,
)


class OnboardingState:
    """The serializable per-user interview state."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        self.stage: str = data.get("stage", STAGE_INTERVIEW)
        # question id -> chosen option text (raw free text kept in ``raw``).
        self.answers: dict[str, str] = dict(data.get("answers", {}))
        # question id -> raw message text as typed (even non-matching).
        self.raw: dict[str, str] = dict(data.get("raw", {}))
        # ordered question ids asked so far.
        self.asked: list[str] = list(data.get("asked", []))
        # ids of adaptive follow-ups that fired.
        self.follow_ups: list[str] = list(data.get("follow_ups", []))
        # statement scan summary (from Go's sync scan) or None.
        self.document_summary: str | None = data.get("document_summary")
        self.plan: dict[str, Any] | None = data.get("plan")
        # Whether the consent poll is on screen (vs. the plan-text turn).
        self.plan_presented: bool = bool(data.get("plan_presented"))
        # adjustment notes typed during plan review.
        self.adjustments: list[str] = list(data.get("adjustments", []))
        self.started_at: float = float(data.get("started_at") or time.time())
        self.completed_at: float | None = data.get("completed_at")
        self.updated_at: float = data.get("updated_at") or time.time()

    @property
    def complete(self) -> bool:
        return self.stage == STAGE_COMPLETE

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "answers": self.answers,
            "raw": self.raw,
            "asked": self.asked,
            "follow_ups": self.follow_ups,
            "document_summary": self.document_summary,
            "plan": self.plan,
            "plan_presented": self.plan_presented,
            "adjustments": self.adjustments,
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
                        return OnboardingState(data)
        except Exception as e:
            logger.debug("Onboarding state Redis read failed: %s", e)
        record = self._local.get(key)
        if record is not None:
            # Local fallback borrows the same sliding TTL so a stale interview
            # left before a restart is not resurrected indefinitely.
            if time.time() - float(record.get("updated_at") or 0) > self._ttl:
                self._local.pop(key, None)
                return None
            return OnboardingState(record)
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

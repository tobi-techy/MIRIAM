"""One inbound message is one turn, however many times it is delivered.

The bridge dedupes in process memory for sixty seconds, so a webhook replay, a
retry after a timeout, or a second replica can hand the same provider message to
the platform again, and the platform to this agent. Without a check here each
delivery is a fresh model turn and a second reply.

A request that carries ``message_id`` is run under a claim in Redis, keyed on
the user and the id:

* first delivery: the claim is taken, the turn runs, and its response is stored;
* replay after the turn finished: the stored response is returned and the model
  does not run, so a caller that never got the first reply can still deliver it;
* replay while the first delivery is still running: the replay waits for it and
  then returns its stored response. It does not answer "duplicate" and stop: the
  first caller may have hung up (a timeout and retry is the usual way a replay
  arrives), and then nobody would deliver the reply.

A turn that raises releases its claim, and a waiting replay then runs the turn.
If Redis is down, or a first delivery is still running after the wait budget, the
turn runs: a lost reply costs the person more than a duplicate one.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder

logger = logging.getLogger(__name__)

# How long a claim for a turn in flight holds. A statement read can run ~150s.
PROCESSING_TTL_SECONDS = 180
# How long a replay waits for the first delivery before running the turn itself,
# kept under the platform's 60s timeout on this endpoint.
IN_FLIGHT_WAIT_SECONDS = 50.0
_IN_FLIGHT_POLL_SECONDS = 0.25
# How long a finished turn is remembered; longer than any provider redelivery.
DONE_TTL_SECONDS = 24 * 60 * 60
_PROCESSING = "processing"
_PREFIX = "miriam:turn:"


class RedisLike(Protocol):
    async def set(
        self, name: str, value: str, *, nx: bool = ..., ex: int | None = ...
    ) -> Any: ...

    async def get(self, name: str) -> Any: ...

    async def delete(self, *names: str) -> Any: ...


class TurnStore:
    """The claim-and-remember record for turns, on a shared Redis."""

    def __init__(self, redis: RedisLike) -> None:
        self._redis = redis

    async def begin(self, key: str) -> tuple[str, dict[str, Any] | None]:
        """``("new", None)``, ``("done", stored)`` or ``("in_flight", None)``."""
        name = _PREFIX + key
        if await self._redis.set(name, _PROCESSING, nx=True, ex=PROCESSING_TTL_SECONDS):
            return "new", None
        raw = await self._redis.get(name)
        if raw is None:
            # The holder released between our two calls: take the claim ourselves.
            if await self._redis.set(
                name, _PROCESSING, nx=True, ex=PROCESSING_TTL_SECONDS
            ):
                return "new", None
            return "in_flight", None
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        if text == _PROCESSING:
            return "in_flight", None
        return "done", json.loads(text)

    async def finish(self, key: str, payload: dict[str, Any]) -> None:
        await self._redis.set(
            _PREFIX + key, json.dumps(jsonable_encoder(payload)), ex=DONE_TTL_SECONDS
        )

    async def release(self, key: str) -> None:
        await self._redis.delete(_PREFIX + key)


_store: TurnStore | None = None


def get_turn_store() -> TurnStore | None:
    """The process-wide store on the shared Redis, or None when it is unusable."""
    global _store
    if _store is None:
        try:
            from miriam_agent.core.redis_client import build_redis_client

            _store = TurnStore(build_redis_client())
        except Exception:  # noqa: BLE001 - idempotency must never stop a turn
            logger.exception("turn idempotency store unavailable; failing open")
            return None
    return _store


def idempotent_post(router: APIRouter, path: str) -> Callable[..., Any]:
    """``router.post(path)`` for a turn endpoint taking ``body`` and ``user``."""

    def decorate(fn: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def run_once(*args: Any, **kwargs: Any) -> dict[str, Any]:
            body, user = kwargs.get("body"), kwargs.get("user")
            message_id = (getattr(body, "message_id", "") or "").strip()
            store = get_turn_store() if message_id and user is not None else None
            if store is None:
                return await fn(*args, **kwargs)
            key = f"{user.id}:{message_id}"
            loop = asyncio.get_running_loop()
            deadline = loop.time() + IN_FLIGHT_WAIT_SECONDS
            try:
                while True:
                    verdict, stored = await store.begin(key)
                    if verdict != "in_flight" or loop.time() >= deadline:
                        break
                    await asyncio.sleep(_IN_FLIGHT_POLL_SECONDS)
            except Exception:  # noqa: BLE001 - fail open, see module docstring
                logger.exception("turn idempotency check failed; running the turn")
                return await fn(*args, **kwargs)
            if verdict == "done" and stored is not None:
                logger.info("replayed a finished turn without running the model")
                return {**stored, "replayed": True}
            try:
                payload = await fn(*args, **kwargs)
            except BaseException:
                await _best_effort(store.release(key))
                raise
            await _best_effort(store.finish(key, payload))
            return payload

        return router.post(path)(run_once)

    return decorate


async def _best_effort(call: Awaitable[Any]) -> None:
    try:
        await call
    except Exception:  # noqa: BLE001 - the turn already has its answer
        logger.exception("turn idempotency bookkeeping failed")

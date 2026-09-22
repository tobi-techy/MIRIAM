"""Typed evaluation of a state against a catalog.

``evaluate`` is the only place that talks to TypeSafe. Any SDK error (network,
timeout, or HTTP) is translated into :class:`JudgmentUnavailableError`; the
gate decides whether to fail open or fail closed, because that is a policy
decision, not a transport decision.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel
from typesafe_sdk import AsyncTypeSafeClient, SystemOneResponse, TypeSafeError

from miriam_agent.judgment.client import get_async_client
from miriam_agent.judgment.questions import Catalog

logger = logging.getLogger(__name__)

# Any catalog's state: the ingress/tool/egress state model, the money STATE, or
# an already-serialized payload. `evaluate` only ever calls `model_dump` on it.
StatePayload = BaseModel | dict[str, Any]


class JudgmentUnavailableError(Exception):
    """Raised when a safety-critical judgment could not be evaluated."""

    def __init__(self, catalog: str, reason: str):
        super().__init__(f"TypeSafe '{catalog}' unavailable: {reason}")
        self.catalog = catalog
        self.reason = reason


async def evaluate(
    state: StatePayload,
    catalog: Catalog,
    *,
    client: AsyncTypeSafeClient | None = None,
) -> SystemOneResponse:
    """Evaluate ``state`` against ``catalog`` and return the typed response."""
    client = client or get_async_client()
    payload = (
        state.model_dump(exclude_none=True) if isinstance(state, BaseModel) else state
    )
    start = time.perf_counter()
    try:
        response = await client.system_one(
            state=payload,
            questions=catalog.questions,
            response_model=catalog.response_model,
        )
    except TypeSafeError as exc:
        logger.error(
            "typesafe evaluate failed",
            extra={"catalog": catalog.name, "error": str(exc)},
        )
        raise JudgmentUnavailableError(catalog.name, str(exc)) from exc

    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    try:
        request_id = response.request_id
    except Exception:  # pragma: no cover - logging must never break the path
        request_id = ""
    usage = response.usage
    logger.info(
        "typesafe request",
        extra={
            "catalog": catalog.name,
            "catalog_version": catalog.version,
            "request_id": request_id,
            "model": response.model,
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "latency_ms": latency_ms,
        },
    )
    return response

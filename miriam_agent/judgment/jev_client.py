"""Layer 2 - JUDGMENT. The JEV client for money questions.

The only place Layer 2 talks to JEV. Every transport problem becomes a
:class:`JudgeUnavailable`, and the caller fails closed: a judgment that could
not be made is never a green light.

A chat model is not welcome here. This module sends a frozen catalog of typed
questions and receives typed answers. It never sends prose, never asks for a
sentence back, and never accepts an amount.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from typesafe_sdk import AsyncTypeSafeClient

from miriam_agent.hands.state import HandlerState
from miriam_agent.judgment.client import enabled
from miriam_agent.judgment.schema import MONEY, MoneyJudgment
from miriam_agent.judgment.service import JudgmentUnavailableError, evaluate

logger = logging.getLogger(__name__)


class JudgeUnavailable(Exception):
    """The money judgment could not be evaluated. Fail closed, always."""

    def __init__(self, reason: str):
        super().__init__(f"JEV unavailable for the money catalog: {reason}")
        self.reason = reason


def money_state_payload(state: HandlerState, utterance: str = "") -> dict[str, Any]:
    """The STATE as JEV should see it: JSON-safe, no ``None`` padding.

    ``decision`` and ``execution`` are dropped when empty because this call runs
    *before* a decision exists, and handing a model an empty decision field
    invites it to fill one in. The user's raw words ride alongside STATE as
    ``turn_text``, which is the only thing the intent question reads.
    """
    payload = state.to_dict()
    for key in ("decision", "execution"):
        if payload.get(key) is None:
            payload.pop(key, None)
    payload["turn_text"] = utterance or ""
    return payload


async def evaluate_money(
    state: HandlerState,
    utterance: str = "",
    *,
    client: AsyncTypeSafeClient | None = None,
) -> MoneyJudgment:
    """Answer the seven money questions for this STATE.

    Raises :class:`JudgeUnavailable` on any transport or configuration problem
    rather than returning a default, so the rules layer has one thing to handle.
    """
    if not enabled():
        raise JudgeUnavailable("JEV is disabled or has no API key")
    try:
        response = await evaluate(
            money_state_payload(state, utterance), MONEY, client=client
        )
    except JudgmentUnavailableError as exc:
        raise JudgeUnavailable(exc.reason) from exc
    except Exception as exc:  # noqa: BLE001 - any failure is an unavailable judge
        logger.warning("money judgment failed: %s", exc)
        raise JudgeUnavailable(str(exc)) from exc
    return cast(MoneyJudgment, response)


__all__ = ["JudgeUnavailable", "evaluate_money", "money_state_payload"]

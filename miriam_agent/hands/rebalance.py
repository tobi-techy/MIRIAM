"""Layer 1 - HANDS. Rebalance / pause / resume leg of the Glider sleeve.

Deterministic code only. No LLM, no JEV.

These three mutations run immediately on a Hands-issued confirm_id tap -- no
staging, because there is no wallet signature to obtain. A 429 from the
provider is a cooldown, surfaced as a queued receipt plus a retry-after card,
never as a silent failure.

Fail closed: missing strategy id, provider error, or no execution id --
receipt rejected, nothing moves. Split out of hands/orders.py to keep both
modules under the size ratchet.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from miriam_agent.core.exceptions import CoolingDownError
from miriam_agent.hands.audit import Receipt, sleeves_snapshot
from miriam_agent.hands.ledger import Ledger, LedgerStore
from miriam_agent.hands.orders import ORDER_SLEEVE, ORDER_TTL_MINUTES, _id, _utcnow

logger = logging.getLogger(__name__)


# --- immediate: rebalance (no staging, cooldown surfaces Retry-After) ---


def _cooldown_card(retry_after: int | None) -> dict[str, Any]:
    wait = f" in {retry_after}s" if retry_after else ""
    return {
        "kind": "rebalance_queued",
        "title": "REBALANCE",
        "subtitle": "Glider \u00b7 Rail Stock Sleeve",
        "primary": f"Cooling down \u2014 try again{wait}",
        "amount": "",
        "source": "sleeve",
        "cta": "Wait",
        "note": "The strategy is cooling down after a recent rebalance.",
        "execution_id": "",
        "operation_id": "",
        "expires_in_sec": ORDER_TTL_MINUTES * 60,
    }


async def prepare_rebalance(
    *,
    store: LedgerStore,
    ledger: Ledger,
    user_id: str,
    token: str,
    strategy_id: str,
    reason: str | None = None,
    rebalance_call: Callable[..., Awaitable[dict[str, Any]]],
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any] | None, Ledger]:
    """Run a strategy rebalance immediately. No staging; cooldown surfaces."""
    timestamp = at if at is not None else _utcnow()
    before = sleeves_snapshot(ledger.sleeves)

    def _reject_reb(reasons: list[str], detail: str) -> tuple[Receipt, None, Ledger]:
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action="rebalance_prepare",
            currency=ledger.currency,
            counterparty="Rail Stock Sleeve",
            sleeve=ORDER_SLEEVE,
            decision_id=strategy_id,
            idempotency_key=f"rebalance-prepare:{strategy_id}",
            reasons=reasons,
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            detail=detail,
        )
        ledger.remember_receipt(receipt)
        return receipt, None, ledger

    if not strategy_id.strip():
        return _reject_reb(["BAD_STRATEGY"], "no strategy id supplied; nothing moved")

    try:
        result = await rebalance_call(strategy_id, reason)
    except CoolingDownError as exc:
        card = _cooldown_card(exc.retry_after)
        detail = "rebalance refused: strategy cooling down" + (
            f"; retry in {exc.retry_after}s" if exc.retry_after else ""
        )
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="queued",
            action="rebalance_prepare",
            currency=ledger.currency,
            counterparty="Rail Stock Sleeve",
            sleeve=ORDER_SLEEVE,
            decision_id=strategy_id,
            idempotency_key=f"rebalance-prepare:{strategy_id}",
            reasons=["COOLDOWN"],
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            detail=detail,
        )
        ledger.remember_receipt(receipt)
        await store.save(ledger)
        return receipt, card, ledger
    except Exception as exc:  # noqa: BLE001 - a provider failure is a business result
        logger.warning("rebalance prepare failed: %s", exc)
        return _reject_reb(
            ["GLIDER_NOT_LIVE"],
            f"the rebalance did not start ({exc}); nothing moved",
        )
    if not isinstance(result, dict):
        return _reject_reb(["NO_EXECUTION"], "the provider returned no result")
    execution = result.get("execution", {})
    if not isinstance(execution, dict):
        execution = {}
    execution_id = str(
        execution.get("id")
        or result.get("execution_id")
        or result.get("executionId")
        or result.get("operation_id")
        or result.get("operationId")
        or ""
    )
    if not execution_id:
        return _reject_reb(
            ["NO_EXECUTION"],
            "the provider returned no execution id; nothing is claimed",
        )
    card: dict[str, Any] = {
        "kind": "rebalance_confirm",
        "title": "REBALANCE",
        "subtitle": "Glider \u00b7 Rail Stock Sleeve",
        "primary": "Sleeve rebalanced",
        "amount": "",
        "source": "sleeve",
        "cta": "Track",
        "note": "Track with execution status.",
        "execution_id": execution_id,
        "operation_id": str(
            result.get("operation_id") or result.get("operationId") or ""
        ),
        "strategy_id": strategy_id,
        "expires_in_sec": ORDER_TTL_MINUTES * 60,
    }
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="executed",
        action="rebalance_prepare",
        currency=ledger.currency,
        counterparty="Rail Stock Sleeve",
        sleeve=ORDER_SLEEVE,
        decision_id=strategy_id,
        idempotency_key=f"rebalance-prepare:{strategy_id}",
        rail_reference=execution_id,
        confirm_id=strategy_id,
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        detail=f"rebalanced sleeve -> execution {execution_id}",
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    return receipt, card, ledger


# --- immediate: pause / resume (no staging) ---


async def _prepare_pause_resume(
    *,
    store: LedgerStore,
    ledger: Ledger,
    action: str,
    strategy_id: str,
    call: Callable[..., Awaitable[dict[str, Any]]],
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any] | None, Ledger]:
    timestamp = at if at is not None else _utcnow()
    before = sleeves_snapshot(ledger.sleeves)

    def _reject_pr(reasons: list[str], detail: str) -> tuple[Receipt, None, Ledger]:
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action=f"{action}_prepare",
            currency=ledger.currency,
            counterparty="Rail Stock Sleeve",
            sleeve=ORDER_SLEEVE,
            decision_id=strategy_id,
            idempotency_key=f"{action}-prepare:{strategy_id}",
            reasons=reasons,
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            detail=detail,
        )
        ledger.remember_receipt(receipt)
        return receipt, None, ledger

    if not strategy_id.strip():
        return _reject_pr(["BAD_STRATEGY"], "no strategy id supplied; nothing moved")

    try:
        result = await call(strategy_id)
    except Exception as exc:  # noqa: BLE001 - a provider failure is a business result
        logger.warning("%s prepare failed: %s", action, exc)
        return _reject_pr(
            ["GLIDER_NOT_LIVE"],
            f"{action} did not run ({exc}); nothing moved",
        )
    if not isinstance(result, dict):
        return _reject_pr(["NO_EXECUTION"], "the provider returned no result")
    card: dict[str, Any] = {
        "kind": "order_confirm",
        "title": action.upper(),
        "subtitle": "Glider \u00b7 Rail Stock Sleeve",
        "primary": f"Sleeve {action}d",
        "amount": "",
        "source": "sleeve",
        "cta": "Done",
        "note": "",
        "execution_id": str(result.get("execution_id") or ""),
        "operation_id": "",
        "strategy_id": strategy_id,
        "expires_in_sec": ORDER_TTL_MINUTES * 60,
    }
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="executed",
        action=f"{action}_prepare",
        currency=ledger.currency,
        counterparty="Rail Stock Sleeve",
        sleeve=ORDER_SLEEVE,
        decision_id=strategy_id,
        idempotency_key=f"{action}-prepare:{strategy_id}",
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        detail=f"sleeve {action}d",
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    return receipt, card, ledger


async def prepare_pause(
    *,
    store: LedgerStore,
    ledger: Ledger,
    user_id: str,
    token: str,
    strategy_id: str,
    pause_call: Callable[..., Awaitable[dict[str, Any]]],
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any] | None, Ledger]:
    """Pause the sleeve strategy immediately (no staging)."""
    return await _prepare_pause_resume(
        store=store,
        ledger=ledger,
        action="pause",
        strategy_id=strategy_id,
        call=pause_call,
        at=at,
    )


async def prepare_resume(
    *,
    store: LedgerStore,
    ledger: Ledger,
    user_id: str,
    token: str,
    strategy_id: str,
    resume_call: Callable[..., Awaitable[dict[str, Any]]],
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any] | None, Ledger]:
    """Resume the sleeve strategy immediately (no staging)."""
    return await _prepare_pause_resume(
        store=store,
        ledger=ledger,
        action="resume",
        strategy_id=strategy_id,
        call=resume_call,
        at=at,
    )

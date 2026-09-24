"""Layer 1 - HANDS. Glider order/set-allocation leg.

Deterministic code only. No LLM, no JEV.

Orders and set-allocation shifts run through this module after a
Hands-issued confirm_id tap. Utterance parsing lives in hands/nl.py and is
re-exported here so existing import paths keep working.

Settlement model: staged mutation -> _obtain_and_replay, Rail-signed
server-side (no wallet signature needed).

Fail closed everywhere: unknown symbol, amount breach, no enrollment,
provider error - receipt rejected, nothing moves.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from miriam_agent.hands.audit import Receipt, sleeves_snapshot
from miriam_agent.hands.invest import _obtain_and_replay, _sleeve_from_catalogue
from miriam_agent.hands.ledger import Ledger, LedgerStore, PendingOrder, money
from miriam_agent.hands.nl import ORDER_SLEEVE as ORDER_SLEEVE
from miriam_agent.hands.nl import _parse_side as _parse_side
from miriam_agent.hands.nl import _parse_symbol as _parse_symbol
from miriam_agent.hands.nl import parse_order_utterance as parse_order_utterance
from miriam_agent.hands.nl import (
    parse_performance_utterance as parse_performance_utterance,
)
from miriam_agent.hands.nl import parse_rebalance_utterance as parse_rebalance_utterance

logger = logging.getLogger(__name__)

# Short TTL: the allocation shift converges on the next rebalance.
ORDER_TTL_MINUTES = 15


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


__all__ = [
    "ORDER_SLEEVE",
    "ORDER_TTL_MINUTES",
    "parse_order_utterance",
    "parse_performance_utterance",
    "parse_rebalance_utterance",
    "prepare_order",
    "prepare_set_allocation",
    "settle_order",
    "settle_set_allocation",
]


def _pick_asset(assets: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    """Exact symbol match from a search_assets response, or None."""
    want = (symbol or "").strip().upper()
    if not want:
        return None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        for key in ("symbol", "Symbol"):
            have = str(asset.get(key) or "").strip().upper()
            if have and have == want:
                return asset
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        base = str(asset.get("symbol") or asset.get("Symbol") or "").strip().upper()
        if base == want or base.rstrip("X") == want.rstrip("X"):
            return asset
    return None


async def prepare_order(
    *,
    store: LedgerStore,
    ledger: Ledger,
    user_id: str,
    token: str,
    side: str,
    symbol: str,
    amount_usd: Decimal,
    decision_id: str = "",
    list_strategies: Callable[..., Awaitable[dict[str, Any]]],
    search_assets: Callable[..., Awaitable[dict[str, Any]]],
    order_call: Callable[..., Awaitable[dict[str, Any]]],
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any] | None, Ledger]:
    """Run a Glider buy/sell after the tap and return the execution card."""
    timestamp = at if at is not None else _utcnow()
    before = sleeves_snapshot(ledger.sleeves)
    if amount_usd is None:
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action="order_prepare",
            currency=ledger.currency,
            counterparty=symbol or "Rail Stock Sleeve",
            sleeve=ORDER_SLEEVE,
            decision_id=decision_id,
            idempotency_key=f"order-prepare:{decision_id}:{side}:{symbol}:none",
            reasons=["BAD_AMOUNT"],
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            detail="no order amount was named; nothing moved",
        )
        ledger.remember_receipt(receipt)
        return receipt, None, ledger
    try:
        amount = money(amount_usd)
    except Exception:
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action="order_prepare",
            currency=ledger.currency,
            counterparty=symbol or "Rail Stock Sleeve",
            sleeve=ORDER_SLEEVE,
            decision_id=decision_id,
            idempotency_key=f"order-prepare:{decision_id}:{side}:{symbol}:bad",
            reasons=["BAD_AMOUNT"],
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            detail="the order amount was not a number; nothing moved",
        )
        ledger.remember_receipt(receipt)
        return receipt, None, ledger
    # Idempotency first: a double-tap replays the original receipt instead
    # of running quote -> initiate -> create on the provider a second time.
    _key = f"order-prepare:{decision_id}:{side}:{symbol}:{amount}"
    _prior = ledger.receipt_for(_key)
    if _prior is not None:
        return _prior, None, ledger

    def _reject(reasons: list[str], detail: str) -> tuple[Receipt, None, Ledger]:
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action="order_prepare",
            currency=ledger.currency,
            amount=amount,
            counterparty=symbol or "Rail Stock Sleeve",
            sleeve=ORDER_SLEEVE,
            decision_id=decision_id,
            idempotency_key=f"order-prepare:{decision_id}:{side}:{symbol}:{amount}",
            reasons=reasons,
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            detail=detail,
        )
        ledger.remember_receipt(receipt)
        return receipt, None, ledger

    if side not in ("buy", "sell"):
        return _reject(["BAD_ORDER_SIDE"], "the order side must be buy or sell")
    if amount is None or amount <= 0:
        return _reject(["BAD_AMOUNT"], "the order amount must be greater than zero")
    clean_symbol = (symbol or "").strip()
    if not clean_symbol:
        return _reject(["UNKNOWN_SYMBOL"], "no asset symbol was named; nothing moved")

    try:
        catalogue = await list_strategies()
        strategies = (
            catalogue.get("strategies", []) if isinstance(catalogue, dict) else []
        )
        sleeve = _sleeve_from_catalogue(
            strategies if isinstance(strategies, list) else []
        )
    except Exception as exc:  # noqa: BLE001 - a lookup failure is a business result
        logger.warning("order prepare: catalogue unreadable: %s", exc)
        return _reject(
            ["GLIDER_UNREACHABLE"],
            "the stock sleeve catalogue could not be read; nothing moved",
        )
    if sleeve is None:
        return _reject(
            ["SLEEVE_MISSING"],
            "Rail Stock Sleeve is not configured yet; escalate, do not invent tickers",
        )
    strategy_id = str(sleeve.get("id") or "")
    glider_strategy_id = str(
        sleeve.get("glider_strategy_id") or sleeve.get("gliderStrategyId") or ""
    )
    if not strategy_id or not glider_strategy_id:
        return _reject(
            ["SLEEVE_UNBOUND"], "the sleeve has no live provider binding; nothing moved"
        )

    try:
        found = await search_assets()
        rows = found.get("assets", []) if isinstance(found, dict) else []
        asset = _pick_asset(rows if isinstance(rows, list) else [], clean_symbol)
    except Exception as exc:  # noqa: BLE001 - a lookup failure is a business result
        logger.warning("order prepare: asset search unreadable: %s", exc)
        return _reject(
            ["GLIDER_UNREACHABLE"],
            "the asset catalogue could not be read; nothing moved",
        )
    if asset is None:
        return _reject(
            ["UNKNOWN_SYMBOL"],
            f"{clean_symbol} is not a known sleeve asset; nothing moved",
        )
    asset_id = str(asset.get("asset_id") or asset.get("assetId") or "")
    asset_symbol = str(asset.get("symbol") or asset.get("Symbol") or clean_symbol)

    caip19 = str(asset.get("caipAssetId") or asset.get("caip19") or "")
    if caip19:
        try:
            from miriam_agent.tools.glider_sleeve import parse_caip19

            parse_caip19(caip19)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            return _reject(
                ["BAD_ASSET_ID"],
                f"asset id failed strict CAIP-19 parse ({exc}); nothing moved",
            )
    if not asset_id:
        return _reject(
            ["UNKNOWN_SYMBOL"],
            f"{clean_symbol} has no asset id in the catalogue; nothing moved",
        )

    if side == "buy" and amount > ledger.balance(ORDER_SLEEVE):
        return _reject(
            ["OVER_BALANCE"],
            f"the stash holds {ledger.balance(ORDER_SLEEVE)}, "
            f"which is less than {amount}",
        )

    try:
        completed = await _obtain_and_replay(
            order_call,
            {
                "strategy_id": strategy_id,
                "side": side,
                "asset_id": asset_id,
                "amount_usd": float(amount),
            },
        )
    except Exception as exc:  # noqa: BLE001 - a provider failure is a business result
        logger.warning("order prepare: stage failed: %s", exc)
        return _reject(
            ["GLIDER_NOT_LIVE"],
            f"the order did not start ({exc}); nothing moved",
        )

    status = str(completed.get("status") or "")
    if status == "AWAITING_CONFIRMATION":
        return _reject(
            ["STILL_STAGED"], "the provider stayed staged after replay; nothing moved"
        )
    if completed.get("simulated") is True or completed.get("live") is False:
        return _reject(
            ["GLIDER_NOT_LIVE"],
            "Glider is not live (simulation mode); no execution was issued",
        )
    funding = completed.get("funding", {})
    if not isinstance(funding, dict):
        funding = {}
    if str(funding.get("status") or "").upper() == "FAILED":
        reason = (
            funding.get("failure_reason") or funding.get("FailureReason") or "unknown"
        )
        return _reject(
            ["FUNDING_FAILED"],
            f"the order placed but funding failed ({reason}); sleeve not debited",
        )
    execution_id = str(
        completed.get("execution_id")
        or completed.get("executionId")
        or completed.get("order_id")
        or completed.get("orderId")
        or ""
    )
    operation_id = str(
        completed.get("operation_id")
        or completed.get("operationId")
        or completed.get("rebalance_op_id")
        or ""
    )
    if not execution_id:
        return _reject(
            ["NO_EXECUTION"],
            "the provider returned no execution id; nothing is claimed",
        )

    order_id = _id("ord")
    binding = PendingOrder(
        id=order_id,
        confirm_id=decision_id,
        strategy_id=strategy_id,
        glider_strategy_id=glider_strategy_id,
        side=side,
        asset_id=asset_id,
        symbol=asset_symbol,
        amount_usd=str(amount),
        status="completed",
        created_at=timestamp,
    )
    ledger.pending_order[order_id] = binding
    await store.save(ledger)

    card: dict[str, Any] = {
        "kind": "order_confirm",
        "title": "ORDER",
        "subtitle": "Glider \u00b7 Rail Stock Sleeve",
        "primary": f"{side.upper()} {asset_symbol}",
        "amount": f"{amount} USD",
        "source": "stash" if side == "buy" else "sleeve",
        "cta": "Track",
        "note": "Converges on rebalance \u2014 no price guarantee",
        "execution_id": execution_id,
        "operation_id": operation_id,
        "strategy_id": strategy_id,
        "expires_in_sec": ORDER_TTL_MINUTES * 60,
    }
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="executed",
        action="order_prepare",
        currency=ledger.currency,
        amount=amount,
        counterparty=asset_symbol,
        sleeve=ORDER_SLEEVE,
        decision_id=decision_id,
        idempotency_key=f"order-prepare:{decision_id}:{side}:{symbol}:{amount}",
        rail_reference=execution_id,
        confirm_id=decision_id,
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        detail=f"{side} {amount} USD of {asset_symbol} -> execution {execution_id}; "
        "converges on rebalance, no price guarantee",
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    return receipt, card, ledger


async def settle_order(
    *,
    store: LedgerStore,
    ledger: Ledger,
    user_id: str,
    order_id: str,
    decision_id: str = "",
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any], Ledger]:
    """Re-affirm a staged order binding. Server-signed: no wallet signature."""
    timestamp = at if at is not None else _utcnow()
    before = sleeves_snapshot(ledger.sleeves)

    def _reject(
        reasons: list[str], detail: str
    ) -> tuple[Receipt, dict[str, Any], Ledger]:
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action="order_settle",
            currency=ledger.currency,
            decision_id=decision_id,
            idempotency_key=f"order-settle:{order_id}",
            reasons=reasons,
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            detail=detail,
        )
        ledger.remember_receipt(receipt)
        return receipt, {"ok": False, "reasons": reasons, "detail": detail}, ledger

    binding = ledger.pending_order.get(order_id)
    if binding is None:
        return _reject(["NO_SUCH_ORDER"], "that order does not match anything open")
    # Idempotent replay: the same settle twice returns the first receipt.
    _settle_key = f"order-settle:{order_id}"
    _prior_settle = ledger.receipt_for(_settle_key)
    if _prior_settle is not None:
        return _prior_settle, {"ok": True, "idempotent_replay": True}, ledger
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="executed",
        action="order_settle",
        currency=ledger.currency,
        amount=money(binding.amount_usd),
        counterparty=binding.symbol,
        sleeve=ORDER_SLEEVE,
        decision_id=decision_id,
        idempotency_key=f"order-settle:{order_id}",
        rail_reference=binding.id,
        confirm_id=binding.confirm_id,
        idempotent_replay=True,
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        detail=f"replay of order {binding.id}; nothing moved twice",
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    return receipt, {"ok": True, "idempotent_replay": True}, ledger


async def prepare_set_allocation(
    *,
    store: LedgerStore,
    ledger: Ledger,
    user_id: str,
    token: str,
    strategy_id: str,
    legs: list[dict[str, Any]],
    decision_id: str = "",
    allocation_call: Callable[..., Awaitable[dict[str, Any]]],
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any] | None, Ledger]:
    """Run a set-allocation shift after the tap and return the execution card."""
    timestamp = at if at is not None else _utcnow()
    before = sleeves_snapshot(ledger.sleeves)
    # Idempotency first: a replayed tap replays the receipt, never the rail.
    _alloc_key = f"set-allocation-prepare:{decision_id}"
    _prior_alloc = ledger.receipt_for(_alloc_key) if decision_id else None
    if _prior_alloc is not None:
        return _prior_alloc, None, ledger

    def _reject(reasons: list[str], detail: str) -> tuple[Receipt, None, Ledger]:
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action="set_allocation_prepare",
            currency=ledger.currency,
            counterparty="Rail Stock Sleeve",
            sleeve=ORDER_SLEEVE,
            decision_id=decision_id,
            idempotency_key=f"set-allocation-prepare:{decision_id}",
            reasons=reasons,
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            detail=detail,
        )
        ledger.remember_receipt(receipt)
        return receipt, None, ledger

    if not strategy_id.strip():
        return _reject(["BAD_STRATEGY"], "no strategy id was supplied; nothing moved")
    clean_legs: list[dict[str, Any]] = []
    total = 0.0
    for leg in legs or []:
        if not isinstance(leg, dict):
            continue
        asset_id = str(leg.get("asset_id") or "")
        weight = leg.get("weight")
        try:
            w = float(weight)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if not asset_id or w <= 0:
            continue
        clean_legs.append({"asset_id": asset_id, "weight": w})
        total += w
    if not clean_legs:
        return _reject(["BAD_LEGS"], "no allocation legs were supplied; nothing moved")
    # Legs must describe a real split: weights near 1.0 (fractions) or 100.0
    # (percents), every asset id non-empty and unique. Malformed splits are
    # rejected here with a clear reason instead of failing opaquely at Go.
    if len({leg["asset_id"] for leg in clean_legs}) != len(clean_legs):
        return _reject(["BAD_LEGS"], "allocation legs repeat an asset; nothing moved")
    if not (0.99 <= total <= 1.01 or 99.0 <= total <= 101.0):
        return _reject(
            ["BAD_LEGS"],
            f"allocation weights sum to {total:g}, not 1.0 or 100; nothing moved",
        )

    try:
        completed = await _obtain_and_replay(
            allocation_call,
            {"strategy_id": strategy_id, "legs": clean_legs},
        )
    except Exception as exc:  # noqa: BLE001 - a provider failure is a business result
        logger.warning("set-allocation prepare: stage failed: %s", exc)
        return _reject(
            ["GLIDER_NOT_LIVE"],
            f"the allocation shift did not start ({exc}); nothing moved",
        )
    status = str(completed.get("status") or "")
    if status == "AWAITING_CONFIRMATION":
        return _reject(
            ["STILL_STAGED"], "the provider stayed staged after replay; nothing moved"
        )
    execution_id = str(
        completed.get("execution_id")
        or completed.get("executionId")
        or completed.get("order_id")
        or ""
    )
    operation_id = str(
        completed.get("operation_id")
        or completed.get("operationId")
        or completed.get("rebalance_op_id")
        or ""
    )
    if not execution_id:
        return _reject(
            ["NO_EXECUTION"],
            "the provider returned no execution id; nothing is claimed",
        )
    card: dict[str, Any] = {
        "kind": "order_confirm",
        "title": "SET ALLOCATION",
        "subtitle": "Glider \u00b7 Rail Stock Sleeve",
        "primary": f"{len(clean_legs)} legs",
        "amount": "",
        "source": "sleeve",
        "cta": "Track",
        "note": "Converges on rebalance \u2014 no price guarantee",
        "execution_id": execution_id,
        "operation_id": operation_id,
        "strategy_id": strategy_id,
        "expires_in_sec": ORDER_TTL_MINUTES * 60,
    }
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="executed",
        action="set_allocation_prepare",
        currency=ledger.currency,
        counterparty="Rail Stock Sleeve",
        sleeve=ORDER_SLEEVE,
        decision_id=decision_id,
        idempotency_key=f"set-allocation-prepare:{decision_id}",
        rail_reference=execution_id,
        confirm_id=decision_id,
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        detail=f"set allocation ({len(clean_legs)} legs) -> execution {execution_id}",
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    return receipt, card, ledger


async def settle_set_allocation(
    *,
    store: LedgerStore,
    ledger: Ledger,
    user_id: str,
    allocation_id: str,
    decision_id: str = "",
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any], Ledger]:
    """Re-affirm a set-allocation binding. Server-signed: no wallet signature."""
    timestamp = at if at is not None else _utcnow()
    before = sleeves_snapshot(ledger.sleeves)
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="rejected",
        action="set_allocation_settle",
        currency=ledger.currency,
        decision_id=decision_id,
        idempotency_key=f"set-allocation-settle:{allocation_id}",
        reasons=["NOT_STAGED"],
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        detail="set-allocation completes inside prepare; nothing left to settle",
    )
    ledger.remember_receipt(receipt)
    return receipt, {"ok": False, "reasons": ["NOT_STAGED"]}, ledger

"""Funding helpers for the Glider stock-sleeve invest leg.

Split verbatim out of :mod:`miriam_agent.hands.invest` (blob-ratchet): the
staged-mutation replay, the funding-response extraction, the needs-review card,
the top-up path, the catalogue sleeve picker, and the shared ``INVEST_SLEEVE``
constant + ``_id`` generator they use. ``invest.py`` re-imports every name, so
call sites and import paths are unchanged.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from miriam_agent.hands.audit import Receipt, sleeves_snapshot
from miriam_agent.hands.ledger import Ledger, LedgerStore, Movement

logger = logging.getLogger(__name__)

# The sleeve all invest movements draw on. Never the spend pot.
INVEST_SLEEVE = "savings"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def _obtain_and_replay(
    call: Callable[..., Awaitable[dict[str, Any]]],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Run a Go staged mutation: first call stages, replay carries the token.

    The replay sends the byte-identical payload plus the issued token, so the
    backend's payload-hash binding rejects anything mutated in between.
    """
    if "confirmation_token" in payload:
        raise RuntimeError("staged payload must not pre-carry a confirmation token")
    first = await call(dict(payload))
    if not isinstance(first, dict):
        raise RuntimeError(f"go host returned no staged response: {first!r}")
    if first.get("status") == "AWAITING_CONFIRMATION":
        token = (
            (first.get("confirmation") or {})
            if isinstance(first.get("confirmation"), dict)
            else {}
        ).get("token")
        if not token:
            # Some handlers nest it one level deeper.
            conf = (
                first.get("result", {}).get("confirmation", {})
                if isinstance(first.get("result"), dict)
                else {}
            )
            token = conf.get("token") if isinstance(conf, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("go host staged a confirmation without a token")
        second = await call({**payload, "confirmation_token": token})
        if not isinstance(second, dict):
            raise RuntimeError(f"go host returned no confirmed response: {second!r}")
        return second
    return first


def _extract_funding(enrolled: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (enrollment, funding) for a Go response, fail-closed on conflict.

    Funding may ride top-level or nested inside ``enrollment``, depending on
    the Go handler shape. Either location counts: a FAILED anywhere blocks
    the sleeve debit, so a FAILED in either spot wins over a SUBMITTED in
    the other. Otherwise the non-empty side wins.
    """
    enrollment = enrolled.get("enrollment") if isinstance(enrolled, dict) else None
    if not isinstance(enrollment, dict):
        enrollment = {}
    top = enrolled.get("funding") if isinstance(enrolled, dict) else None
    nested = enrollment.get("funding")
    if not isinstance(top, dict):
        top = {}
    if not isinstance(nested, dict):
        nested = {}
    for candidate in (top, nested):
        if str(candidate.get("status") or "").upper() == "FAILED":
            return enrollment, candidate
    if top:
        return enrollment, top
    return enrollment, nested


def _needs_review_card(
    *, amount: Decimal, currency: str, strategy_id: str, enrollment_id: str
) -> dict[str, Any]:
    return {
        "kind": "needs_reconciliation",
        "title": "NEEDS REVIEW",
        "subtitle": "Glider · Rail Stock Sleeve",
        "primary": "Funded remotely; local ledger needs reconciliation",
        "amount": f"{amount} {currency}",
        "source": "stash",
        "strategy_id": strategy_id,
        "enrollment_id": enrollment_id,
    }


async def _fund_existing(
    *,
    store: LedgerStore,
    ledger: Ledger,
    amount: Decimal,
    strategy_id: str,
    decision_id: str,
    fund_call: Callable[..., Awaitable[dict[str, Any]]] | None,
    prepared: dict[str, Any],
    before: dict[str, str],
    timestamp: datetime,
    reject: Callable[[list[str], str], tuple[Receipt, None, Ledger]],
) -> tuple[Receipt, dict[str, Any] | None, Ledger]:
    """Add money to a portfolio that is already enrolled. No new signature.

    Invariants: ``rejected`` always means nothing moved anywhere (local or
    remote). The local balance is checked *before* touching the provider, and
    a local receipt is reserved under the top-up idempotency key so a retap
    after a crash replays instead of double-funding. A ``parked`` key is
    terminal too: money moved remotely and needs a human, never a retry.
    """
    topup_key = f"invest-topup:{decision_id}:{amount}"
    prior = ledger.receipt_for(topup_key)
    if prior is not None and prior.status == "executed":
        replay = prior.model_copy(update={"idempotent_replay": True})
        card = {
            "kind": "funded",
            "title": "FUNDED",
            "subtitle": "Glider · Rail Stock Sleeve",
            "primary": "Already added to the existing sleeve",
            "amount": f"{amount} {ledger.currency}",
            "source": "stash",
            "strategy_id": strategy_id,
            "enrollment_id": prior.rail_reference,
            "idempotent_replay": True,
        }
        return replay, card, ledger
    if prior is not None and prior.status in ("parked", "noop"):
        # Terminal without a second provider call: remote may already hold
        # the money, so a retap must replay the review card, not re-fund.
        replay = prior.model_copy(update={"idempotent_replay": True})
        card = _needs_review_card(
            amount=amount,
            currency=ledger.currency,
            strategy_id=strategy_id,
            enrollment_id=prior.rail_reference,
        )
        card["idempotent_replay"] = True
        return replay, card, ledger
    if fund_call is None:
        return reject(
            ["FUND_UNAVAILABLE"],
            "this strategy is already enrolled and this path cannot add money",
        )
    if amount > ledger.balance(INVEST_SLEEVE):
        # Checked before the provider call so a refusal never moves money.
        return reject(
            ["OVER_BALANCE"],
            f"the stash holds {ledger.balance(INVEST_SLEEVE)}, "
            f"which is less than {amount}; nothing moved",
        )
    try:
        funded = await _obtain_and_replay(
            fund_call,
            {
                "strategy_id": strategy_id,
                "amount_usd": float(amount),
                "source": "stash",
                "idempotency_key": topup_key,
            },
        )
    except Exception as exc:  # noqa: BLE001 - a provider failure is a business result
        logger.warning("invest prepare: top-up failed: %s", exc)
        return reject(
            ["FUNDING_FAILED"],
            f"the portfolio is enrolled but the top-up did not start ({exc})",
        )
    if str(funded.get("status") or "") == "AWAITING_CONFIRMATION":
        return reject(
            ["STILL_STAGED"], "the top-up stayed staged after replay; nothing moved"
        )
    _enrollment_pre, funding = _extract_funding(funded)
    if str(funding.get("status") or "").upper() == "FAILED":
        reason = (
            funding.get("failure_reason") or funding.get("FailureReason") or "unknown"
        )
        return reject(
            ["FUNDING_FAILED"],
            f"the portfolio is enrolled but funding failed ({reason}); nothing moved",
        )
    enrollment_id = str(
        prepared.get("enrollment_id")
        or _enrollment_pre.get("id")
        or _enrollment_pre.get("enrollment_id")
        or ""
    )
    try:
        ledger.debit(INVEST_SLEEVE, amount)
    except (
        Exception
    ) as exc:  # noqa: BLE001 - remote moved, local refused: diverge loudly
        logger.error(
            "invest top-up diverged: Go funded %s but stash debit refused (%s)",
            topup_key,
            exc,
        )
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="parked",
            action="invest_settle",
            currency=ledger.currency,
            amount=amount,
            counterparty="Rail Stock Sleeve",
            sleeve=INVEST_SLEEVE,
            decision_id=decision_id,
            idempotency_key=topup_key,
            reasons=["LEDGER_DIVERGED"],
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            rail_reference=enrollment_id,
            strategy_id=strategy_id,
            detail=(
                f"Go funded ${amount} but the local stash debit refused ({exc}); "
                "money moved remotely and needs reconciliation; not re-tappable "
                "under this key"
            ),
        )
        ledger.remember_receipt(receipt)
        await store.save(ledger)
        card = _needs_review_card(
            amount=amount,
            currency=ledger.currency,
            strategy_id=strategy_id,
            enrollment_id=enrollment_id,
        )
        return receipt, card, ledger
    ledger.record_movement(
        Movement(
            kind="outflow",
            amount=amount,
            sleeve=INVEST_SLEEVE,
            counterparty="Rail Stock Sleeve",
            category="invest",
            ref=enrollment_id or decision_id,
            at=timestamp,
        )
    )
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="executed",
        action="invest_settle",
        currency=ledger.currency,
        amount=amount,
        counterparty="Rail Stock Sleeve",
        sleeve=INVEST_SLEEVE,
        decision_id=decision_id,
        idempotency_key=topup_key,
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        rail_reference=enrollment_id,
        strategy_id=strategy_id,
        detail=f"${amount} added to the existing Rail Stock Sleeve",
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    card = {
        "kind": "funded",
        "title": "FUNDED",
        "subtitle": "Glider \u00b7 Rail Stock Sleeve",
        "primary": "Added to the existing sleeve",
        "amount": f"{amount} {ledger.currency}",
        "source": "stash",
        "strategy_id": strategy_id,
        "enrollment_id": enrollment_id,
    }
    return receipt, card, ledger


def _sleeve_from_catalogue(strategies: list[dict[str, Any]]) -> dict[str, Any] | None:
    for s in strategies:
        if str(s.get("name") or "").strip().lower() == "rail stock sleeve":
            return s
    return None

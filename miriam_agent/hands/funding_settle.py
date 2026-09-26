"""Layer 1 - HANDS. Funding settlement: onramp prepare, OTP submit, offramp stage.

Deterministic code only. No LLM, no JEV.

Companion to :mod:`miriam_agent.hands.funding` (parse + thin Go writes).
The orchestrator calls these after a confirm_id tap; they return
``(receipt, card, ledger)`` and the orchestrator narrates. Fail-closed
everywhere: no token or no quote is a rejected receipt, a wrong OTP is a
retry with the challenge left open, and the offramp stages an envelope the
Rail app POSTs with the passcode -- Hands never POSTs funds-OUT.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from miriam_agent.hands.audit import Receipt, sleeves_snapshot
from miriam_agent.hands.funding import (
    FUND_TTL_MINUTES,
    create_onramp,
    initiate_paj_session,
    stage_offramp_envelope,
    verify_paj_otp,
)
from miriam_agent.hands.ledger import Challenge, Ledger, LedgerStore, money

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def prepare_onramp(
    *,
    store: LedgerStore,
    ledger: Ledger,
    user_id: str,
    token: str | None,
    amount: Decimal,
    symbol: str,
    provider: str = "paj",
    decision_id: str = "",
    confirm_id: str = "",
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any] | None, Ledger]:
    """Quote, then Paj initiate, after the confirm tap.

    A ``paj_otp`` card means the code was sent and the user's next message
    settles it via :func:`submit_paj_otp`; an ``onramp_order`` card is the
    final bank-transfer order. Fail-closed: no token or no quote is a
    rejected receipt, never silence.
    """
    timestamp = at or _utcnow()
    try:
        quoted_amount: Decimal = money(amount)
    except Exception:
        quoted_amount = Decimal("0")
    if quoted_amount <= 0:
        # No usable amount: fail closed before any quote or rail call.
        failed = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action="onramp_prepare",
            currency=ledger.currency,
            counterparty=symbol,
            sleeve="spendable",
            decision_id=decision_id,
            idempotency_key=f"onramp:{confirm_id or decision_id}",
            reasons=["BAD_AMOUNT"],
            sleeves_before=sleeves_snapshot(ledger.sleeves),
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            detail="no onramp amount was named; nothing moved",
        )
        ledger.remember_receipt(failed)
        return failed, None, ledger
    amount = quoted_amount
    before = sleeves_snapshot(ledger.sleeves)

    def _reject(reasons: list[str], detail: str) -> tuple[Receipt, None, Ledger]:
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action="onramp_prepare",
            currency=ledger.currency,
            amount=amount,
            counterparty=symbol,
            sleeve="spendable",
            decision_id=decision_id,
            idempotency_key=f"onramp:{confirm_id or decision_id}",
            reasons=reasons,
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            detail=detail,
        )
        ledger.remember_receipt(receipt)
        return receipt, None, ledger

    # Idempotency first: a double-tap replays the receipt instead of
    # re-quoting and re-initiating Paj.
    _prior = ledger.receipt_for(f"onramp:{confirm_id or decision_id}")
    if _prior is not None:
        return _prior, None, ledger
    _prior_init = ledger.receipt_for(f"paj-initiate:{confirm_id or decision_id}")
    if _prior_init is not None and _prior_init.status in ("queued", "executed"):
        return _prior_init, None, ledger

    if not token:
        receipt, _, ledger = _reject(
            ["NO_GO_TOKEN"], "no Go host token on this path; nothing moved"
        )
        return receipt, None, ledger

    try:
        from miriam_agent.integrations.go_client import get_go_client

        quote = await get_go_client().get_crypto_quote(token, "onramp", float(amount))
    except Exception as exc:  # noqa: BLE001 - a quote failure is a business result
        logger.warning("funding quote failed: %s", exc)
        quote = {"_tool_error": str(exc)[:200]}
    if not isinstance(quote, dict) or quote.get("_tool_error"):
        receipt, _, ledger = _reject(
            ["RATE_UNAVAILABLE"], "no live quote right now; nothing moved"
        )
        return receipt, None, ledger

    # RampHub buy orders return the NGN virtual account the user pays into.
    # There is no OTP and no wallet address in that instruction: USDC is
    # credited to the Circle wallet only after the bank transfer lands.
    if (provider or "").lower() == "ramp":
        created = await create_onramp(
            token,
            kind="ramp",
            amount_ngn=amount,
            currency="NGN",
            verified=True,
            idempotency_key=f"onramp:{confirm_id or decision_id}",
            confirm_id=confirm_id or None,
        )
        return _finish_onramp_order(
            ledger,
            amount,
            symbol,
            decision_id,
            confirm_id or decision_id,
            quote,
            created,
            at=timestamp,
        )

    initiated = await initiate_paj_session(
        token,
        idempotency_key=f"paj-initiate:{confirm_id or decision_id}",
        confirm_id=confirm_id or None,
    )
    raw = initiated.get("raw") if isinstance(initiated, dict) else None
    status = (
        str((raw or {}).get("status") or "").lower() if isinstance(raw, dict) else ""
    )
    if initiated.get("ok") and status == "already_verified":
        created = await create_onramp(
            token,
            kind=provider,
            amount_ngn=amount,
            currency="NGN",
            verified=True,
            idempotency_key=f"onramp:{confirm_id or decision_id}",
            confirm_id=confirm_id or None,
        )
        return _finish_onramp_order(
            ledger,
            amount,
            symbol,
            decision_id,
            confirm_id or decision_id,
            quote,
            created,
            at=timestamp,
        )

    # OTP required: the OTP turn is challenge state (30-min TTL). No ledger
    # movement happens until the code verifies. The id is unique per tap
    # (never truncated): two onramps off the same decision must never share
    # a challenge id and overwrite each other's OTP state.
    _otp_suffix = (confirm_id or decision_id or _id("otp")).strip() or _id("otp")
    otp_challenge = Challenge(
        id=f"confirm_otp_{_otp_suffix}_{uuid.uuid4().hex[:8]}",
        user_id=ledger.user_id,
        action="paj_otp",
        amount=money(amount),
        counterparty=symbol,
        destination=symbol,
        sleeve="spendable",
        decision_id=decision_id,
        created_at=timestamp,
        expires_at=timestamp + timedelta(minutes=FUND_TTL_MINUTES),
        meta={
            "onramp_confirm_id": confirm_id or decision_id,
            "provider": provider,
            "rate": str((quote or {}).get("rate") or ""),
            "symbol": symbol,
        },
    )
    ledger.challenges[otp_challenge.id] = otp_challenge
    masked = str((raw or {}).get("recipient") or "") if isinstance(raw, dict) else ""
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="queued",
        action="onramp_initiate",
        currency=ledger.currency,
        amount=amount,
        counterparty=symbol,
        sleeve="spendable",
        decision_id=decision_id,
        idempotency_key=f"paj-initiate:{confirm_id or decision_id}",
        confirm_id=confirm_id,
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        detail=(
            f"quote {quote.get('rate')} locked for {amount:g} NGN; "
            f"OTP sent to {masked or 'your recipient'}. Reply with the code."
        ),
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    card: dict[str, Any] = {
        "kind": "paj_otp",
        "title": "VERIFY RECIPIENT",
        "amount": f"{amount:g} NGN",
        "rate": str(quote.get("rate") or ""),
        "recipient": masked,
        "otp_challenge_id": otp_challenge.id,
        "note": "Reply with the Paj code. Nothing moves until it verifies.",
    }
    return receipt, card, ledger


def _provider_fiat_amount(raw: dict[str, Any], amount: Decimal) -> str:
    """The naira figure the user must transfer, from the provider when present."""
    fiat = raw.get("fiatAmount")
    try:
        if fiat is not None and str(fiat).strip() != "":
            parsed = Decimal(str(fiat))
            if parsed > 0:
                return _trim_decimal(parsed)
    except Exception:  # noqa: BLE001 - a bad provider figure falls back to the ask
        pass
    return _trim_decimal(amount)


def _trim_decimal(value: Decimal) -> str:
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _finish_onramp_order(
    ledger: Ledger,
    amount: Decimal,
    symbol: str,
    decision_id: str,
    confirm_id: str,
    quote: dict[str, Any],
    created: dict[str, Any],
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any] | None, Ledger]:
    """Build the onramp order receipt + card from a create_onramp result."""
    timestamp = at or _utcnow()
    before = sleeves_snapshot(ledger.sleeves)
    if not created.get("ok"):
        reasons = list(created.get("reasons") or ["ONRAMP_FAILED"])
        # ONRAMP_FAILED can mean the POST landed and the reply was lost.
        # Keep onramp:{confirm_id} free so the next tap replays that key.
        replayable = "ONRAMP_FAILED" in reasons
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action="onramp_prepare",
            currency=ledger.currency,
            amount=amount,
            counterparty=symbol,
            sleeve="spendable",
            decision_id=decision_id,
            idempotency_key=(
                f"onramp-incomplete:{confirm_id}"
                if replayable
                else f"onramp:{confirm_id}"
            ),
            reasons=reasons,
            sleeves_before=before,
            sleeves_after=before,
            detail=str(
                created.get("detail") or "the onramp did not start; nothing moved"
            ),
        )
        ledger.remember_receipt(receipt)
        return receipt, None, ledger
    raw = created.get("raw") if isinstance(created.get("raw"), dict) else {}
    account_number = str(raw.get("accountNumber") or "").strip()
    bank = str(raw.get("bank") or "").strip()
    # A RampHub buy is only payable once the virtual account is known. The
    # create call already used onramp:{confirm_id}; a retry must POST that
    # same key so Go replays the order instead of opening another one.
    # This rejection is stored under a different key so that replay can run.
    if not account_number or not bank:
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action="onramp_prepare",
            currency=ledger.currency,
            amount=amount,
            counterparty=symbol,
            sleeve="spendable",
            decision_id=decision_id,
            idempotency_key=f"onramp-incomplete:{confirm_id}",
            reasons=["PAYIN_ACCOUNT_MISSING"],
            sleeves_before=before,
            sleeves_after=before,
            detail=(
                "the provider did not return the bank account to pay; "
                f"replay idempotency key onramp:{confirm_id}"
            ),
        )
        ledger.remember_receipt(receipt)
        return receipt, None, ledger
    pay_ngn = _provider_fiat_amount(raw, amount)
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="executed",
        action="onramp_prepare",
        currency=ledger.currency,
        amount=amount,
        counterparty=symbol,
        sleeve="spendable",
        decision_id=decision_id,
        idempotency_key=f"onramp:{confirm_id}",
        rail_reference=str(raw.get("orderId") or raw.get("transactionId") or ""),
        confirm_id=confirm_id,
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        detail=(
            f"Pay exactly {pay_ngn} NGN into "
            f"{raw.get('accountName') or ''} {account_number} "
            f"at {bank}. That bank account is where the naira "
            f"goes. Rate {raw.get('rate') or quote.get('rate')}."
        ),
    )
    ledger.remember_receipt(receipt)
    card = {
        "kind": "onramp_order",
        "title": "ONRAMP ORDER",
        "amount": f"{pay_ngn} NGN",
        "rate": str(raw.get("rate") or quote.get("rate") or ""),
        "account_number": account_number,
        "account_name": str(raw.get("accountName") or ""),
        "bank": bank,
        "token_amount": str(
            raw.get("tokenAmount")
            or quote.get("estimatedOutput")
            or quote.get("tokenAmount")
            or ""
        ),
        "order_id": str(raw.get("orderId") or raw.get("transactionId") or ""),
        "note": "Transfer the exact amount. Your deposit credits automatically.",
    }
    return receipt, card, ledger


async def submit_paj_otp(
    *,
    store: LedgerStore,
    ledger: Ledger,
    otp_challenge: Any,
    otp: str,
    token: str | None,
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any] | None, Ledger]:
    """Verify the OTP from the user's next message, then create the order.

    Wrong OTP is a retry receipt: the challenge stays open and no order is
    created. Nothing is ever auto-bought.
    """
    timestamp = at or _utcnow()
    before = sleeves_snapshot(ledger.sleeves)

    def _reject(reasons: list[str], detail: str) -> tuple[Receipt, None, Ledger]:
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action="onramp_verify",
            currency=ledger.currency,
            amount=otp_challenge.amount,
            counterparty=otp_challenge.counterparty,
            sleeve=otp_challenge.sleeve,
            decision_id=otp_challenge.decision_id,
            idempotency_key=f"paj-verify:{otp_challenge.id}",
            reasons=reasons,
            sleeves_before=before,
            sleeves_after=before,
            detail=detail,
        )
        ledger.remember_receipt(receipt)
        return receipt, None, ledger

    if not token:
        receipt, _, ledger = _reject(
            ["NO_GO_TOKEN"], "no Go host token on this path; nothing moved"
        )
        return receipt, None, ledger
    meta = getattr(otp_challenge, "meta", {}) or {}
    verified = await verify_paj_otp(
        token,
        otp,
        idempotency_key=f"paj-verify:{otp_challenge.id}:{otp}",
        confirm_id=meta.get("onramp_confirm_id") or otp_challenge.id,
    )
    if not verified.get("ok"):
        # Retry, never an auto-buy: the challenge stays open.
        receipt, _, ledger = _reject(
            list(verified.get("reasons") or ["PAJ_VERIFY_FAILED"]),
            "that code did not verify; reply with the correct code. "
            "No order was created.",
        )
        await store.save(ledger)
        return receipt, None, ledger
    otp_challenge.status = "consumed"
    ledger.challenges[otp_challenge.id] = otp_challenge
    onramp_id = str(meta.get("onramp_confirm_id") or otp_challenge.id)
    provider = str(meta.get("provider") or "paj")
    created = await create_onramp(
        token,
        kind=provider,
        amount_ngn=otp_challenge.amount,
        currency="NGN",
        verified=True,
        idempotency_key=f"onramp:{onramp_id}",
        confirm_id=onramp_id,
    )
    quote = {"rate": meta.get("rate") or ""}
    receipt, card, ledger = _finish_onramp_order(
        ledger,
        money(otp_challenge.amount),
        otp_challenge.counterparty,
        otp_challenge.decision_id,
        onramp_id,
        quote,
        created,
        at=timestamp,
    )
    await store.save(ledger)
    return receipt, card, ledger


async def stage_offramp(
    *,
    store: LedgerStore,
    ledger: Ledger,
    amount: Decimal,
    counterparty: str = "NGN",
    decision_id: str = "",
    confirm_id: str = "",
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any], Ledger]:
    """Stage the passcode-gated offramp envelope for the Rail app.

    App-only: Hands never POSTs this. Returns a rejected receipt (nothing
    moved) plus the envelope card.
    """
    timestamp = at or _utcnow()
    amount = money(amount)
    before = sleeves_snapshot(ledger.sleeves)
    staged = stage_offramp_envelope(amount_ngn=amount, currency="NGN")
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="rejected",
        action="offramp_stage",
        currency=ledger.currency,
        amount=amount,
        counterparty=counterparty,
        sleeve="spendable",
        decision_id=decision_id,
        idempotency_key=f"offramp:{confirm_id or decision_id}",
        reasons=["APP_ONLY"],
        sleeves_before=before,
        sleeves_after=before,
        detail=(
            "cash-out needs your app passcode; staged for the Rail app, nothing moved"
        ),
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    return receipt, {"kind": "offramp_envelope", **staged}, ledger


__all__ = ["prepare_onramp", "stage_offramp", "submit_paj_otp"]

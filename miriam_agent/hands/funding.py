"""Layer 1 - HANDS. NGN <-> crypto funding leg (RampHub / Paj / ChainRails).

Deterministic code only. No LLM, no JEV.

Buy (funds IN, agent-safe): "buy <NGN> [of] <symbol>" parses by regex to an
``onramp`` action. Settlement lives in :mod:`miriam_agent.hands.funding_settle`:
the confirm tap stages the order proposal (amount, side, provider, rate from
the Go quote), then the Paj flow engages: initiate -> await OTP typed as the
user's next message -> verify -> create onramp order. No ledger movement
happens until the OTP verifies.

Sell/offramp (funds OUT, passcode-gated): app-only. Hands never POSTs it.
It returns a staged envelope ({path, method, payload, needs:[app_confirm,
passcode]} + rail://authorize) for the Rail app, mirroring propose_vault_plan.

ChainRails: guidance only, never a POST from here.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from miriam_agent.hands.state import ProposedAction

logger = logging.getLogger(__name__)

# OTP challenge TTL mirrors the ledger confirm challenges (30 min).
FUND_TTL_MINUTES = 30

_BUY_RE = re.compile(
    r"\bbuy\s+(\d[\d,]*(?:\.\d+)?)\s*(k|m|thousand|mille)?\s*(?:naira|ngn|₦)?\s*(?:of\s+)?([a-z]{2,10})?",
    re.IGNORECASE,
)
_SELL_RE = re.compile(
    r"\bsell\s+(\d[\d,]*(?:\.\d+)?)\s*(k|m|thousand|mille)?\s*(?:naira|ngn|₦|usdc|usdt)?",
    re.IGNORECASE,
)
_OTP_RE = re.compile(r"^\s*(\d{4,8})\s*$")

_MULTIPLIERS = {"k": 1000, "thousand": 1000, "mille": 1000, "m": 1_000_000}

_SYMBOL_ALIASES = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "usdt": "USDT",
    "tether": "USDT",
    "usdc": "USDC",
    "dollar": "USDC",
    "dollars": "USDC",
    "crypto": "USDC",
    "eth": "ETH",
    "ethereum": "ETH",
    "sol": "SOL",
    "solana": "SOL",
    "naira": "USDC",
    "ngn": "USDC",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def classify_symbol(raw: str | None) -> str:
    """Map a free word to a funding asset. Defaults to USDC (the credit asset)."""
    key = (raw or "").strip().casefold()
    if not key:
        return "USDC"
    return _SYMBOL_ALIASES.get(key, "USDC")


def parse_funding_utterance(text: str) -> ProposedAction | None:
    """Turn 'buy <NGN> [of] <symbol>' into an onramp action, with regex.

    Returns None when the sentence is not a buy with an amount. A model is
    never asked what the user meant.
    """
    from miriam_agent.hands.transfer import parse_amount

    lowered = (text or "").casefold()
    if not lowered.strip():
        return None
    if "buy" not in lowered and "top up" not in lowered and "fund" not in lowered:
        return None
    # 'sell' sentences belong to the offramp leg, never here.
    if re.search(r"\bsell\b", lowered):
        return None
    match = _BUY_RE.search(text or "")
    if match is None:
        # "top up / fund / add money <amount>" without the word buy.
        if not any(w in lowered for w in ("top up", "fund", "add money")):
            return None
        amount = parse_amount(lowered)
        if amount is None:
            return None
        symbol = "USDC"
        for word in sorted(_SYMBOL_ALIASES, key=len, reverse=True):
            if re.search(rf"\b{re.escape(word)}\b", lowered):
                symbol = _SYMBOL_ALIASES[word]
                break
        return ProposedAction(
            type="onramp",
            amount=amount,
            counterparty=symbol,
            sleeve="spendable",
            raw=text,
            source="user",
            side="buy",
        )
    raw_symbol = (match.group(3) or "").strip().casefold()
    # A bare "buy <amount> of <anything>" is not an onramp: require a crypto
    # word, naira context, or a fund/top-up frame so grocery buys stay advice.
    if raw_symbol not in _SYMBOL_ALIASES and not any(
        w in lowered
        for w in (
            "usdc",
            "usdt",
            "btc",
            "bitcoin",
            "crypto",
            "naira",
            "ngn",
            "top up",
            "top-up",
            "fund",
            "add money",
            "onramp",
            "₦",
        )
    ):
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except Exception:
        return None
    suffix = (match.group(2) or "").lower()
    if suffix in _MULTIPLIERS:
        value *= _MULTIPLIERS[suffix]
    from miriam_agent.hands.ledger import money

    amount = money(value)
    if amount is None or amount <= 0:
        return None
    symbol = classify_symbol(match.group(3))
    return ProposedAction(
        type="onramp",
        amount=amount,
        counterparty=symbol,
        sleeve="spendable",
        raw=text,
        source="user",
        side="buy",
    )


def parse_offramp_utterance(text: str) -> ProposedAction | None:
    """Turn a 'sell ... / withdraw to naira' sentence into an offramp action.

    App-only: the action stages an envelope, never a rail call.
    """
    from miriam_agent.hands.transfer import parse_amount

    lowered = (text or "").casefold()
    if not lowered.strip():
        return None
    if not re.search(r"\bsell\b|\bofframp\b|\bwithdraw\b.*\bnaira\b", lowered):
        return None
    amount = parse_amount(lowered)
    if amount is None:
        return None
    return ProposedAction(
        type="offramp",
        amount=amount,
        counterparty="NGN",
        sleeve="spendable",
        raw=text,
        source="user",
        side="sell",
    )


def extract_otp(text: str) -> str | None:
    """An OTP typed as the user's next message: 4-8 digits, nothing else."""
    match = _OTP_RE.match(text or "")
    return match.group(1) if match else None


# ---- Go writes (hands-only; never registry tools) ----


def _binding_headers(confirm_id: str | None) -> dict[str, str]:
    from miriam_agent.integrations.go_client import CONFIRM_ID_HEADER

    return {CONFIRM_ID_HEADER: confirm_id} if confirm_id else {}


async def initiate_paj_session(
    token: str | None,
    *,
    phone: str | None = None,
    idempotency_key: str | None = None,
    confirm_id: str | None = None,
) -> dict[str, Any]:
    """POST /funding/paj/initiate. Fail-closed without a token."""
    if not token:
        return {"ok": False, "reasons": ["NO_GO_TOKEN"], "status": "rejected"}
    from miriam_agent.integrations.go_client import get_go_client

    payload: dict[str, Any] = {}
    if phone:
        payload["phone"] = phone
    try:
        out = await get_go_client()._token_post(
            "/api/v1/funding/paj/initiate",
            token,
            payload,
            idempotency_key=idempotency_key or _id("idem"),
            extra_headers=_binding_headers(confirm_id),
        )
    except Exception as exc:  # noqa: BLE001 - a rail error is a business result
        logger.warning("paj initiate failed: %s", exc)
        return {
            "ok": False,
            "reasons": ["PAJ_INITIATE_FAILED"],
            "detail": str(exc)[:200],
        }
    if not isinstance(out, dict):
        return {"ok": False, "reasons": ["PAJ_INITIATE_FAILED"]}
    return {"ok": True, "raw": out}


async def verify_paj_otp(
    token: str | None,
    otp: str,
    *,
    phone: str | None = None,
    idempotency_key: str | None = None,
    confirm_id: str | None = None,
) -> dict[str, Any]:
    """POST /funding/paj/verify. Wrong OTP -> retry, never an auto-buy."""
    if not token:
        return {"ok": False, "reasons": ["NO_GO_TOKEN"], "status": "rejected"}
    if not re.fullmatch(r"\d{4,8}", (otp or "").strip()):
        return {
            "ok": False,
            "reasons": ["BAD_OTP_FORMAT"],
            "detail": "the code must be 4-8 digits; nothing moved",
        }
    from miriam_agent.integrations.go_client import get_go_client

    payload: dict[str, Any] = {"otp": otp.strip()}
    if phone:
        payload["phone"] = phone
    try:
        out = await get_go_client()._token_post(
            "/api/v1/funding/paj/verify",
            token,
            payload,
            idempotency_key=idempotency_key or _id("idem"),
            extra_headers=_binding_headers(confirm_id),
        )
    except Exception as exc:  # noqa: BLE001 - a rail error is a business result
        logger.warning("paj verify failed: %s", exc)
        return {
            "ok": False,
            "reasons": ["PAJ_VERIFY_FAILED"],
            "detail": "that code did not verify; no order was created",
        }
    if not isinstance(out, dict) or str(out.get("status") or "").lower() != "verified":
        return {
            "ok": False,
            "reasons": ["PAJ_VERIFY_FAILED"],
            "detail": "that code did not verify; no order was created",
            "raw": out if isinstance(out, dict) else {},
        }
    return {"ok": True, "raw": out}


async def create_onramp(
    token: str | None,
    *,
    kind: str = "paj",
    amount_ngn: Decimal,
    currency: str = "NGN",
    verified: bool = False,
    idempotency_key: str | None = None,
    confirm_id: str | None = None,
) -> dict[str, Any]:
    """POST /funding/{paj,ramp}/onramp. Paj rail requires verified=True."""
    _kind = (kind or "paj").lower()
    if _kind not in ("paj", "ramp"):
        return {
            "ok": False,
            "reasons": ["BAD_RAIL"],
            "detail": f"unknown rail {kind!r}",
        }
    if not token:
        return {"ok": False, "reasons": ["NO_GO_TOKEN"], "status": "rejected"}
    if amount_ngn is None or Decimal(str(amount_ngn)) <= 0:
        return {"ok": False, "reasons": ["BAD_AMOUNT"]}
    if _kind == "paj" and not verified:
        return {
            "ok": False,
            "reasons": ["PAJ_VERIFICATION_REQUIRED"],
            "detail": "verify the Paj recipient code first; no order was created",
        }
    from miriam_agent.integrations.go_client import get_go_client

    payload: dict[str, Any] = {
        "amount": float(amount_ngn),
        "currency": currency or "NGN",
    }
    try:
        out = await get_go_client()._token_post(
            f"/api/v1/funding/{_kind}/onramp",
            token,
            payload,
            idempotency_key=idempotency_key or _id("idem"),
            extra_headers=_binding_headers(confirm_id),
        )
    except Exception as exc:  # noqa: BLE001 - a rail error is a business result
        logger.warning("onramp create failed: %s", exc)
        return {"ok": False, "reasons": ["ONRAMP_FAILED"], "detail": str(exc)[:200]}
    if not isinstance(out, dict):
        return {"ok": False, "reasons": ["ONRAMP_FAILED"]}
    return {"ok": True, "raw": out}


def stage_offramp_envelope(
    *,
    amount_ngn: Decimal,
    currency: str = "NGN",
    bank_code: str | None = None,
    bank_id: str | None = None,
    account_number: str | None = None,
    bank_name: str | None = None,
    rail: str = "paj",
) -> dict[str, Any]:
    """Staged offramp envelope for the Rail app (passcode-gated, app-only).

    Mirrors propose_vault_plan: {path, method, payload, needs} + a
    rail://authorize instruction. Hands never POSTs this.
    """
    _rail = (rail or "paj").lower()
    path = (
        "/api/v1/funding/paj/offramp"
        if _rail == "paj"
        else "/api/v1/funding/ramp/offramp"
    )
    if _rail == "paj":
        payload: dict[str, Any] = {
            "amount": float(amount_ngn),
            "currency": currency,
        }
        if bank_id:
            payload["bankId"] = bank_id
        if account_number:
            payload["accountNumber"] = account_number
    else:
        payload = {
            "amount": float(amount_ngn),
            "currency": currency,
        }
        if bank_code:
            payload["bankCode"] = bank_code
        if account_number:
            payload["accountNumber"] = account_number
        if bank_name:
            payload["bankName"] = bank_name
    envelope = {
        "path": path,
        "method": "POST",
        "payload": payload,
        "needs": ["app_confirm", "passcode"],
    }
    return {
        "staged": True,
        "offramp_envelope": envelope,
        "authorize": "rail://authorize",
        "spoken": (
            f"Cashing out {amount_ngn:g} naira needs your app passcode, so I staged "
            "it for the Rail app. Open the staged cash-out, confirm with your "
            "passcode, and the naira lands in your saved bank."
        ),
    }


__all__ = [
    "FUND_TTL_MINUTES",
    "classify_symbol",
    "create_onramp",
    "extract_otp",
    "initiate_paj_session",
    "parse_funding_utterance",
    "parse_offramp_utterance",
    "stage_offramp_envelope",
    "verify_paj_otp",
]

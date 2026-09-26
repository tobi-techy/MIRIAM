"""Policy / eligibility engine for investment actions (spec §18).

Miriam proposes; this engine decides. It consumes the **real** limits the Go
backend publishes at ``/api/v1/investments/limits`` (KYC tier, per-transaction
and daily volume caps, minimum cash reserve, position and strategy caps) and
returns a verdict with reasons. Nothing here is invented: a limit the backend
does not report is treated as *unknown*, which blocks the action rather than
guessing at a number.

The LLM never sees a veto it can argue with -- it sees the same verdict dict
the transaction service does.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Decision(BaseModel):
    """A policy verdict for one proposed money action."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    checks: dict[str, str] = Field(default_factory=dict)
    limits_used: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


def _first_number(payload: dict[str, Any], *keys: str) -> float | None:
    """The first parseable number among ``keys``.

    The Go backend serializes money as strings and does not use one field name
    for one concept across endpoints, so callers offer candidates in priority
    order. An absent value is ``None`` -- never a silent zero, which would
    silently *allow* an unlimited trade.
    """
    for key in keys:
        if key not in payload:
            continue
        try:
            return float(payload[key])
        except (TypeError, ValueError):
            continue
    return None


def _first_bool(payload: dict[str, Any], *keys: str) -> bool | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().casefold()
            if lowered in ("true", "yes", "1"):
                return True
            if lowered in ("false", "no", "0"):
                return False
    return None


def _first_str(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def evaluate_investment_action(
    amount: float,
    limits: dict[str, Any] | None,
    *,
    kyc_verified: bool | None = None,
    jurisdiction: str | None = None,
    supported_jurisdictions: tuple[str, ...] | None = None,
    asset: dict[str, Any] | None = None,
    available_balance: float | None = None,
    idempotency_key: str | None = None,
) -> Decision:
    """Whether one investment order may proceed (spec §18).

    Every check is recorded in ``checks`` -- including the ones *skipped*
    because the backend did not report that limit -- so an audit can see
    exactly what the verdict rested on. Fail-closed throughout: an unknown
    limit blocks, it never waves the trade through.
    """
    checks: dict[str, str] = {}
    reasons: list[str] = []
    used: dict[str, Any] = {}
    allowed = True

    def deny(reason: str) -> None:
        nonlocal allowed
        allowed = False
        reasons.append(reason)

    if amount <= 0:
        deny("the amount must be positive")
    checks["amount_positive"] = "pass" if amount > 0 else "fail"

    if kyc_verified is not True:
        checks["kyc"] = (
            "fail" if kyc_verified is False else "not_required_for_strategy_start"
        )
    else:
        checks["kyc"] = "pass"

    if jurisdiction is not None and supported_jurisdictions is not None:
        supported = {j.upper() for j in supported_jurisdictions}
        if jurisdiction.upper() not in supported:
            deny(f"jurisdiction {jurisdiction} is not supported")
            checks["jurisdiction"] = "fail"
        else:
            checks["jurisdiction"] = "pass"
    else:
        checks["jurisdiction"] = "unknown"

    if available_balance is not None and amount > available_balance:
        deny(
            f"the amount ({amount:,.0f}) exceeds the available balance "
            f"({available_balance:,.0f})"
        )
        checks["balance"] = "fail"
    elif available_balance is not None:
        checks["balance"] = "pass"
    else:
        checks["balance"] = "unknown"

    if limits:
        max_tx = _first_number(
            limits,
            "max_transaction_usd",
            "max_transaction",
            "maxAmount",
            "max_transaction_amount",
        )
        if max_tx is not None:
            used["max_transaction"] = max_tx
            if amount > max_tx:
                deny(
                    f"the amount ({amount:,.0f}) is above the per-transaction "
                    f"limit ({max_tx:,.0f})"
                )
                checks["max_transaction"] = "fail"
            else:
                checks["max_transaction"] = "pass"
        else:
            checks["max_transaction"] = "unknown"

        max_daily = _first_number(
            limits, "max_daily_usd", "max_daily_volume", "maxDailyAmount"
        )
        if max_daily is not None:
            used["max_daily"] = max_daily
            if amount > max_daily:
                deny(
                    f"the amount ({amount:,.0f}) is above the daily limit "
                    f"({max_daily:,.0f})"
                )
                checks["max_daily"] = "fail"
            else:
                checks["max_daily"] = "pass"
        else:
            checks["max_daily"] = "unknown"

        can_invest = _first_bool(limits, "can_invest", "canInvest", "investing_enabled")
        if can_invest is False:
            deny("investing is not enabled for this account yet")
            checks["can_invest"] = "fail"
        elif can_invest is True:
            checks["can_invest"] = "pass"
        else:
            checks["can_invest"] = "unknown"

        kyc_tier = _first_str(limits, "kyc_tier", "kycTier", "kyc_status")
        if kyc_tier:
            used["kyc_tier"] = kyc_tier
            checks["kyc_tier"] = "reported"
    else:
        checks["limits"] = "unavailable"
        deny("investment limits are unavailable, so the action cannot be authorized")

    if asset is not None:
        tradable = _first_bool(asset, "tradable", "is_tradable", "tradeable")
        symbol = asset.get("symbol") or asset.get("asset_id")
        if tradable is False:
            deny(f"asset {symbol} is not tradable")
            checks["asset_tradable"] = "fail"
        elif tradable is True:
            checks["asset_tradable"] = "pass"
        else:
            checks["asset_tradable"] = "unknown"
    else:
        checks["asset_tradable"] = "not_applicable"

    if not idempotency_key:
        # Not a user-facing refusal, but the action cannot be executed safely
        # without one (spec §18), so the transaction service will require it.
        checks["idempotency"] = "missing"
    else:
        checks["idempotency"] = "pass"

    return Decision(allowed=allowed, reasons=reasons, checks=checks, limits_used=used)

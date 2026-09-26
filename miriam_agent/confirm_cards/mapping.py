"""Miriam-action -> Go-card mapping. Pure functions, no I/O.

The wiring table (§2 of the Face ID plan) is the spec; every row is unit
tested. ``None`` means no card: the challenge keeps its existing chat confirm.

- lock/unlock: internal and reversible, so chat confirm stays (no card).
- onramp/offramp: Paj OTP stays. Face ID never replaces it.
- invest (Glider enroll): no card. The tap + wallet-signature two-tap flow is
  unchanged; a first tap may still arrive via the settle endpoint, which needs
  no card to work.
- limit.change: Miriam has no user-settable limit action; skipped. The Go
  executor stays fail-closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from miriam_agent.hands.ledger import Challenge

#: Payload key the Go side uses to route execution back through Miriam.
MIRIAM_CONFIRM_KEY = "miriam_confirm_id"


@dataclass(frozen=True)
class CardSpec:
    """One Go card mint: action, payload, and optional copy overrides."""

    go_action: str
    payload: dict[str, Any] = field(default_factory=dict)
    title: str | None = None
    subtitle: str | None = None


def _amount_str(amount: Decimal | None) -> str:
    if amount is None:
        return ""
    return str(amount)


def card_for_action(challenge: Challenge) -> CardSpec | None:
    """Map a staged challenge onto a Go card, or None when no card applies."""
    action = challenge.action
    confirm_id = challenge.id
    meta = challenge.meta or {}

    if action == "transfer":
        return CardSpec(
            go_action="transfer.send",
            payload={
                "identifier": challenge.counterparty,
                "amount": _amount_str(challenge.amount),
                MIRIAM_CONFIRM_KEY: confirm_id,
            },
        )
    if action == "internal_move":
        # Internal + reversible, but it moves between real wallets, so it gets
        # a card with honest copy (the save.sweep renderer would call it
        # "Park … per inflow", which is not what a stash move is).
        destination = challenge.destination or challenge.sleeve or "Stash"
        return CardSpec(
            go_action="save.sweep",
            payload={
                "amount": _amount_str(challenge.amount),
                "destination": destination,
                MIRIAM_CONFIRM_KEY: confirm_id,
            },
            title=f"Move {_amount_str(challenge.amount)} to {destination}",
            subtitle="Miriam · approve to move between your wallets",
        )
    if action == "order":
        side = meta.get("side", "buy")
        go_action = "invest.sell" if side == "sell" else "invest.buy"
        symbol = challenge.counterparty or meta.get("symbol", "")
        payload: dict[str, Any] = {
            "symbol": symbol,
            "amount_usd": _amount_str(challenge.amount),
            MIRIAM_CONFIRM_KEY: confirm_id,
        }
        # Only a real strategy id rides along; the "strategy" sleeve tag in
        # challenge meta is not a Glider id and is left out.
        if meta.get("strategy_id"):
            payload["strategy_id"] = meta["strategy_id"]
        return CardSpec(go_action=go_action, payload=payload)
    if action == "set_allocation":
        # The sanctioned path for non-enum shapes: invest.buy with a
        # title/subtitle override. The Go enum is never extended.
        legs = _parse_legs(meta.get("legs", "[]"))
        count = str(len(legs)) if legs is not None else "?"
        strategy = (
            meta.get("strategy_id") or meta.get("strategy") or "Rail Stock Sleeve"
        )
        payload = {
            "strategy_id": meta.get("strategy_id", ""),
            MIRIAM_CONFIRM_KEY: confirm_id,
        }
        if legs is not None:
            payload["legs"] = legs
        return CardSpec(
            go_action="invest.buy",
            payload=payload,
            title=f"Set allocation · {count} legs",
            subtitle=strategy,
        )
    if action == "rebalance":
        strategy = (
            meta.get("strategy_id")
            or meta.get("strategy")
            or challenge.counterparty
            or "Rail Stock Sleeve"
        )
        return CardSpec(
            go_action="invest.buy",
            payload={
                "strategy_id": meta.get("strategy_id", meta.get("strategy", "")),
                MIRIAM_CONFIRM_KEY: confirm_id,
            },
            title=f"Rebalance · {strategy}",
            subtitle="Miriam · approve to rebalance now",
        )
    if action == "pause":
        strategy = (
            meta.get("strategy_id")
            or meta.get("strategy")
            or challenge.counterparty
            or "strategy"
        )
        return CardSpec(
            go_action="mandate.revoke",
            payload={
                "strategy_id": meta.get("strategy_id", meta.get("strategy", "")),
                MIRIAM_CONFIRM_KEY: confirm_id,
            },
            title=f"Pause {strategy}",
            subtitle="Miriam · approve to pause",
        )
    if action == "resume":
        strategy = (
            meta.get("strategy_id")
            or meta.get("strategy")
            or challenge.counterparty
            or "strategy"
        )
        return CardSpec(
            go_action="mandate.approve",
            payload={
                "strategy_id": meta.get("strategy_id", meta.get("strategy", "")),
                MIRIAM_CONFIRM_KEY: confirm_id,
            },
            title=f"Resume {strategy}",
            subtitle="Miriam · approve to resume",
        )
    if action == "save_rule":
        payload = {MIRIAM_CONFIRM_KEY: confirm_id}
        if meta.get("automation_id"):
            payload["automation_id"] = meta["automation_id"]
        # The save.sweep renderer already writes the right copy
        # ("Park 15% of inflow"), so no override here.
        if meta.get("percentage"):
            payload["percentage"] = meta["percentage"]
        elif meta.get("amount"):
            payload["amount"] = meta["amount"]
        else:
            return None
        return CardSpec(go_action="save.sweep", payload=payload)
    # lock/unlock: internal + reversible → chat confirm stays, no card.
    # onramp/offramp: Paj OTP stays; Face ID never replaces it.
    # invest (enroll): wallet-signature two-tap flow is unchanged, no card.
    return None


def _parse_legs(raw: str | list[Any] | None) -> list[Any] | None:
    """Parse stored allocation legs, accepting a JSON string or a real list.

    Challenge meta may carry legs either way; a list-shaped value must ride
    onto the card untouched instead of collapsing to "?" legs.
    """
    if isinstance(raw, list):
        return raw
    try:
        legs = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return None
    return legs if isinstance(legs, list) else None


__all__ = ["MIRIAM_CONFIRM_KEY", "CardSpec", "card_for_action"]

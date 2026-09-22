"""Locked dollar retirement vault: copy guard.

Every user-facing string Miriam can emit about the vault passes through
``check_vault_copy``. The vault is Sleeve A: Rail-owned tiers, human labels
only. Words that would let the model drift into asset-picking,
chain-naming, or provider talk are refused here, before they reach a bubble.

Banned (case-insensitive, substring): crypto, chain, token, wallet, seed,
glider, caip, mint, contract. The internal ``confirmation_token`` field name
must never leak into spoken copy; spoken copy says "confirmation" or
"approve in the app".
"""

from __future__ import annotations

import re
from typing import Final

BANNED_VAULT_WORDS: Final[tuple[str, ...]] = (
    "crypto",
    "chain",
    "token",
    "wallet",
    "seed",
    "glider",
    "caip",
    "mint",
    "contract",
)

_BANNED_RE: Final[re.Pattern[str]] = re.compile(
    "|".join(re.escape(w) for w in BANNED_VAULT_WORDS), re.IGNORECASE
)

VAULT_TIERS: Final[tuple[str, ...]] = ("Steady", "Balanced", "Growth")

_TIER_RISK: Final[dict[str, str]] = {
    "Steady": "Steady keeps the most in calm holdings, so it moves the least.",
    "Balanced": "Balanced splits calm and growth, so it moves a middle amount.",
    "Growth": "Growth leans hardest into growth, so it swings the most.",
}

NOT_LIVE_LINE: Final[str] = "the dollar plans are not live yet."


def check_vault_copy(text: str) -> None:
    """Raise ``ValueError`` if vault copy contains a banned word."""
    if not text:
        return
    hit = _BANNED_RE.search(text)
    if hit:
        raise ValueError(f"vault copy uses a banned word: {hit.group(0)!r}")


def tier_risk_line(tier: str) -> str:
    """One-line risk characterisation for a human tier label. Labels only."""
    return _TIER_RISK.get(tier, "This tier is set by Rail, not by me.")


def assert_known_tier(tier: str) -> str:
    """Normalise a tier label or raise. Never accept weights or ids here."""
    for known in VAULT_TIERS:
        if tier.strip().lower() == known.lower():
            return known
    raise ValueError(f"unknown vault tier: {tier!r}")


__all__ = [
    "BANNED_VAULT_WORDS",
    "NOT_LIVE_LINE",
    "VAULT_TIERS",
    "assert_known_tier",
    "check_vault_copy",
    "tier_risk_line",
]

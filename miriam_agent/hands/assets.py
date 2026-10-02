"""Layer 1 - HANDS. Deterministic asset resolution (the grounding rail).

The single place a company name, ticker, or symbol becomes a canonical asset.
A model is never asked to map "Google" to a ticker: this module resolves it
against a fixed catalogue, and every outcome that is not exactly one canonical
match becomes an ask, never an invented symbol.

This is the grounding half of the trust boundary. It answers "what asset does
the user mean?", and it does so in code, from a catalogue, so a hallucinated
or misspelled ticker can only ever surface as ``unknown``/``ambiguous`` -- it
can never reach a money action.

``supported=False`` marks an asset the catalogue knows but the product does not
yet move (single-name equities). The caller uses that to offer the live
alternative (the diversified stock sleeve) instead of inventing a buy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class AssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"
    FUND = "fund"


@dataclass(frozen=True)
class Asset:
    """One canonical asset in the catalogue."""

    symbol: str
    name: str
    currency: str
    asset_class: AssetClass = AssetClass.EQUITY
    supported: bool = True
    aliases: tuple[str, ...] = ()

    def match_tokens(self) -> set[str]:
        """Every lowercase token that should resolve to this asset."""
        tokens = {self.symbol.casefold(), self.name.casefold()}
        tokens.update(a.casefold() for a in self.aliases)
        return tokens


ResolutionKind = Literal["resolved", "ambiguous", "unknown"]


@dataclass(frozen=True)
class AssetResolution:
    """The outcome of resolving a user's asset reference."""

    kind: ResolutionKind
    asset: Asset | None = None
    candidates: tuple[Asset, ...] = field(default_factory=tuple)
    query: str = ""

    @property
    def resolved(self) -> bool:
        return self.kind == "resolved" and self.asset is not None


# The catalogue. Small and deliberate, and it is the SINGLE source of truth for
# company-name -> ticker: ``hands/nl.py::_parse_symbol`` derives its
# ``_COMPANY_ALIASES`` from :func:`company_aliases` below, so the two cannot
# drift apart (the codebase's standing rule: one implementation of a rule).
#
# Every entry carries its settlement currency and whether the product moves it
# today (``supported=False`` = resolvable, but not a live single-name product).
# The order matches the historical ``_COMPANY_ALIASES`` order so ``_parse_symbol``
# keeps picking the same company when a sentence names more than one.
_ASSETS: tuple[Asset, ...] = (
    Asset(
        symbol="AAPL",
        name="Apple Inc.",
        currency="USD",
        supported=False,
        aliases=("apple",),
    ),
    Asset(
        symbol="NVDA",
        name="NVIDIA Corporation",
        currency="USD",
        supported=False,
        aliases=("nvidia",),
    ),
    Asset(
        symbol="TSLA",
        name="Tesla, Inc.",
        currency="USD",
        supported=False,
        aliases=("tesla",),
    ),
    Asset(
        symbol="MSFT",
        name="Microsoft Corporation",
        currency="USD",
        supported=False,
        aliases=("microsoft",),
    ),
    Asset(
        symbol="GOOGL",
        name="Alphabet Inc. Class A",
        currency="USD",
        supported=False,
        aliases=("google", "alphabet"),
    ),
    Asset(
        symbol="AMZN",
        name="Amazon.com, Inc.",
        currency="USD",
        supported=False,
        aliases=("amazon",),
    ),
    Asset(
        symbol="META",
        name="Meta Platforms, Inc.",
        currency="USD",
        supported=False,
        aliases=("meta", "facebook"),
    ),
)

_NORMALISE_RE = re.compile(r"[^a-z0-9 .]", re.IGNORECASE)


def _normalise(query: str) -> str:
    """Lowercase and strip punctuation/cash-symbol noise for matching."""
    return _NORMALISE_RE.sub(" ", query or "").casefold().strip()


def _exact_tokens(catalogue: tuple[Asset, ...]) -> dict[str, list[Asset]]:
    """Build a token -> assets index, so lookup is deterministic."""
    index: dict[str, list[Asset]] = {}
    for asset in catalogue:
        for token in asset.match_tokens():
            index.setdefault(token, []).append(asset)
    return index


def resolve_asset(
    query: str, *, catalogue: tuple[Asset, ...] | None = None
) -> AssetResolution:
    """Resolve a user's asset reference to zero, one, or several catalogue entries.

    * exactly one canonical match -> ``resolved``
    * several matches             -> ``ambiguous`` (the caller asks which one)
    * no match                    -> ``unknown`` (the caller asks for the name)

    A misspelling or a hallucinated symbol can only land in ``unknown``; there
    is no path that invents a ticker.
    """
    assets = catalogue if catalogue is not None else _ASSETS
    token = _normalise(query)
    if not token:
        return AssetResolution(kind="unknown", query=query)

    index = _exact_tokens(assets)
    matches = index.get(token, [])

    if not matches:
        # The bare symbol may be wrapped in a sentence ("buy googl shares");
        # still resolve an exact ticker token even when extra words ride along.
        for asset in assets:
            if asset.symbol.casefold() in token.split():
                matches.append(asset)

    matches = list(dict.fromkeys(matches))
    if not matches:
        return AssetResolution(kind="unknown", query=query)
    if len(matches) == 1:
        return AssetResolution(kind="resolved", asset=matches[0], query=query)
    return AssetResolution(kind="ambiguous", candidates=tuple(matches), query=query)


def company_aliases() -> dict[str, str]:
    """Company-name -> canonical ticker, in catalogue (and historical) order.

    The single source of truth a symbol parser reads. ``hands/nl.py`` derives
    its ``_COMPANY_ALIASES`` from here (appending its own ``x`` marker), so a
    company name resolves to exactly one ticker everywhere it is parsed.
    """
    out: dict[str, str] = {}
    for asset in _ASSETS:
        for alias in asset.aliases:
            out.setdefault(alias, asset.symbol)
    return out


def known_assets() -> tuple[Asset, ...]:
    """The catalogue, for callers that need to enumerate what is known."""
    return _ASSETS


__all__ = [
    "Asset",
    "AssetClass",
    "AssetResolution",
    "company_aliases",
    "known_assets",
    "resolve_asset",
]

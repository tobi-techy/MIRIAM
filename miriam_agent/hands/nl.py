"""Layer 1 - HANDS. Deterministic natural-language parsing.

One home for turning the user's plain-English sentences into structured
actions, with regex and word tables, never a model. Split out of
hands/transfer.py and hands/orders.py (blob ratchet): the execution legs
import the parsers from here and re-export them, so every existing import
path keeps working.

What lives here:

* shared helpers: politeness/carrier-phrase stripping, spelled-out amounts
  ("fifty", "one hundred and twenty k"), the order sleeve constant
* transfer parsing: ``parse_amount``, ``parse_transfer_utterance``
* sleeve-order parsing: ``parse_order_utterance``,
  ``parse_rebalance_utterance``, ``parse_performance_utterance``

Fail-closed throughout: an unknown sentence is ``None``, and Judgment asks
a question instead of a model guessing what money should move.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from miriam_agent.hands.ledger import money
from miriam_agent.hands.state import ProposedAction

# The sleeve all order movements draw on. Never the spend pot.
ORDER_SLEEVE = "savings"

# Carrier phrases people wrap around requests ("please can you send ...").
# Stripped before verb matching so plain English reaches the verbs.
_CARRIER_RE = re.compile(
    r"\b(please|kindly|can you|could you|would you|i want to|i'd like to|"
    r"i wanna|i need to|i need you to|help me|just|actually|let me)\b"
)


def strip_carrier(text: str) -> str:
    """Lowercase a sentence with politeness/carrier phrases removed."""
    lowered = (text or "").casefold()
    return _CARRIER_RE.sub(" ", lowered)


# Plain-English number words, so "fifty dollars" and "one hundred k" parse.
# People rarely type digits when they talk to Miriam.
_WORD_NUMBERS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
    "thousand": 1000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
    "grand": 1000,
    # Fractions only make sense against a unit ("half a million"); a bare
    # "half" with no magnifier is not an amount ("send half to Femi" asks).
    "half": 0.5,
}
_WORD_MAGNIFIERS = {"hundred": 100, "thousand": 1000, "million": 1_000_000}


def _words_to_number(lowered: str) -> Decimal | None:
    """Parse a spelled-out amount ("fifty", "one hundred and twenty k").

    Returns None when no number words are present. Handles tens+units
    ("twenty five"), hundreds ("two hundred"), and k/m suffixes.
    """
    tokens = re.findall(r"[a-z]+", lowered or "")
    if not tokens:
        return None
    total = 0.0
    current = 0.0
    seen = False
    mag_seen = False
    for tok in tokens:
        if tok in ("and", "a", "an"):
            continue
        if tok not in _WORD_NUMBERS:
            if seen:
                break
            continue
        seen = True
        value = _WORD_NUMBERS[tok]
        if tok in _WORD_MAGNIFIERS:
            mag_seen = True
            if current == 0:
                current = 1
            current *= value
            if value >= 1000:
                total += current
                current = 0
        else:
            current += value
    if not seen:
        return None
    total += current
    # A trailing k/m word ("fifty k") multiplies, like the digit path.
    tail = tokens[-3:]
    if any(t in ("k",) for t in tail):
        total *= 1000
        mag_seen = True
    elif any(t in ("m", "million") for t in tail) and total < 1_000_000:
        # "fifty million" already multiplied above; bare "fifty m" needs it.
        if "million" not in tokens:
            total *= 1_000_000
            mag_seen = True
    if total < 1 and not mag_seen:
        # "half", "zero", or dust with no unit is not an amount to move.
        return None
    try:
        return money(Decimal(str(total)))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Transfer parsing
# ---------------------------------------------------------------------------

_AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k|m|mille|thousand)?", re.IGNORECASE)
_TO_RE = re.compile(r"\bto\s+([A-Za-z][A-Za-z'\- ]{1,40})", re.IGNORECASE)
# "send Femi 5k": the name rides right after the verb, no "to" in sight.
_VERB_NAME_RE = re.compile(
    r"\b(?:send|pay|give|transfer|wire)\s+([A-Z][A-Za-z'\-]{1,39})"
)
_SEND_WORDS = (
    "send",
    "transfer",
    "pay",
    "give",
    "move",
    "wire",
    "ship",
    "forward",
    "lend",
    "front me",
    "send over",
    "shoot over",
)
_BUY_WORDS = ("buy", "purchase", "afford", "get")
_LOCK_WORDS = ("lock", "freeze", "reserve")
_UNLOCK_WORDS = ("unlock", "release", "unfreeze")

_MULTIPLIERS = {"k": 1000, "thousand": 1000, "m": 1_000_000, "mille": 1000}

# Names that name the user's own sleeves, never a person. A sentence that says
# "move N to <sleeve>" is an internal move, not a P2P send.
_SLEEVE_WORDS = ("spendable", "savings", "stash", "yield", "locked")

_SLEEVE_ALIASES = {"stash": "savings"}


def parse_amount(text: str) -> Decimal | None:
    """The first money amount in a sentence, digits or plain words.

    "50", "50k", "fifty", "one hundred and twenty" all parse; the k/m
    suffix works on both. Digits win when both are present.
    """
    match = _AMOUNT_RE.search(text or "")
    if match is not None:
        try:
            value = Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            value = None
        if value is not None:
            suffix = (match.group(2) or "").lower()
            if suffix in _MULTIPLIERS:
                value *= _MULTIPLIERS[suffix]
            return money(value)
    return _words_to_number((text or "").casefold())


def parse_transfer_utterance(text: str) -> ProposedAction | None:
    """Turn a user's sentence into a structured action, with regex.

    Returns ``None`` when the sentence does not clearly ask for a concrete
    action. A model is never asked what the user meant in money terms: an
    unknown sentence becomes no action, and Judgment asks a question instead.

    Sleeve names are never counterparties. "move 1k to savings|stash|yield|
    locked" is an internal move between the user's own sleeves; a person name
    stays a P2P transfer.
    """
    lowered = strip_carrier(text)
    if not lowered.strip():
        return None
    amount = parse_amount(lowered)
    if amount is None:
        return None

    match = _TO_RE.search(text or "")
    counterparty = match.group(1).strip() if match else ""
    if not counterparty:
        # Plain English often drops "to": "send Femi 5k", "pay Ada fifty".
        # Only a capitalized name right after the verb counts, so lowercase
        # English ("send money home") never becomes a counterparty.
        verb_match = _VERB_NAME_RE.search(text or "")
        counterparty = verb_match.group(1).strip() if verb_match else ""
    destination = counterparty.casefold().strip(" .,;:")

    if any(word in lowered for word in _SEND_WORDS) and destination in _SLEEVE_WORDS:
        to_sleeve = _SLEEVE_ALIASES.get(destination, destination)
        return ProposedAction(
            type="internal_move",
            amount=amount,
            counterparty="",
            sleeve=to_sleeve,
            raw=text,
            source="user",
        )

    if any(word in lowered for word in _UNLOCK_WORDS):
        return ProposedAction(
            type="unlock", amount=amount, sleeve="locked", raw=text, source="user"
        )
    if any(word in lowered for word in _LOCK_WORDS):
        return ProposedAction(
            type="lock", amount=amount, sleeve="spendable", raw=text, source="user"
        )
    if any(word in lowered for word in _SEND_WORDS):
        return ProposedAction(
            type="transfer",
            amount=amount,
            counterparty=counterparty,
            sleeve="spendable",
            raw=text,
            source="user",
        )
    if any(word in lowered for word in _BUY_WORDS):
        return ProposedAction(
            type="purchase",
            amount=amount,
            counterparty=counterparty,
            sleeve="spendable",
            raw=text,
            source="user",
        )
    return None


# ---------------------------------------------------------------------------
# Sleeve-order parsing (single-name tickers, rebalance, performance)
# ---------------------------------------------------------------------------

_ORDER_BUY_WORDS = frozenset(
    {
        "buy",
        "long",
        "add",
        "pick up",
        "pickup",
        "purchase",
        "get",
        "grab",
        "invest in",
        "i want to buy",
        "i'd like to buy",
        "buy me",
    }
)
_ORDER_SELL_WORDS = frozenset(
    {
        "sell",
        "short",
        "dump",
        "trim",
        "sell off",
        "get rid of",
        "i want to sell",
        "i'd like to sell",
        "sell me",
    }
)

# Plain-English company names people actually say -> sleeve tickers.
_COMPANY_ALIASES = {
    "apple": "AAPLx",
    "nvidia": "NVDAx",
    "tesla": "TSLAx",
    "microsoft": "MSFTx",
    "google": "GOOGLx",
    "alphabet": "GOOGLx",
    "amazon": "AMZNx",
    "meta": "METAx",
    "facebook": "METAx",
}

_TOKEN_RE = re.compile(r"\b([A-Za-z]{2,6})\b")
_STOPWORDS = frozenset(
    {
        "I",
        "A",
        "OR",
        "AN",
        "AS",
        "AT",
        "IN",
        "ON",
        "OF",
        "TO",
        "MY",
        "ME",
        "UP",
        "US",
        "SO",
        "NO",
        "DO",
        "BE",
        "IS",
        "IT",
        "AND",
        "THE",
        "FOR",
        "PLEASE",
        "WANT",
        "LIKE",
        "JUST",
        "NOW",
        "SOME",
        "WITH",
        "BUY",
        "SELL",
        "ADD",
        "GET",
        "LONG",
        "SHORT",
        "DUMP",
        "TRIM",
        "GRAB",
    }
)
_REBALANCE_WORDS = frozenset(
    {
        "rebalance",
        "re-balance",
        "rebal",
        "balance my",
        "even out",
        "sort out my",
        "fix my portfolio",
        "tidy up my",
    }
)
_PERFORMANCE_WORDS = frozenset(
    {"performance", "returns", "return", "doing", "grown", "growth", "pnl", "p&l"}
)


def _parse_side(text: str) -> Literal["buy", "sell"] | None:
    lowered = strip_carrier(text)
    has_buy = any(w in lowered for w in _ORDER_BUY_WORDS)
    has_sell = any(w in lowered for w in _ORDER_SELL_WORDS)
    if has_buy and not has_sell:
        return "buy"
    if has_sell and not has_buy:
        return "sell"
    return None


def _parse_symbol(text: str) -> str | None:
    """Find the stock symbol, never the verb, never a grocery word.

    Only three signals count, in order:
    1. an explicit `x`-suffixed ticker ("NVDAx", any case),
    2. a known company name ("nvidia" -> NVDAx),
    3. an ALL-CAPS ticker ("buy 50 NVDA").
    A lowercase English word ("worth", "groceries") is never a symbol, so
    "buy groceries worth 50" stays purchase advice instead of becoming a
    bogus WORTHx order. Last mention wins ("buy 50 NVDAx", not BUYx).
    """
    raw = text or ""
    lowered = raw.casefold()
    for name, ticker in _COMPANY_ALIASES.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return ticker
    candidates: list[tuple[int, str]] = []
    for match in _TOKEN_RE.finditer(raw):
        token = match.group(1)
        if token.upper() in _STOPWORDS:
            continue
        has_x_suffix = len(token) >= 3 and token[-1] in ("x", "X")
        if has_x_suffix:
            stem = token[:-1]
            if not stem.isalpha() or len(stem) < 2 or len(stem) > 5:
                continue
            candidates.append((2, f"{stem.upper()}x"))
        elif token.isupper() and token.isalpha() and 2 <= len(token) <= 5:
            candidates.append((1, f"{token.upper()}x"))
        # Anything else (lowercase English, verbs, misc words) is ignored.
    if not candidates:
        return None
    # Highest score wins (explicit x-suffix beats bare caps); ties go to
    # the last mention, which is where people put the ticker.
    best_score = max(s for s, _ in candidates)
    for score, sym in reversed(candidates):
        if score == best_score:
            return sym
    return candidates[-1][1]


def parse_order_utterance(text: str) -> ProposedAction | None:
    """Turn a 'buy 50 NVDAx' sentence into an order action, with regex."""
    lowered = (text or "").casefold()
    if not lowered.strip():
        return None
    side = _parse_side(text)
    if side is None:
        return None
    amount = parse_amount(lowered)
    if amount is None:
        return None
    symbol = _parse_symbol(text)
    if symbol is None:
        return None
    return ProposedAction(
        type="order",
        amount=amount,
        counterparty=symbol,
        sleeve=ORDER_SLEEVE,
        raw=text,
        source="user",
        side=side,
    )


def parse_rebalance_utterance(text: str) -> ProposedAction | None:
    """Turn a 'rebalance my sleeve' sentence into a rebalance action."""
    lowered = (text or "").casefold()
    if not lowered.strip():
        return None
    if not any(word in lowered for word in _REBALANCE_WORDS):
        return None
    if not any(
        word in lowered
        for word in (
            "sleeve",
            "stocks",
            "stock",
            "portfolio",
            "investments",
            "holdings",
            "allocation",
        )
    ):
        return None
    return ProposedAction(
        type="rebalance",
        counterparty="Rail Stock Sleeve",
        sleeve=ORDER_SLEEVE,
        raw=text,
        source="user",
    )


def parse_performance_utterance(text: str) -> ProposedAction | None:
    """Detect a sleeve performance question. Read-only, never an order."""
    lowered = (text or "").casefold()
    if not lowered.strip():
        return None
    if not any(word in lowered for word in _PERFORMANCE_WORDS):
        return None
    if not any(word in lowered for word in ("sleeve", "stocks", "stock", "portfolio")):
        return None
    return ProposedAction(
        type="none",
        counterparty="Rail Stock Sleeve",
        sleeve=ORDER_SLEEVE,
        raw=text,
        source="user",
    )


__all__ = [
    "ORDER_SLEEVE",
    "parse_amount",
    "parse_order_utterance",
    "parse_performance_utterance",
    "parse_rebalance_utterance",
    "parse_transfer_utterance",
    "strip_carrier",
]

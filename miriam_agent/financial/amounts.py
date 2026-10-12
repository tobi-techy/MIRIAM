"""Amount and currency parsing for deterministic money extraction (spec §6).

Split out of :mod:`miriam_agent.financial.profile` for the blob ratchet; the
logic is unchanged and ``profile`` re-imports these names, so the public
surface is preserved.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

_NUMBER_WORDS: dict[str, float] = {
    "half": 0.5,
    "a": 1.0,
    "an": 1.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
}
_MAGNITUDES: dict[str, float] = {
    "k": 1_000.0,
    "thousand": 1_000.0,
    "m": 1_000_000.0,
    "mn": 1_000_000.0,
    "million": 1_000_000.0,
    "b": 1_000_000_000.0,
    "bn": 1_000_000_000.0,
    "billion": 1_000_000_000.0,
}

# Digit form: 500k, 1.5m, 500,000, 350000, NGN500k
_DIGIT_AMOUNT_RE = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<mag>k|m|mn|b|bn|thousand|million|billion)?\b",
    re.IGNORECASE,
)

# Word form: "half a million", "a million", "two hundred thousand"
_WORD_AMOUNT_RE = re.compile(
    r"\b(?P<count>half|an?|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:a\s+)?(?P<hundred>hundred\s+)?(?P<mag>thousand|million|billion)\b",
    re.IGNORECASE,
)

# Years are not amounts: a bare 1900-2100 with no money marker is a date.
_YEAR_LOW, _YEAR_HIGH = 1900, 2100

_CURRENCY_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("\u20a6", "NGN"),
    ("$", "USD"),
    ("\u00a3", "GBP"),
    ("\u20ac", "EUR"),
)
_CURRENCY_WORDS: tuple[tuple[str, str], ...] = (
    ("naira", "NGN"),
    ("ngn", "NGN"),
    ("dollars", "USD"),
    ("dollar", "USD"),
    ("usd", "USD"),
    ("pounds", "GBP"),
    ("pound", "GBP"),
    ("gbp", "GBP"),
    ("euros", "EUR"),
    ("euro", "EUR"),
    ("eur", "EUR"),
    ("cedis", "GHS"),
    ("shillings", "KES"),
    ("rand", "ZAR"),
)


def _has_money_marker(text: str, match: re.Match[str]) -> bool:
    """True when a currency symbol or money word sits around this amount."""
    window = text[max(0, match.start() - 12) : match.end() + 12].casefold()
    return any(symbol in window for symbol, _ in _CURRENCY_SYMBOLS) or any(
        word in window for word, _ in _CURRENCY_WORDS
    )


def _amount_from_digit(match: re.Match[str], text: str) -> float | None:
    raw = match.group("num")
    try:
        base = float(raw.replace(",", ""))
    except ValueError:
        return None
    mag = (match.group("mag") or "").strip().casefold()
    if mag:
        return base * _MAGNITUDES.get(mag, 1.0)
    if "," not in raw and "." not in raw and float(base).is_integer():
        value = int(base)
        if _YEAR_LOW <= value <= _YEAR_HIGH and not _has_money_marker(text, match):
            return None
    return base


def _amount_from_words(match: re.Match[str]) -> float | None:
    count = _NUMBER_WORDS.get(match.group("count").casefold())
    if count is None:
        return None
    scale = 100.0 if match.group("hundred") else 1.0
    magnitude = _MAGNITUDES.get(match.group("mag").casefold(), 1.0)
    return count * scale * magnitude


class _Candidate(BaseModel):
    """One amount found in the text, with how it was written."""

    model_config = ConfigDict(extra="forbid")

    value: float
    start: int
    end: int
    explicit: bool  # carried a magnitude/unit rather than a bare number
    spelled: bool  # written as words ("half a million")


def _find_amounts(text: str) -> list[_Candidate]:
    """Every amount in ``text`` -- digit and word forms -- ordered by position.
    A word form overlapping a digit hit is skipped: the digits are the more
    explicit reading of the same money."""
    found: list[_Candidate] = []
    for match in _DIGIT_AMOUNT_RE.finditer(text):
        value = _amount_from_digit(match, text)
        if value is None or value <= 0:
            continue
        found.append(
            _Candidate(
                value=value,
                start=match.start(),
                end=match.end(),
                explicit=bool(match.group("mag")),
                spelled=False,
            )
        )
    for match in _WORD_AMOUNT_RE.finditer(text):
        if any(f.start <= match.start() < f.end for f in found):
            continue
        value = _amount_from_words(match)
        if value is None or value <= 0:
            continue
        found.append(
            _Candidate(
                value=value,
                start=match.start(),
                end=match.end(),
                explicit=True,
                spelled=True,
            )
        )
    found.sort(key=lambda c: (c.start, c.end))
    return found


def detect_currency(text: str, default: str = "NGN") -> tuple[str, bool]:
    """The currency in ``text`` and whether it had to be defaulted.

    Returns ``(code, assumed)``. ``assumed`` True means no currency was named,
    so the caller records the default as an explicit *assumption* instead of
    pretending the user said it (spec §10: label estimates, never fabricate).
    """
    lowered = (text or "").casefold()
    for symbol, code in _CURRENCY_SYMBOLS:
        if symbol in (text or ""):
            return code, False
    for word, code in _CURRENCY_WORDS:
        if re.search(rf"\b{word}\b", lowered):
            return code, False
    return default.upper(), True

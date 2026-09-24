"""Structured financial profile + deterministic money extraction (spec §6, §7).

Miriam's onboarding is a conversation, not a form. The user says things like
"I make about 500k every month and spend maybe 350k" -- the LLM is responsible
for *understanding* that, but the backend is responsible for the *normalized
value*. This module is that backend half:

  - :class:`FinancialFact` -- one field with provenance: value, source,
    confidence, timestamp, currency. Nothing enters the profile without saying
    where it came from and how sure we are (§7).
  - :class:`FinancialProfile` -- the §7 field set, each an optional fact, with a
    deterministic **merge policy** so a user correction always beats a stale
    inference (a later, vaguer guess never erases what they actually told us).
  - :func:`extract_money_facts` -- deterministic natural-language extraction of
    amounts, frequencies and currencies from free text, so the numbers Miriam
    reasons over are normalized once, in one place, and are testable.

Design rules:
  - Never invent a number. Extraction only ever reports what a cue + an amount
    actually said; anything derived carries a lower confidence and an
    assumption string.
  - Currency defaults to NGN (Rail is Nigeria-first: naira billpay, naira
    balances), and the default is always *recorded as an assumption* rather
    than silently assumed.
  - Pure functions only -- no I/O, no LLM, no DB. Everything here is a
    deterministic, unit-testable transform.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Field vocabulary (spec §7)
# ---------------------------------------------------------------------------

# The full §7 schema. Every field is optional: the system asks the minimum it
# needs for a useful diagnosis (§8) and fills the rest over time.
PROFILE_FIELDS: tuple[str, ...] = (
    "income_amount",
    "income_currency",
    "income_frequency",
    "income_type",
    "income_volatility",
    "essential_expenses",
    "discretionary_expenses",
    "debt",
    "savings",
    "emergency_fund",
    "financial_dependents",
    "current_investments",
    "investment_experience",
    "financial_goal",
    "goal_horizon",
    "financial_stress",
    "spending_behavior",
    "saving_behavior",
    "investing_behavior",
)

# Fields whose value is a *number*. When something else (a conductor fact key, a
# statement line) has already told us what a value is, these accept the amount
# without demanding a cue word -- and prose is never stored in them, because a
# sentence where an amount belongs would silently poison every calculation.
MONEY_FIELDS: frozenset[str] = frozenset(
    {
        "income_amount",
        "essential_expenses",
        "discretionary_expenses",
        "debt",
        "savings",
        "emergency_fund",
    }
)

# Provenance ranks for the merge policy. A user correction outranks anything;
# hard evidence (a statement, the ledger) outranks a conversational read; a
# model inference is the weakest thing we will store.
SOURCE_RANK: dict[str, int] = {
    "user_correction": 5,
    "user": 4,
    "statement": 3,
    "ledger": 3,
    "conversation": 2,
    "inferred": 1,
    "system": 1,
}
DEFAULT_SOURCE_RANK = 1


def _now() -> float:
    return datetime.now(UTC).timestamp()


class FinancialFact(BaseModel):
    """One field value plus its provenance (spec §7).

    ``value`` is deliberately ``Any``: amounts are floats, frequencies are
    strings, dependents is an int, a goal is a string, and a list of
    investments is a list. ``confidence`` is always present so downstream
    engines can weight it, and ``updated_at`` makes recency comparable.
    """

    model_config = ConfigDict(extra="forbid")

    value: Any
    source: str = "inferred"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    updated_at: float = Field(default_factory=_now)
    currency: str | None = None
    note: str = ""

    def money(self) -> float | None:
        """The value as a float when it is a money amount, else ``None``.

        The Go backend serializes amounts as strings, and an LLM may hand us
        ``"500000"`` or ``500000.0``; both normalize here.
        """
        if isinstance(self.value, bool) or self.value is None:
            return None
        try:
            return float(self.value)
        except (TypeError, ValueError):
            return None


class FinancialProfile(BaseModel):
    """The structured financial profile, one optional fact per §7 field.

    Unknown fields are rejected (``extra="forbid"``) so a drifting caller fails
    loudly instead of quietly writing junk into memory.
    """

    model_config = ConfigDict(extra="forbid")

    income_amount: FinancialFact | None = None
    income_currency: FinancialFact | None = None
    income_frequency: FinancialFact | None = None
    income_type: FinancialFact | None = None
    income_volatility: FinancialFact | None = None
    essential_expenses: FinancialFact | None = None
    discretionary_expenses: FinancialFact | None = None
    debt: FinancialFact | None = None
    savings: FinancialFact | None = None
    emergency_fund: FinancialFact | None = None
    financial_dependents: FinancialFact | None = None
    current_investments: FinancialFact | None = None
    investment_experience: FinancialFact | None = None
    financial_goal: FinancialFact | None = None
    goal_horizon: FinancialFact | None = None
    financial_stress: FinancialFact | None = None
    spending_behavior: FinancialFact | None = None
    saving_behavior: FinancialFact | None = None
    investing_behavior: FinancialFact | None = None

    # -- reads ----------------------------------------------------------

    def fact(self, field: str) -> FinancialFact | None:
        """The fact for ``field``, or ``None``. Unknown names return ``None``
        rather than raising -- callers iterate the field list."""
        if field not in PROFILE_FIELDS:
            return None
        return getattr(self, field, None)

    def value(self, field: str) -> Any:
        """The raw value for ``field``, or ``None``."""
        fact = self.fact(field)
        return fact.value if fact is not None else None

    def money(self, field: str) -> float | None:
        """The value for ``field`` as a float amount, or ``None``."""
        fact = self.fact(field)
        return fact.money() if fact is not None else None

    def confidence(self, field: str) -> float:
        """Confidence for ``field``, or 0.0 when we have nothing."""
        fact = self.fact(field)
        return fact.confidence if fact is not None else 0.0

    def known(self) -> dict[str, Any]:
        """Field -> value for everything we actually know."""
        out: dict[str, Any] = {}
        for field in PROFILE_FIELDS:
            fact = self.fact(field)
            if fact is not None:
                out[field] = fact.value
        return out

    def missing(self, fields: tuple[str, ...] | list[str]) -> list[str]:
        """The subset of ``fields`` we do not yet have (spec §8: ask only for
        what is needed, and only when it is actually missing)."""
        return [f for f in fields if self.fact(f) is None]

    def currency(self) -> str:
        """The profile's currency, defaulting to NGN (Rail is Nigeria-first)."""
        fact = self.fact("income_currency")
        if fact is not None and isinstance(fact.value, str) and fact.value:
            return fact.value.upper()
        if fact is not None and fact.currency:
            return fact.currency.upper()
        return "NGN"

    @property
    def volatile_income(self) -> bool:
        """True when the profile says the income is not steady."""
        fact = self.fact("income_volatility")
        return bool(fact is not None and str(fact.value).casefold() in _VOLATILE_VALUES)

    # -- writes ---------------------------------------------------------

    def set_fact(
        self,
        field: str,
        value: Any,
        *,
        source: str = "inferred",
        confidence: float = 0.5,
        currency: str | None = None,
        note: str = "",
        at: float | None = None,
    ) -> bool:
        """Merge one fact into the profile. Returns True when it was applied.

        Merge policy (deterministic; spec §7 "do not ask again for information
        already confidently established", plus "user corrections win"):

          1. Nothing stored yet -> store it.
          2. Higher source rank wins -- a user correction always beats an
             inference, however recent or confident that inference was.
          3. Same rank -> higher confidence wins.
          4. Same rank and confidence -> the newer fact wins.
          5. Identical value -> refresh recency only, keeping the stronger
             provenance, so re-reading the same fact never downgrades it.

        A refused write returns False so callers can tell "already known" from
        "recorded"; blank values are never stored.
        """
        if field not in PROFILE_FIELDS or _is_blank(value):
            return False
        incoming = FinancialFact(
            value=value,
            source=source,
            confidence=max(0.0, min(1.0, float(confidence))),
            currency=currency,
            note=note,
            updated_at=at if at is not None else _now(),
        )
        current = self.fact(field)
        if current is None:
            setattr(self, field, incoming)
            return True

        if _same_value(current.value, incoming.value):
            current.updated_at = incoming.updated_at
            if _rank(incoming.source) > _rank(current.source) or (
                _rank(incoming.source) == _rank(current.source)
                and incoming.confidence > current.confidence
            ):
                current.source = incoming.source
                current.confidence = incoming.confidence
            if incoming.currency and not current.currency:
                current.currency = incoming.currency
            return False

        if _rank(incoming.source) > _rank(current.source):
            setattr(self, field, incoming)
            return True
        if _rank(incoming.source) == _rank(current.source) and (
            incoming.confidence > current.confidence
            or (
                incoming.confidence == current.confidence
                and incoming.updated_at >= current.updated_at
            )
        ):
            setattr(self, field, incoming)
            return True
        return False

    def set_all(self, facts: dict[str, FinancialFact]) -> dict[str, bool]:
        """Merge a batch of facts (the shape :func:`extract_money_facts`
        returns). Returns field -> applied."""
        applied: dict[str, bool] = {}
        for field, fact in facts.items():
            applied[field] = self.set_fact(
                field,
                fact.value,
                source=fact.source,
                confidence=fact.confidence,
                currency=fact.currency,
                note=fact.note,
                at=fact.updated_at,
            )
        return applied

    def merge(self, other: FinancialProfile) -> dict[str, bool]:
        """Fold another profile in, honoring the same merge policy."""
        return self.set_all(
            {f: fact for f in PROFILE_FIELDS if (fact := other.fact(f)) is not None}
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dump: field -> fact, provenance included."""
        return {
            field: fact.model_dump()
            for field in PROFILE_FIELDS
            if (fact := self.fact(field)) is not None
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> FinancialProfile:
        """Rebuild a profile from a dump. Unknown keys and malformed facts are
        skipped rather than raising, so schema drift in stored memory can never
        break the conversation (fail-open, like the rest of onboarding)."""
        profile = cls()
        if not isinstance(data, dict):
            return profile
        for field, raw in data.items():
            if field not in PROFILE_FIELDS or not isinstance(raw, dict):
                continue
            try:
                fact = FinancialFact.model_validate(raw)
            except Exception:
                continue
            if not _is_blank(fact.value):
                setattr(profile, field, fact)
        return profile


_VOLATILE_VALUES = frozenset(
    {
        "variable",
        "volatile",
        "irregular",
        "unpredictable",
        "freelance",
        "seasonal",
        "commission",
        "lumpy",
        "inconsistent",
        "mixed",
    }
)


def _rank(source: str) -> int:
    return SOURCE_RANK.get((source or "").strip().casefold(), DEFAULT_SOURCE_RANK)


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _same_value(a: Any, b: Any) -> bool:
    """Equality tolerant of the str-vs-float money split ("500000" == 500000) so
    re-reporting the same amount never looks like a correction."""
    if a == b:
        return True
    if isinstance(a, str) or isinstance(b, str):
        try:
            return float(str(a).replace(",", "")) == float(str(b).replace(",", ""))
        except (TypeError, ValueError):
            return str(a).strip().casefold() == str(b).strip().casefold()
    return False


# ---------------------------------------------------------------------------
# Natural extraction (spec §6)
#
# "I make about 500k every month and spend maybe 350k" must normalize to
#   income_amount=500000, income_frequency=monthly, essential_expenses=350000
# without the user ever filling a field. The LLM understands the sentence; the
# functions below own the numbers.
# ---------------------------------------------------------------------------

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


_FREQUENCY_CHECKS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "weekly",
        ("every week", "per week", "a week", "/week", "/wk", "weekly", "each week"),
        ("week",),
    ),
    (
        "daily",
        ("every day", "per day", "a day", "/day", "daily", "each day"),
        ("day",),
    ),
    (
        "yearly",
        (
            "per year",
            "a year",
            "yearly",
            "annually",
            "/year",
            "per annum",
            "each year",
        ),
        ("year",),
    ),
    (
        "monthly",
        (
            "every month",
            "per month",
            "a month",
            "/month",
            "/mo",
            "monthly",
            "each month",
            "pcm",
        ),
        ("month",),
    ),
)


def _detect_frequency(window: str) -> tuple[str | None, float]:
    """The pay/spend cadence in a text window, with its confidence.

    Explicit cadence phrasing ("every month") scores higher than a bare time
    noun ("month"), because "every month" is a statement about the money while
    "this month" is often just a time reference.
    """
    lowered = (window or "").casefold()
    for name, strong, _weak in _FREQUENCY_CHECKS:
        if any(phrase in lowered for phrase in strong):
            return name, 0.95
    for name, _strong, weak in _FREQUENCY_CHECKS:
        if any(re.search(rf"\b{w}\b", lowered) for w in weak):
            return name, 0.7
    return None, 0.0


# Cue words -> profile field. Ordered by specificity: the first cue found
# *closest* to an amount wins, so "i make 500k and spend 350k" routes each
# amount to the right field even though both cues sit in one sentence.
_INCOME_CUES = (
    "take home",
    "take-home",
    "bring in",
    "brings in",
    "come in",
    "comes in",
    "paycheck",
    "salary",
    "commission",
    "freelance",
    "revenue",
    "income",
    "earn",
    "make",
    "made",
    "paid",
    "get",
)
_EXPENSE_CUES = (
    "goes out",
    "going out",
    "outgoing",
    "go out",
    "outflow",
    "spend",
    "spends",
    "spent",
    "spending",
    "expenses",
    "expense",
    "costs",
    "cost",
    "bills",
    "rent",
    "buy",
    "buys",
    "bought",
)
_SAVINGS_CUES = (
    "put away",
    "set aside",
    "saving",
    "savings",
    "save",
    "saves",
    "stash",
    "stashed",
)
_DEBT_CUES = ("owe", "owes", "loan", "debt", "credit card", "borrowed", "installment")
_BUFFER_CUES = ("buffer", "rainy day")

_FIELD_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("emergency_fund", _BUFFER_CUES),
    ("debt", _DEBT_CUES),
    ("savings", _SAVINGS_CUES),
    ("essential_expenses", _EXPENSE_CUES),
    ("income_amount", _INCOME_CUES),
)

# A noun phrase that names the money's *kind* ("essentials", "emergency fund")
# governs the amount that follows it -- a verb is the loosest possible claim on
# money, so the noun wins.
_KIND_PHRASES: tuple[tuple[str, str], ...] = (
    ("essential_expenses", "essentials"),
    ("essential_expenses", "essential expenses"),
    ("essential_expenses", "fixed costs"),
    ("essential_expenses", "fixed cost"),
    ("essential_expenses", "living costs"),
    ("essential_expenses", "monthly costs"),
    ("emergency_fund", "emergency fund"),
    ("emergency_fund", "emergency"),
    ("emergency_fund", "buffer"),
    ("emergency_fund", "runway"),
    ("emergency_fund", "rainy day"),
)

# A savings verb cannot win over a buffer phrase for the same amount:
# "300000 saved as an emergency fund" is the buffer, not savings. These
# compounds govern the amount they *follow*.
_REVERSE_COMPOUNDS: tuple[tuple[str, str], ...] = (
    ("saved as an emergency fund", "emergency_fund"),
    ("saved for an emergency fund", "emergency_fund"),
    ("saved into an emergency fund", "emergency_fund"),
    ("kept as an emergency fund", "emergency_fund"),
    ("saved as emergency fund", "emergency_fund"),
)

# How far a kind phrase may sit from the amount it governs, and the rule that
# keeps it honest: no *other amount* may sit in between, or the phrase belongs
# to that one instead.
_KIND_REACH = 24


def _kind_owners(
    text: str, cands: list[_Candidate]
) -> dict[int, list[tuple[str, str]]]:
    """Which amount each kind phrase owns, computed once per text.

    A noun that names the money's kind governs the *next* amount that follows
    it ("essentials are 400000", "my emergency fund is 100k") -- never an
    earlier amount it merely happens to sit after. The reverse compounds
    ("300000 saved as an emergency fund") govern the amount they follow.
    Keyed by object identity, so it is looked up per candidate in O(1).
    """
    lowered = text.casefold()
    owners: dict[int, list[tuple[str, str]]] = {}

    def _clean(gap: str) -> bool:
        return len(gap) <= _KIND_REACH and not any(ch.isdigit() for ch in gap)

    for field, phrase in _KIND_PHRASES:
        hit = lowered.find(phrase)
        while hit >= 0:
            phrase_end = hit + len(phrase)
            follower = next((c for c in cands if c.start >= phrase_end), None)
            if follower is not None and _clean(lowered[phrase_end : follower.start]):
                owners.setdefault(id(follower), []).append((field, phrase))
            hit = lowered.find(phrase, hit + 1)

    for phrase, field in _REVERSE_COMPOUNDS:
        hit = lowered.find(phrase)
        while hit >= 0:
            preceder = next((c for c in reversed(cands) if c.end <= hit), None)
            if preceder is not None and _clean(lowered[preceder.end : hit]):
                owners.setdefault(id(preceder), []).append((field, phrase))
            hit = lowered.find(phrase, hit + 1)
    return owners


# Context radius used to attribute an amount to a field. Wide enough for
# "i make about 500k every month", tight enough not to steal a neighbour's cue.
_CONTEXT = 34


_CUE_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _cue_pattern(cue: str) -> re.Pattern[str]:
    """A whole-word pattern for one cue word, cached.

    Substring matching caused real false positives ("save" firing on "saved",
    "get" firing on "forget"), so every money cue has to start and end on a
    word boundary. For phrases the entire phrase is wrapped, which also keeps
    hyphenated forms ("take-home") matching.
    """
    pattern = _CUE_PATTERN_CACHE.get(cue)
    if pattern is None:
        pattern = re.compile(rf"\b{re.escape(cue)}\b")
        _CUE_PATTERN_CACHE[cue] = pattern
    return pattern


def _cue_candidates(
    text: str,
    cand: _Candidate,
    owners: dict[int, list[tuple[str, str]]] | None = None,
) -> list[tuple[int, int, str, str]]:
    """Every (priority, distance, field, cue) near this amount, best first.

    Priority -1: a kind phrase *owns* this amount ("essentials are 400000",
              "300000 saved as an emergency fund"). A noun that names the money
              beats a verb that merely floats near it.
    Priority  0: a cue *governs* the amount -- it sits right before it with only
              small talk between ("make about 500k", "rent is 250000"). A
              governing cue wins over a floating neighbour, which is what makes
              "i make 500k and spend 350k" route each amount to its own verb.
    Priority  1: a cue floats nearby (character distance, nearest first).

    The caller walks this list and takes the first cue whose field is still
    unfilled, so one sentence can hold several amounts without a greedy
    neighbour swallowing them.
    """
    lowered = text.casefold()
    window_start = max(0, cand.start - _CONTEXT)
    window_end = min(len(lowered), cand.end + _CONTEXT)
    found: list[tuple[int, int, str, str]] = []
    for field, phrase in (owners or {}).get(id(cand), []):
        found.append((-1, 0, field, phrase))
    for field, cues in _FIELD_CUES:
        for cue in cues:
            for match in _cue_pattern(cue).finditer(lowered[window_start:window_end]):
                start = window_start + match.start()
                end = window_start + match.end()
                distance = min(abs(start - cand.start), abs(start - cand.end))
                priority = 1
                if end <= cand.start:
                    gap = lowered[end : cand.start]
                    if len(gap) <= 12 and not any(ch.isdigit() for ch in gap):
                        priority = 0
                found.append((priority, distance, field, cue))
    found.sort(key=lambda item: (item[0], item[1]))
    return found


_VOLATILE_CUES = (
    "irregular",
    "not steady",
    "unpredictable",
    "varies",
    "varies a lot",
    "volatile",
    "seasonal",
    "commission",
    "freelance",
    "lumpy",
    "all over the place",
    "up and down",
    "inconsistent",
    "some months",
)
_STEADY_CUES = ("steady", "fixed", "salary", "same every month", "salaried", "stable")

_STAFF_CUES = (
    "stress",
    "stressed",
    "anxious",
    "anxiety",
    "worried",
    "worry",
    "scared",
    "panic",
    "drowning",
    "overwhelmed",
    "broke all the time",
)

_EXPERIENCE_CUES: tuple[tuple[str, str], ...] = (
    # Most specific first: "never invested" contains "invested", so the
    # negative read has to win before the generic intermediate cue sees it.
    ("advanced", "asset allocation"),
    ("advanced", "portfolio"),
    ("none", "never invested"),
    ("none", "haven't invested"),
    ("none", "havent invested"),
    ("none", "don't invest"),
    ("none", "dont invest"),
    ("none", "no investments"),
    ("intermediate", "invest"),
    ("intermediate", "invested"),
    ("intermediate", "investing"),
    ("intermediate", "stocks"),
    ("intermediate", "stock"),
    ("intermediate", "etf"),
    ("intermediate", "mutual fund"),
    ("intermediate", "index fund"),
    ("intermediate", "crypto"),
    ("intermediate", "bonds"),
)
_EXPERIENCE_SOURCE = "conversation"

_HORIZON_CUES: tuple[tuple[str, str], ...] = (
    ("short", "next month"),
    ("short", "this year"),
    ("short", "few months"),
    ("medium", "next year"),
    ("medium", "in a year"),
    ("medium", "couple of years"),
    ("long", "five years"),
    ("long", "5 years"),
    ("long", "ten years"),
    ("long", "10 years"),
    ("long", "long term"),
    ("long", "long-term"),
    ("long", "retirement"),
    ("long", "retire"),
)

_SPENDING_BEHAVIORS: tuple[tuple[str, str], ...] = (
    ("discretionary_heavy", "spend too much"),
    ("discretionary_heavy", "overspend"),
    ("discretionary_heavy", "impulse"),
    ("untracked", "don't track"),
    ("untracked", "dont track"),
    ("untracked", "do not track"),
    ("untracked", "doesn't track"),
    ("untracked", "doesnt track"),
    ("untracked", "not tracking"),
    ("untracked", "never track"),
    ("untracked", "no tracking"),
    ("untracked", "no idea where"),
    ("untracked", "don't know where"),
    ("untracked", "dont know where"),
    ("untracked", "disappears"),
)
_SAVING_BEHAVIORS: tuple[tuple[str, str], ...] = (
    ("inconsistent", "can't save"),
    ("inconsistent", "cant save"),
    ("inconsistent", "never save"),
    ("inconsistent", "inconsistently"),
    ("inconsistent", "save inconsistently"),
    ("inconsistent", "don't save"),
    ("inconsistent", "dont save"),
    ("automatic", "automatic"),
    ("automatic", "automate"),
    ("automatic", "auto-save"),
    ("automatic", "standing order"),
)
_INVESTING_BEHAVIORS: tuple[tuple[str, str], ...] = (
    ("none", "never invested"),
    ("none", "don't invest"),
    ("none", "dont invest"),
    ("active", "i invest"),
    ("active", "i do invest"),
    ("active", "invest every month"),
    ("active", "dca"),
)

_DEPENDENT_PATTERNS: tuple[tuple[str, int], ...] = (
    ("send money home", 1),
    ("sends money home", 1),
    ("support my family", 1),
    ("support my mum", 1),
    ("support my mom", 1),
    ("support my mother", 1),
    ("support my parents", 2),
    ("take care of my parents", 2),
    ("my mum", 1),
    ("my mom", 1),
    ("my mother", 1),
    ("my father", 1),
    ("my dad", 1),
    ("my parents", 2),
    ("my kids", 1),
    ("my children", 1),
    ("my child", 1),
    ("my brother", 1),
    ("my sister", 1),
    ("my siblings", 2),
)


# Goal phrasing -> the goal itself (spec §13). "I just want to stop being broke
# all the time" is a goal; so is "saving for a house in 2028". These patterns
# capture what the money is *for*, which is what makes a plan concrete.
_GOAL_PATTERNS: tuple[str, ...] = (
    r"(?:(?:my|the|our)\s+)?goal\s+is\s+([^.,;!?]{3,70})",
    r"i(?:'m| am)\s+saving\s+(?:up\s+)?(?:for|towards?)\s+([^.,;!?]{3,70})",
    r"i(?:'m| am)\s+saving\s+to\s+([^.,;!?]{3,70})",
    r"saving\s+(?:up\s+)?(?:for|towards?)\s+([^.,;!?]{3,70})",
    r"i\s+(?:just\s+)?(?:want|need)\s+(?:to\s+)?([^.,;!?]{3,70})",
    r"i(?:'m| am)\s+planning\s+to\s+([^.,;!?]{3,70})",
    r"(?:for|towards?)\s+(retirement|financial independence|an emergency fund|"
    r"emergency fund|a house|a home|a car|school fees|tuition|my business)",
)
_GOAL_SPLIT = re.compile(r"\s+(?:and|but|because|so|when|then|plus)\s+", re.IGNORECASE)


def goal_from_text(text: str) -> str:
    """The user's financial goal, in their own words (spec §13).

    Deliberately permissive on *content* -- "stop being broke all the time" and
    "a house in 2028" are both goals, and the spec is explicit that users are
    never forced into predefined categories -- but strict about *shape*: the
    captured phrase is cut at the first conjunction, so a whole sentence never
    becomes a goal.
    """
    if not text or not text.strip():
        return ""
    lowered = text.casefold()
    for pattern in _GOAL_PATTERNS:
        match = re.search(pattern, lowered)
        if not match:
            continue
        phrase = _GOAL_SPLIT.split(match.group(1).strip())[0].strip(" ,.'\"")
        if len(phrase) >= 3:
            return phrase[:80]
    return ""


def amount_in(text: str, *, default_currency: str = "NGN") -> FinancialFact | None:
    """The first amount in ``text``, with no cue required.

    Used when something *else* has already established what the value is -- a
    conductor fact keyed ``cashflow``, a statement line labelled salary -- so
    the text only has to supply the number. Confidence reflects how the amount
    was written (digits beat words), never a cue that was not needed.
    """
    candidates = _find_amounts(text or "")
    if not candidates:
        return None
    first = candidates[0]
    currency, assumed = detect_currency(text or "", default_currency)
    confidence = 0.9 if first.explicit and not first.spelled else 0.75
    note = "amount read from the value"
    if first.spelled:
        note += "; amount spelled out"
    if assumed:
        note += f"; currency defaulted to {currency} (none named)"
    return FinancialFact(
        value=first.value,
        source="conversation",
        confidence=confidence,
        currency=currency,
        note=note,
    )


def _qualitative_facts(text: str) -> dict[str, FinancialFact]:
    """The non-money reads extraction owns: volatility, stress, behaviors,
    experience, horizon and dependents. Every one is a plain substring cue, so
    nothing is inferred beyond what was actually written."""
    lowered = (text or "").casefold()
    out: dict[str, FinancialFact] = {}

    def add(field: str, value: Any, confidence: float, note: str) -> None:
        if field not in out:
            out[field] = FinancialFact(
                value=value, source=_EXPERIENCE_SOURCE, confidence=confidence, note=note
            )

    for cue in _VOLATILE_CUES:
        if cue in lowered:
            add("income_volatility", "variable", 0.8, f"read from '{cue}'")
            break
    else:
        for cue in _STEADY_CUES:
            if cue in lowered:
                add("income_volatility", "steady", 0.7, f"read from '{cue}'")
                break

    for cue in _STAFF_CUES:
        if cue in lowered:
            add("financial_stress", "high", 0.7, f"read from '{cue}'")
            break

    for value, cue in _EXPERIENCE_CUES:
        if cue in lowered:
            add("investment_experience", value, 0.8, f"read from '{cue}'")
            break
    for value, cue in _HORIZON_CUES:
        if cue in lowered:
            add("goal_horizon", value, 0.75, f"read from '{cue}'")
            break
    for value, cue in _SPENDING_BEHAVIORS:
        if cue in lowered:
            add("spending_behavior", value, 0.75, f"read from '{cue}'")
            break
    for value, cue in _SAVING_BEHAVIORS:
        if cue in lowered:
            add("saving_behavior", value, 0.75, f"read from '{cue}'")
            break
    for value, cue in _INVESTING_BEHAVIORS:
        if cue in lowered:
            add("investing_behavior", value, 0.75, f"read from '{cue}'")
            break

    dependents = 0
    cue_used = ""
    for cue, count in _DEPENDENT_PATTERNS:
        if cue in lowered:
            dependents = max(dependents, count)
            cue_used = cue_used or cue
    if dependents:
        add("financial_dependents", dependents, 0.7, f"read from '{cue_used}'")

    goal = goal_from_text(text)
    if goal:
        add("financial_goal", goal, 0.8, "stated in the conversation")
    return out


def extract_money_facts(
    text: str,
    *,
    source: str = "conversation",
    default_currency: str = "NGN",
) -> dict[str, FinancialFact]:
    """Normalize free-form money talk into profile facts (spec §6).

    "I make about 500k every month and spend maybe 350k." becomes::

        {
          "income_amount":     FinancialFact(500_000.0, currency="NGN", ...),
          "income_frequency":  FinancialFact("monthly", ...),
          "income_currency":   FinancialFact("NGN", ...),
          "essential_expenses": FinancialFact(350_000.0, currency="NGN", ...),
        }

    Confidence is not a vibe. It is:

      - ``0.95`` an explicit digit amount with an explicit cadence
      - ``0.90`` an explicit digit amount (no cadence, monthly assumed)
      - ``0.80`` an amount spelled out in words ("half a million")
      - ``0.70`` a bare number whose cue was implied by position alone

    A cadence with no currency named records ``monthly`` at ``0.7`` and tags
    the fact's note as an assumption, so the plan engine can label it rather
    than present a guess as a figure the user gave (spec §10).
    """
    facts: dict[str, FinancialFact] = {}
    body = text or ""
    if not body.strip():
        return facts

    currency, assumed_currency = detect_currency(body, default_currency)
    currency_note = (
        f"currency defaulted to {currency} (none named)" if assumed_currency else ""
    )

    # One amount per field: the first, strongest reading wins ("i make 500k" is
    # the income; a later aside about a bonus is not the monthly figure).
    candidates = _find_amounts(body)
    owners = _kind_owners(body, candidates)
    for cand in candidates:
        selected = next(
            (
                (field_name, cue_word_found)
                for _priority, _distance, field_name, cue_word_found in (
                    _cue_candidates(body, cand, owners)
                )
                if field_name not in facts
            ),
            None,
        )
        if selected is None:
            # A second spend amount is lifestyle, not rent: when essentials are
            # already known, the same cue lands on discretionary instead.
            if (
                "essential_expenses" in facts
                and "discretionary_expenses" not in facts
                and any(
                    field_name == "essential_expenses"
                    for _p, _d, field_name, _c in _cue_candidates(body, cand, owners)
                )
            ):
                field = "discretionary_expenses"
                cue_word = "spend"
            else:
                continue
        else:
            field, cue_word = selected
        window = body[
            max(0, cand.start - _CONTEXT) : min(len(body), cand.end + _CONTEXT)
        ]
        frequency, freq_confidence = _detect_frequency(window)
        confidence = 0.95 if (cand.explicit and not cand.spelled) else 0.80
        if frequency is None and not cand.spelled:
            confidence = 0.90
        elif frequency is None:
            confidence = 0.75
        if field in ("income_amount",):
            confidence = min(confidence, 0.98)
        notes = [f"read from '{cue_word}'"]
        if cand.spelled:
            notes.append("amount spelled out")
        if currency_note:
            notes.append(currency_note)
        facts[field] = FinancialFact(
            value=cand.value,
            source=source,
            confidence=round(confidence, 2),
            currency=currency,
            note="; ".join(notes),
        )
        if frequency is not None and field == "income_amount":
            facts["income_frequency"] = FinancialFact(
                value=frequency,
                source=source,
                confidence=round(freq_confidence, 2),
                note=f"read from '{frequency}' cadence near the income",
            )

    if facts:
        facts["income_currency"] = FinancialFact(
            value=currency,
            source=source,
            confidence=0.9 if not assumed_currency else 0.6,
            currency=currency,
            note=currency_note or "currency named explicitly",
        )

    facts.update(_qualitative_facts(body))
    return facts


def extract_profile(
    text: str,
    *,
    source: str = "conversation",
    default_currency: str = "NGN",
    base: FinancialProfile | None = None,
) -> FinancialProfile:
    """Extraction straight to a merged :class:`FinancialProfile`."""
    profile = base.model_copy(deep=True) if base is not None else FinancialProfile()
    profile.set_all(
        extract_money_facts(text, source=source, default_currency=default_currency)
    )
    return profile


# ---------------------------------------------------------------------------
# Bridge: existing onboarding state -> structured profile
# ---------------------------------------------------------------------------

# Free-form fact label -> profile field. The onboarding conductor picks its own
# keys, so this is deliberately keyword-based: "cashflow", "monthly_income" and
# "what_i_earn" all land on income.
_FIELD_BY_KEY: tuple[tuple[str, str], ...] = (
    ("income", "income_amount"),
    ("cashflow", "income_amount"),
    ("earn", "income_amount"),
    ("salary", "income_amount"),
    ("debt", "debt"),
    ("owe", "debt"),
    ("loan", "debt"),
    ("credit", "debt"),
    ("saving", "savings"),
    ("buffer", "emergency_fund"),
    ("emergency", "emergency_fund"),
    ("runway", "emergency_fund"),
    ("spend", "essential_expenses"),
    ("expense", "essential_expenses"),
    ("cost", "essential_expenses"),
    ("bill", "essential_expenses"),
    ("rent", "essential_expenses"),
    ("outgo", "essential_expenses"),
    ("fixed", "essential_expenses"),
    ("food", "essential_expenses"),
    ("transport", "essential_expenses"),
    ("goal", "financial_goal"),
    ("rich_life", "financial_goal"),
    ("desired_life", "financial_goal"),
    ("invest", "investment_experience"),
    ("experience", "investment_experience"),
    ("depend", "financial_dependents"),
    ("obligation", "financial_dependents"),
    ("support", "financial_dependents"),
    ("stress", "financial_stress"),
    ("behavior", "spending_behavior"),
    ("volatil", "income_volatility"),
)

_HORIZON_MONTHS = {"short": 6, "medium": 24, "long": 60}


def _field_for_key(key: str) -> str:
    lowered = (key or "").casefold()
    for needle, field in _FIELD_BY_KEY:
        if needle in lowered:
            return field
    return ""


def _goal_horizon_from_date(raw: str) -> str:
    """A goal target date -> a coarse horizon bucket. Unparseable stays empty
    rather than being guessed."""
    if not raw:
        return ""
    years = [int(y) for y in re.findall(r"\b(19\d\d|20\d\d)\b", raw)]
    if not years:
        return ""
    delta = max(years) - datetime.now(UTC).year
    if delta <= 1:
        return "short"
    if delta <= 3:
        return "medium"
    return "long"


def profile_from_onboarding_state(
    state: Any,
    *,
    base: FinancialProfile | None = None,
) -> FinancialProfile:
    """Project the existing conversational onboarding state into a structured
    profile (spec §36: do not duplicate what exists -- reinterpret it).

    Reads, in descending provenance order:

      1. ``document_summary`` -- a bank statement scan. Recorded as source
         ``statement`` so its numbers outrank anything said in chat.
      2. the conductor's free-form ``learned`` facts -- source ``conversation``,
         mapped to fields by keyword, and parsed for an amount when one exists.
      3. ``money_moment`` / ``goal`` prose -- source ``conversation``.
      4. ``goal_meta.target_date`` -- a coarse horizon bucket.

    Safe on any object with the right attributes (a real ``OnboardingState`` or
    a test double): missing attributes are treated as absent.
    """
    profile = base.model_copy(deep=True) if base is not None else FinancialProfile()

    learned = getattr(state, "learned", None) or {}
    if isinstance(learned, dict):
        for key, value in learned.items():
            if not isinstance(value, str) or not value.strip():
                continue
            field = _field_for_key(str(key))
            if not field:
                continue
            text = value.strip()
            if field in MONEY_FIELDS:
                # The key already told us what this is, so the value only has
                # to supply a number; prose is never stored in a money field.
                fact = amount_in(text)
                if fact is not None:
                    profile.set_all({field: fact})
                    if field == "income_amount":
                        frequency, freq_confidence = _detect_frequency(text)
                        if frequency:
                            profile.set_fact(
                                "income_frequency",
                                frequency,
                                source="conversation",
                                confidence=round(freq_confidence, 2),
                                note=f"read from '{frequency}' cadence in '{key}'",
                            )
                        profile.set_fact(
                            "income_currency", fact.currency, confidence=0.6
                        )
                continue
            extracted = extract_money_facts(text, source="conversation")
            if field in extracted:
                profile.set_all({field: extracted[field]})
                continue
            profile.set_fact(
                field,
                text,
                source="conversation",
                confidence=0.6,
                note=f"conductor fact '{key}'",
            )

    money_moment = getattr(state, "money_moment", "") or ""
    goal = getattr(state, "goal", "") or ""
    if goal:
        profile.set_fact(
            "financial_goal",
            str(goal).strip(),
            source="conversation",
            confidence=0.85,
            note="stated goal",
        )

    document_summary = getattr(state, "document_summary", None)
    if document_summary:
        # Statement numbers are the strongest evidence we hold.
        profile.merge(extract_profile(str(document_summary), source="statement"))

    corpus_parts = [p for p in (str(money_moment), str(goal)) if p.strip()]
    if corpus_parts:
        profile.set_all(
            extract_money_facts(" ".join(corpus_parts), source="conversation")
        )

    goal_meta = getattr(state, "goal_meta", None) or {}
    if isinstance(goal_meta, dict):
        horizon = _goal_horizon_from_date(str(goal_meta.get("target_date") or ""))
        if horizon:
            profile.set_fact(
                "goal_horizon",
                horizon,
                source="conversation",
                confidence=0.7,
                note="derived from the stated target date",
            )

    return profile


def horizon_months(profile: FinancialProfile) -> int | None:
    """The goal horizon in months when we have one (used by readiness)."""
    fact = profile.fact("goal_horizon")
    if fact is None:
        return None
    return _HORIZON_MONTHS.get(str(fact.value).casefold())

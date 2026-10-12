"""Note parsing and classification for a shop stock book.

Two halves, kept strictly apart:

* **plain parsers** (regex) pull the amounts, names and days. ``120k`` is
  120,000; ``4,500`` is 4500; ``N150,000`` is 150000; ``15k`` is 15000.
* **Jev** classifies each raw line into a kind. It never writes text and never
  supplies a number; if it cannot classify, the line is left ``other`` and the
  owner is asked instead of a figure being guessed.

An unverified incoming alert ("Alert 15k from Emeka") becomes a pending payment
to match against the bank. It never counts as received. A delivery with no
receipt is marked unverified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from miriam_agent.judgment.reading import NoteState, ReadingJudge
from miriam_agent.judgment.service import JudgmentUnavailableError
from miriam_agent.shop.schemas import NoteKind

_AMOUNT_RE = re.compile(
    r"(?<![A-Za-z0-9/])(?:NGN|N|₦)?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*"
    r"([kKmM])?(?![/\w])(?!\.\d)",
)

_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_RELATIVE_DAYS = ("today", "tomorrow", "next week", "next month")
_DUE_RE = re.compile(
    r"\b(" + "|".join(_WEEKDAYS + _RELATIVE_DAYS) + r")\b", re.IGNORECASE
)

_MULTIPLIER = {"k": Decimal(1000), "m": Decimal(1000000)}

# Kinds whose amount is money the shop must act on. A supplier delivery's
# number is a crate count, not a naira value, so its amount is suppressed.
_MONEY_KINDS = {
    NoteKind.CUSTOMER_DEBT,
    NoteKind.PAYMENT_OUT,
    NoteKind.PAYMENT_IN_UNVERIFIED,
}


@dataclass(frozen=True)
class NoteParse:
    """The deterministic read of one raw note line."""

    text: str
    amount_naira: str | None = None
    due: str | None = None
    party: str = ""


@dataclass
class NoteEvaluation:
    """One note after classification and parsing."""

    text: str
    kind: NoteKind
    parse: NoteParse
    needs_amount: bool = False
    unclassified: bool = False
    flags: list[str] = field(default_factory=list)


def _render(value: Decimal) -> str:
    """A plain naira decimal string, never exponent form (1.2E+5)."""
    if value == value.to_integral_value():
        return str(value.quantize(Decimal(1)))
    return str(value)


def parse_amounts(text: str) -> list[str]:
    """Every money-looking amount in ``text`` as a plain naira decimal string."""
    out: list[str] = []
    for match in _AMOUNT_RE.finditer(text or ""):
        raw = match.group(1)
        suffix = match.group(2)
        try:
            value = Decimal(raw.replace(",", ""))
        except Exception:  # noqa: BLE001 - skip an unparseable token
            continue
        if suffix:
            value = value * _MULTIPLIER[suffix.lower()]
        out.append(_render(value))
    return out


def parse_amount(text: str) -> str | None:
    """The first amount in ``text``, or ``None``. Never guesses a value."""
    amounts = parse_amounts(text)
    if not amounts:
        return None
    return _render(Decimal(amounts[0]))


def parse_due(text: str) -> str | None:
    match = _DUE_RE.search(text or "")
    if not match:
        return None
    return match.group(1).lower()


# Anything that is only punctuation, a rule, or space. A dashed line across the
# page arrives as a "note" made of these; nobody wrote it and it means nothing.
_SEPARATOR_ONLY = re.compile(r"[\s\-–—…_=~.·•*+|/\\:;,\"'`()\[\]{}<>“”‘’]+")


def is_blank_note(text: str) -> bool:
    """True when a "note" carries no words at all.

    Empty, or nothing but separators and punctuation. A ruled or dashed line on
    the page reaches the reader as exactly that ("— '' —"), and showing it back
    as an unclassified note makes the owner read a line about nothing.
    """
    return not _SEPARATOR_ONLY.sub("", text or "")


def _clean_party(raw: str) -> str:
    raw = raw.strip().strip(",.;:-")
    raw = re.sub(r"^(and|minus|plus|for|to)\s+", "", raw, flags=re.IGNORECASE)
    return raw.strip()


def parse_party(text: str) -> str:
    """Pull the person or business a note is about, or an empty string."""
    line = text or ""
    patterns = (
        r"^(.+?)\s+owes\b",
        r"\bfrom\s+([A-Za-z][\w' ]*?)(?:\s*,|\s+to\b|$)",
        r"\bpaid\s+([A-Za-z][\w' ]*?)(?:\s+\d|\s*,|$)",
        r"^(.+?)\s+brought\b",
        r"\bbrought\s+by\s+([A-Za-z][\w' ]*?)(?:\s*,|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, line, re.IGNORECASE)
        if match:
            cleaned = _clean_party(match.group(1))
            if cleaned:
                return cleaned
    return ""


def parse_note_line(text: str) -> NoteParse:
    """The plain read of a note line: text, first amount, due day, party."""
    return NoteParse(
        text=(text or "").strip(),
        amount_naira=parse_amount(text),
        due=parse_due(text),
        party=parse_party(text),
    )


def _coerce_kind(value: str) -> NoteKind | None:
    try:
        return NoteKind(value)
    except ValueError:
        return None


async def classify_and_parse(
    line: str,
    *,
    context: str = "",
    judge: ReadingJudge | None = None,
) -> NoteEvaluation:
    """Classify one raw note line with Jev, then parse it deterministically.

    Fail closed: if Jev is unavailable, the note stays ``other`` and is flagged
    for the owner rather than being guessed into a money kind.
    """
    raw = (line or "").strip()
    parse = parse_note_line(raw)
    if not raw:
        return NoteEvaluation(text=raw, kind=NoteKind.OTHER, parse=parse)

    kind = NoteKind.OTHER
    unclassified = False
    judge = judge or ReadingJudge()
    try:
        judgment = await judge.classify_note(NoteState(line=raw, context=context))
        parsed_kind = _coerce_kind(judgment.kind.choice)
        if parsed_kind is None:
            unclassified = True
        else:
            kind = parsed_kind
    except JudgmentUnavailableError:
        unclassified = True

    enriched = parse
    if kind is NoteKind.SUPPLIER_DELIVERY and parse.amount_naira is not None:
        # The number is a quantity, not naira. Keep the words, drop the amount.
        enriched = NoteParse(
            text=parse.text, amount_naira=None, due=parse.due, party=parse.party
        )

    needs_amount = kind in _MONEY_KINDS and enriched.amount_naira is None
    flags: list[str] = []
    if unclassified:
        flags.append("note not understood")
    if needs_amount:
        flags.append("no amount found")

    return NoteEvaluation(
        text=raw,
        kind=kind,
        parse=enriched,
        needs_amount=needs_amount,
        unclassified=unclassified,
        flags=flags,
    )


__all__ = [
    "NoteEvaluation",
    "NoteParse",
    "classify_and_parse",
    "is_blank_note",
    "parse_amount",
    "parse_amounts",
    "parse_due",
    "parse_note_line",
    "parse_party",
]

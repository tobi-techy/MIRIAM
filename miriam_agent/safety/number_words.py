"""Number, spelling, and scaled-figure machinery for grounding.

Extracted from ``miriam_agent.safety.grounding`` as a pure move for the
blob-ratchet split: these helpers turn digits, magnitude suffixes, and
spelled-out numbers into comparable figures, with no labels or claims logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Digits with optional thousands/decimal separators and an optional percent
# sign: "1,500.00", "60%".
NUMBER_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*%?")

# A list ordinal ("1." at the start of a line) is structure, not a figure.
ORDINAL_PREFIX_RE = re.compile(r"(?m)^[ \t]*\d+[.)][ \t]+")

_WORD_RE = re.compile(r"[a-z]+")

# "30k", "₦1.5m". Only a figure when a currency mark sits on one side of it --
# otherwise it is "5m ago" or "the 10k run".
_SCALED_RE = re.compile(
    r"(?:(?P<sym>[$₦£€])\s*)?(?P<num>\d+(?:[.,]\d+)?)\s*(?P<scale>[kKmM])\b"
    r"(?P<tail>\s*[A-Za-z]*)"
)
_SCALE_FACTOR: dict[str, int] = {"k": 1_000, "m": 1_000_000}

_UNITS: dict[str, int] = {
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
}
_TENS: dict[str, int] = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SCALES: dict[str, int] = {
    "hundred": 100,
    "thousand": 1_000,
    "lakh": 100_000,
    "million": 1_000_000,
    "crore": 10_000_000,
    "billion": 1_000_000_000,
}
_NUMBER_WORDS: frozenset[str] = (
    frozenset(_UNITS) | frozenset(_TENS) | frozenset(_SCALES)
)

# The nouns that turn a spelled-out number into a money figure. "K" is here
# because "twenty k" is how people write it out loud.
_CURRENCY_WORDS: frozenset[str] = frozenset(
    {
        "naira",
        "dollar",
        "dollars",
        "pound",
        "pounds",
        "euro",
        "euros",
        "buck",
        "bucks",
        "grand",
        "k",
    }
)

# Nouns that mean money without naming a currency: amounts ("ten thousand
# left") and the places money sits ("ten thousand in your stash"). These only
# count for figures of 100 or more, so "two left" and "one aside" stay prose
# while the incident's phrasing is still caught.
_MAGNITUDE_WORDS: frozenset[str] = frozenset(
    {
        "left",
        "aside",
        "saved",
        "savings",
        "spent",
        "income",
        "balance",
        "rent",
        "owe",
        "owed",
        "debt",
        "budget",
        "stash",
        "wallet",
        "account",
        "spend",
        "salary",
        "cash",
        "bill",
        "fee",
    }
)
_MAGNITUDE_MIN = 100

# Spoken magnitudes: "twenty grand", "twenty k".
_SPOKEN_SCALES: dict[str, int] = {"k": 1_000, "grand": 1_000, "m": 1_000_000}

# Words that may sit between a spelled figure and the money noun it belongs to:
# "ten thousand *in your* stash". Only these, and only two of them, so a run
# cannot reach across a clause and claim a noun that belongs to something else.
_FILLERS: frozenset[str] = frozenset(
    {
        "in",
        "into",
        "on",
        "to",
        "of",
        "from",
        "at",
        "for",
        "a",
        "an",
        "the",
        "your",
        "my",
        "our",
        "their",
        "his",
        "her",
        "is",
        "are",
        "was",
        "were",
    }
)
_FILLER_SPAN = 2


@dataclass(frozen=True)
class Figure:
    """One figure a reply asserts, what it is worth, and where it sits."""

    text: str
    value: str
    kind: str  # "number" | "scaled" | "spelled"
    start: int = -1
    end: int = -1

    def __str__(self) -> str:  # for logs and correction prompts
        return f"{self.text} ({self.value})" if self.kind != "number" else self.text


def normalise_figure(token: str) -> str:
    """Canonical form for comparison: separators and currency marks stripped,
    decimal trailing zeros trimmed, so 1,500.00 == 1500 and "1,500" == "1500".

    Integer trailing zeros are significant: "1500" is never mistaken for "15".
    A literal percent sign means a ratio, so "60%" normalises to "0.6" -- a
    presenter must be able to say "sixty percent" beside a confidence of 0.6
    without being accused of inventing a number.
    """
    percent = token.rstrip().endswith("%")
    clean = re.sub(r"[^0-9.]", "", token)
    if "." not in clean:
        normalised = clean or "0"
    else:
        whole, _, fraction = clean.partition(".")
        fraction = fraction.rstrip("0")
        normalised = f"{whole}.{fraction}" if fraction else whole or "0"
    if not percent:
        return normalised
    try:
        return str(round(float(normalised) / 100, 6))
    except ValueError:
        return normalised


def _word_spans(text: str) -> list[tuple[str, int, int]]:
    return [
        (match.group(0).lower(), match.start(), match.end())
        for match in _WORD_RE.finditer(text or "")
    ]


def _parse_number_words(words: list[str]) -> int | None:
    """Value of a run of number words, or None when the run is not a number.

    Standard English accumulation: "thirty thousand" -> 30000, "two hundred
    fifty" -> 250, "one hundred thousand" -> 100000.
    """
    total = 0
    current = 0
    seen = False
    for word in words:
        if word in _UNITS:
            current += _UNITS[word]
            seen = True
        elif word in _TENS:
            current += _TENS[word]
            seen = True
        elif word in _SCALES:
            scale = _SCALES[word]
            if scale == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * scale
                current = 0
            seen = True
        else:  # pragma: no cover - runs are built from number words only
            return None
    return (total + current) if seen else None


def _spelled_runs(text: str) -> list[tuple[str, int, int, list[str], list[str]]]:
    """Each run of number words: (surface, start, end, words, words_after)."""
    words = _word_spans(text)
    runs: list[tuple[str, int, int, list[str], list[str]]] = []
    index = 0
    while index < len(words):
        if words[index][0] not in _NUMBER_WORDS:
            index += 1
            continue
        end = index
        while end < len(words) and words[end][0] in _NUMBER_WORDS:
            end += 1
        surface = text[words[index][1] : words[end - 1][2]]
        after = [word[0] for word in words[end : end + _FILLER_SPAN + 2]]
        runs.append(
            (
                surface,
                words[index][1],
                words[end - 1][2],
                [w[0] for w in words[index:end]],
                after,
            )
        )
        index = end
    return runs


def _money_noun_after(after: list[str]) -> str:
    """The money noun a spelled figure belongs to, past a couple of fillers."""
    index = 0
    while index < len(after) and index < _FILLER_SPAN and after[index] in _FILLERS:
        index += 1
    return after[index] if index < len(after) else ""


def _scaled_spans(text: str, *, require_money: bool) -> list[Figure]:
    """Figures written with a magnitude suffix: "30k", "₦1.5m"."""
    figures: list[Figure] = []
    for match in _SCALED_RE.finditer(text or ""):
        if match.start() > 0 and text[match.start() - 1] in "0123456789.":
            continue  # tail of a longer number
        tail_word = _WORD_RE.match(match.group("tail").strip() or "")
        tail = tail_word.group(0).lower() if tail_word is not None else ""
        scale = match.group("scale").lower()
        number = float(match.group("num").replace(",", "").rstrip("."))
        value = int(number * _SCALE_FACTOR[scale])
        money_context = (
            bool(match.group("sym"))
            or tail in _CURRENCY_WORDS
            or (tail in _MAGNITUDE_WORDS and value >= _MAGNITUDE_MIN)
        )
        # "k" is money on its own: in a conversation about someone's money,
        # "30k" is thirty thousand naira and nothing else. "m" is not -- "5m"
        # is minutes far more often than it is millions -- so it still needs a
        # currency mark or a money noun beside it.
        if require_money and scale == "m" and not money_context:
            continue
        figures.append(
            Figure(
                text=match.group(0).strip(),
                value=str(value),
                kind="scaled",
                start=match.start(),
                end=match.end(),
            )
        )
    return figures


def _spelled_figures(
    text: str, *, require_money: bool, money_only: bool = True
) -> list[Figure]:
    """Spelled-out figures, with the claim-side rules applied when asked."""
    figures: list[Figure] = []
    for surface, start, end, run_words, after in _spelled_runs(text):
        value = _parse_number_words(run_words)
        if value is None:
            continue
        # "twenty grand" and "twenty k" are magnitudes the words themselves do
        # not carry, so the spoken scale multiplies what was parsed -- unless
        # the run already named its own scale ("one hundred thousand").
        following = after[0] if after else ""
        if following in _SPOKEN_SCALES and not any(w in _SCALES for w in run_words):
            value *= _SPOKEN_SCALES[following]
        if require_money:
            noun = _money_noun_after(after)
            if noun in _CURRENCY_WORDS:
                pass
            elif noun in _MAGNITUDE_WORDS and value >= _MAGNITUDE_MIN:
                pass
            else:
                continue
        figures.append(
            Figure(
                text=surface,
                value=str(value),
                kind="spelled",
                start=start,
                end=end,
            )
        )
    return figures


def figure_claims(reply: str) -> list[Figure]:
    """Every figure a reply asserts.

    Digits always count. Magnitude suffixes ("30k") count only with a currency
    mark beside them. Spelled-out numbers count next to a money word, and small
    ones only next to a currency noun, so ordinary prose ("Two moves first",
    "three days") is never read as a figure.
    """
    if not reply:
        return []
    cleaned = ORDINAL_PREFIX_RE.sub("", reply)
    scaled = _scaled_spans(cleaned, require_money=True)
    claims: list[Figure] = list(scaled)
    for match in NUMBER_TOKEN_RE.finditer(cleaned):
        if any(figure.start <= match.start() < figure.end for figure in scaled):
            continue  # the digits of "30k" are not a second claim
        claims.append(
            Figure(
                text=match.group(0),
                value=normalise_figure(match.group(0)),
                kind="number",
                start=match.start(),
                end=match.end(),
            )
        )
    claims.extend(_spelled_figures(cleaned, require_money=True))
    return claims


def grounded_values(*texts: str) -> set[str]:
    """Every figure the supplied sources contain.

    Generous by design: sources are read for digits, magnitude suffixes, and
    spelled-out numbers *wherever* they appear -- if the user said "four
    thousand" then 4000 is a figure they gave, and a reply that writes it as
    "4,000" must not be refused for it.
    """
    values: set[str] = set()
    for text in texts:
        if not text:
            continue
        cleaned = ORDINAL_PREFIX_RE.sub("", text)
        scaled = _scaled_spans(cleaned, require_money=False)
        values.update(figure.value for figure in scaled)
        for match in NUMBER_TOKEN_RE.finditer(cleaned):
            if any(figure.start <= match.start() < figure.end for figure in scaled):
                continue
            values.add(normalise_figure(match.group(0)))
        values.update(
            figure.value
            for figure in _spelled_figures(
                cleaned, require_money=False, money_only=False
            )
        )
    return values


def ungrounded_figures(reply: str, grounded: str) -> list[Figure]:
    """Figures in ``reply`` with no source in ``grounded``.

    An empty corpus means nothing is grounded, so every figure is a claim the
    turn was never given. Callers that want "no corpus, nothing to check" opt
    out explicitly rather than getting it by accident.
    """
    if not reply:
        return []
    ground = grounded_values(grounded)
    return [claim for claim in figure_claims(reply) if claim.value not in ground]

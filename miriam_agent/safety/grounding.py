"""Deterministic figural grounding: what a reply may claim, and about what.

The research on production LLM systems converges on one shape for money-grade
surfaces -- the model narrates, code decides. A verifier that lives behind a
network call is a verifier that is absent exactly when something is broken, so
every check here is pure string work. They run on every egress, before and
independently of the LLM judge, and they have no failure mode other than "flag".

Four questions, in increasing order of how much they assume:

``ungrounded_figures``   Is this figure traceable to anything the turn was given?
``label_conflicts``      Is it attached to the *right* thing (wallet, period)?
``action_claims``        Does the reply claim an action that did not happen?
``is_stale_block``       Was the thing it came from too old to count?

Two deliberate asymmetries, both learned the hard way:

* Grounding may be generous. A figure counts as grounded if it appears in the
  user's own words, an injected context block, or this turn's tool results --
  including figures the user spelled out ("I take home four thousand"). Being
  generous here costs nothing and prevents refusing a number the user supplied.
* A claim must be unmistakable. Digits are always figures. Spelled-out numbers
  are only figures next to a money word, and small ones only next to a currency
  noun, because Miriam's own voice says "Two moves first" and "three days" --
  flagging those would teach everyone to ignore the guard.

What this does NOT catch, stated here so nobody has to discover it in
production: a number that is sourced but *wrong* (bad upstream data), a number
that is right but stale (`is_stale_block` only bites when a source declares
itself stale), and non-numeric fabrication with no action verb ("your rent went
up", "I found a charge from a merchant you have never used"). Those need the
upstream data to be right, or the LLM judge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

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

# Labels a figure can be attached to, canonicalised so the same label in two
# grammatical moods is not read as two labels: "a month" and "income_monthly"
# are the same claim, while "monthly" and "weekly" are not. "balance",
# "account", and "wallet" are deliberately absent -- they wrap any wallet, so
# treating them as competing labels would flag "your balance is 12,500" against
# a source that says "spend: 12,500".
_LABEL_CANONICAL: dict[str, str] = {
    "spend": "spend",
    "stash": "savings",
    "savings": "savings",
    "month": "month",
    "monthly": "month",
    "week": "week",
    "weekly": "week",
    "day": "day",
    "daily": "day",
    "year": "year",
    "yearly": "year",
    "annual": "year",
    "annually": "year",
}
_LABEL_WINDOW = 90
_LABEL_TIE = 8

# Keys that carry bookkeeping rather than a fact about the money, so two of
# them differing is never a contradiction worth blocking a reply over.
_SCALAR_LABEL_IGNORE: frozenset[str] = frozenset(
    {
        "id",
        "ids",
        "created",
        "updated",
        "timestamp",
        "version",
        "date",
        "limit",
        "offset",
        "page",
        "status",
        "type",
        "code",
        "token",
        "url",
        "at",
    }
)

# Completed state-changing actions. Applied only on a path where nothing
# executes (the answer loop refuses every mutation), which is what makes a
# completed-action claim false rather than merely unverified.
_ACTION_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\bi(?:'ve| have)\s+(?:just\s+|already\s+)?(?:sent|transferred|moved|"
        r"scheduled|locked|invested|withdrawn|funded|topped|deposited|completed|"
        r"processed|executed)\b"
    ),
    re.compile(
        r"(?i)\bi\s+(?:sent|transferred|moved|scheduled|locked|invested|withdrew|"
        r"funded|deposited|completed|processed|executed|topped\s+up)\b"
    ),
    # "I've paid the rent" / "I paid it" -- but never "I paid attention".
    re.compile(
        r"(?i)\bi(?:'ve| have)?\s+paid\s+(?:the|your|his|her|their|it|that|this|"
        r"them|[$₦£€]|\d)"
    ),
    re.compile(r"(?i)\bi(?:'ve| have)\s+set\s+(?:it|that|this|things?)\s+up\b"),
    re.compile(
        r"(?i)\b(?:your|the)\s+(?:bill|payment|transfer|deposit|top-?up|order|"
        r"action|plan)\s+(?:is|has been|have been|was|were|went)\s+(?:paid|sent|"
        r"done|complete|completed|scheduled|set up|made|processed|settled)\b"
    ),
    re.compile(
        r"(?i)\b(?:payment|transfer|deposit|top-?up)\s+(?:sent|complete|"
        r"completed|done|processed|made)\b"
    ),
    re.compile(
        r"(?i)\bit(?:'s| is)\s+(?:done|paid|sent|transferred|sorted|set up|handled)\b"
    ),
    re.compile(r"(?i)\ball\s+(?:done|set)\b"),
    re.compile(r"(?i)\byou(?:'re| are)\s+all\s+set\b"),
    re.compile(
        r"(?i)\b(?:money|funds?|payment|transfer)\s+(?:is|are)\s+on\s+"
        r"(?:its|their|the)\s+way\b"
    ),
)

# A block may declare itself stale; a stale block cannot ground a figure. This
# is explicit on purpose: inferring staleness from a payload's own date fields
# needs a conversation about which of them mean what (an `created_at` is not an
# `as_of`), and guessing would silently refuse valid balances.
STALE_MARKER = "_stale"


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


def _labels_near(text: str, start: int, end: int) -> set[str]:
    window = max(0, start - _LABEL_WINDOW), min(len(text), end + _LABEL_WINDOW)
    words = {word for word, _s, _e in _word_spans(text[window[0] : window[1]])}
    return words


def _label_keys(text: str, start: int, end: int) -> set[str]:
    """Canonical labels attached to a figure: {"month"}, {"spend"}, ..."""
    return {
        _LABEL_CANONICAL[word]
        for word in _labels_near(text, start, end)
        if word in _LABEL_CANONICAL
    }


def _nearest_label_keys(text: str, start: int, end: int) -> set[str]:
    """Canonical labels closest to a span -- the label a figure actually wears.

    A sentence that mentions two wallets must not lend the second one's name to
    the first figure, so only labels within a short distance of the nearest one
    are counted. ``_LABEL_TIE`` keeps a two-word label ("spend wallet") whole.
    """
    candidates: list[tuple[int, str]] = []
    closest: int | None = None
    for word, w_start, w_end in _word_spans(text):
        key = _LABEL_CANONICAL.get(word)
        if key is None:
            continue
        distance = min(abs(w_start - end), abs(start - w_end))
        if distance > _LABEL_WINDOW:
            continue
        candidates.append((distance, key))
        closest = distance if closest is None else min(closest, distance)
    if closest is None:
        return set()
    return {key for distance, key in candidates if distance <= closest + _LABEL_TIE}


def _collect_structured_labels(
    node: Any,
    path: list[str],
    by_value: dict[str, set[str]],
    scalars: dict[str, set[str]],
    paths_by_value: dict[str, set[str]],
    *,
    in_list: bool = False,
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _collect_structured_labels(
                value,
                path + [str(key)],
                by_value,
                scalars,
                paths_by_value,
                in_list=in_list,
            )
        return
    if isinstance(node, list):
        for item in node:
            _collect_structured_labels(
                item, path, by_value, scalars, paths_by_value, in_list=True
            )
        return
    value = normalise_figure(str(node))
    words: set[str] = set()
    for part in path:
        words.update(_WORD_RE.findall(part.lower()))
    keys = {_LABEL_CANONICAL[word] for word in words if word in _LABEL_CANONICAL}
    if keys:
        by_value.setdefault(value, set()).update(keys)
    # Contradictions are checked per *field*, not per word: "income_monthly" and
    # "income_weekly" are different fields that happen to share a word, and
    # treating them as one would block honest answers about either.
    field = "_".join(part.lower() for part in path)
    if field and not in_list and words - _SCALAR_LABEL_IGNORE:
        scalars.setdefault(field, set()).add(value)
        paths_by_value.setdefault(value, set()).add(field)


@dataclass(frozen=True)
class StructuredFacts:
    """What a structured corpus says: which labels a value carries, and which
    labels have more than one scalar value."""

    by_value: dict[str, set[str]]
    scalars: dict[str, set[str]]
    paths_by_value: dict[str, set[str]]

    @property
    def contested(self) -> set[str]:
        """Fields the sources disagree about."""
        return {field for field, values in self.scalars.items() if len(values) > 1}


def structured_facts(corpus: str) -> StructuredFacts:
    by_value: dict[str, set[str]] = {}
    scalars: dict[str, set[str]] = {}
    paths_by_value: dict[str, set[str]] = {}
    for line in (corpus or "").splitlines():
        candidate = line.strip()
        if not candidate.startswith(("{", "[")):
            continue
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        _collect_structured_labels(data, [], by_value, scalars, paths_by_value)
    return StructuredFacts(
        by_value=by_value, scalars=scalars, paths_by_value=paths_by_value
    )


def structured_labels(corpus: str) -> dict[str, set[str]]:
    """value -> labels, read from the JSON blocks in a corpus.

    Proximity is not enough for structured data: in
    ``{"spend": {"balance": 12500}, "stash": {"balance": 400}}`` the nearest
    label to 12500 is "stash", which is exactly the wrong answer. Walking the
    tree gives each value the labels it actually sits under.
    """
    return structured_facts(corpus).by_value


def label_conflicts(reply: str, grounded: str) -> list[Figure]:
    """Figures attached to a competing label.

    Positive evidence only: a figure is flagged when the reply labels it with
    something (a wallet, a period) *and* the source attaches a different label
    from the same family to that same value. A label the source says nothing
    about is not a conflict, which is what keeps "your balance is 12,500"
    acceptable against a row that only says "spend: 12,500".
    """
    if not reply or not grounded:
        return []
    corpus = [
        (normalise_figure(match.group(0)), match.start(), match.end())
        for match in NUMBER_TOKEN_RE.finditer(grounded)
    ]
    structured = structured_labels(grounded)
    conflicts: list[Figure] = []
    for claim in figure_claims(reply):
        if claim.start < 0 or claim.text.endswith("%"):
            continue
        reply_labels = _nearest_label_keys(reply, claim.start, claim.end)
        if not reply_labels:
            continue
        source_labels = set(structured.get(claim.value, set()))
        if not source_labels:
            # Prose sources fall back to proximity, which is all they support.
            for value, start, end in corpus:
                if value == claim.value:
                    source_labels.update(_nearest_label_keys(grounded, start, end))
        if source_labels and not (reply_labels & source_labels):
            conflicts.append(claim)
    return conflicts


def action_claims(reply: str) -> list[str]:
    """Phrases claiming a state-changing action already happened.

    Only meaningful on a path where nothing executes. On the answer loop the
    invariant holds by construction -- every mutation tool is refused -- so a
    completed-action claim there is false rather than merely unverified. The
    orchestrator path *does* execute, and narrates real executions from STATE,
    so this check must not be applied to Voice.
    """
    if not reply:
        return []
    found: list[str] = []
    for pattern in _ACTION_CLAIM_PATTERNS:
        for match in pattern.finditer(reply):
            found.append(match.group(0).strip())
    return found


def is_stale_block(block: Any) -> bool:
    """Whether a context or tool block has declared itself too old to count."""
    return isinstance(block, dict) and bool(block.get(STALE_MARKER))


def is_grounded(reply: str, grounded: str) -> bool:
    return not ungrounded_figures(reply, grounded)


def describe(items: list[Any], limit: int = 5) -> str:
    """A short, log-safe rendering of offending figures or phrases."""
    return ", ".join(str(item) for item in items[:limit])


__all__ = [
    "Figure",
    "NUMBER_TOKEN_RE",
    "ORDINAL_PREFIX_RE",
    "STALE_MARKER",
    "StructuredFacts",
    "action_claims",
    "change_claims",
    "contested_figures",
    "describe",
    "figure_claims",
    "forecast_claims",
    "grounded_values",
    "is_grounded",
    "is_stale_block",
    "label_conflicts",
    "novel_entities",
    "normalise_figure",
    "observation_claims",
    "structured_facts",
    "structured_labels",
    "ungrounded_figures",
]


# ---------------------------------------------------------------------------
# Claims about data the turn does not have
#
# The figure rules above catch a number with no source. These catch the other
# half of the problem: a sentence that asserts something about the user's money
# which no figure was needed to say, and which nothing in the turn supports.
# Each rule is deliberately narrow -- a pattern plus an evidence test -- because
# the cost of a false positive here is Miriam refusing to talk normally.
# ---------------------------------------------------------------------------

# Predictions. Miriam cannot know the future, and a projection is only hers to
# repeat when the plan actually made one.
_FORECAST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bon track to\b"),
    re.compile(r"(?i)\byou(?:'ll| will)\s+(?:have|hit|reach)\b"),
    re.compile(r"(?i)\b(?:projected|forecast|expected)\s+to\b"),
    re.compile(r"(?i)\bby\s+(?:next|the end of)\s+\w+\s+you(?:'ll| will)\b"),
    re.compile(r"(?i)\byou should (?:have|reach|hit)\b"),
    re.compile(r"(?i)\bwill be worth\b"),
    re.compile(r"(?i)\bgoing to (?:have|reach|hit)\b"),
)
_FORECAST_EVIDENCE: frozenset[str] = frozenset(
    {
        "projected",
        "projection",
        "forecast",
        "target",
        "expected",
        "on_track",
        "runway",
        "months_left",
        "goal_target",
        "estimate",
    }
)

# Statements that something changed. A change needs two points in time; without
# them the sentence is a story about the user's money that nobody told her.
_CHANGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(?:went|gone)\s+(?:up|down)\b"),
    re.compile(
        r"(?i)\b(?:increased|decreased|rose|fell|dropped|jumped|climbed|dipped|"
        r"shrunk|grew)\b"
    ),
    re.compile(r"(?i)\b(?:is|are)\s+(?:higher|lower)\s+than\b"),
    re.compile(r"(?i)\b(?:more|less)\s+than\s+(?:last|before|usual|it was)\b"),
    re.compile(r"(?i)\b(?:up|down)\s+(?:from|by)\b"),
    re.compile(r"(?i)\bhas\s+(?:grown|fallen|risen|dropped|changed)\b"),
    re.compile(r"(?i)\bused to\b"),
)
# A comparison needs two points in time. Any of these in a *field name* is that
# evidence; "current" deliberately is not one, because "current_balance" is
# what a snapshot is called whether or not anything changed.
_CHANGE_FIELD_MARKERS: frozenset[str] = frozenset(
    {
        "previous",
        "prior",
        "opening",
        "closing",
        "initial",
        "last",
        "prev",
        "trend",
        "delta",
        "change",
        "compared",
        "vs",
    }
)
_CHANGE_TEXT_MARKERS: frozenset[str] = frozenset(
    {
        "previous",
        "prior",
        "last month",
        "last week",
        "compared",
        "versus",
        "trend",
        "delta",
    }
)

# Assertions that she read something in the account, which can only be true if
# the turn actually contains account data.
_OBSERVATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\bi\s+(?:found|noticed|spotted|see)\b[^.!?\n]{0,40}\b"
        r"(?:charge|transaction|payment|subscription|fee|merchant)\b"
    ),
    re.compile(
        r"(?i)\bi\s+(?:looked|checked)\b[^.!?\n]{0,30}\byour\s+"
        r"(?:account|transactions?|statement)\b"
    ),
    re.compile(r"(?i)\byour\s+(?:transactions?|statement|account)\s+shows?\b"),
    re.compile(r"(?i)\bthere(?:'s| is)\s+a\s+(?:charge|payment|subscription|fee)\b"),
)

# Named things: a merchant, a bank, a biller. Only capitalized runs that are
# unambiguously names are considered -- a run of two or more capitalized words,
# or one capitalized word right after a money/preposition cue -- so ordinary
# capitalized words and sentence openings are never mistaken for a name.
#
# A run never spans a sentence: the character class excludes the full stop and
# the separator is a space or tab, not any whitespace. "on MTN. Want me to top
# it up?" must not read as one entity called "MTN. Want".
_ENTITY_RUN_RE = re.compile(r"\b[A-Z][A-Za-z&'-]*(?:[ \t]+[A-Z][A-Za-z&'-]*)*\b")
_SENTENCE_START_RE = re.compile(r"(?:^|(?<=[.!?\n]))\s*")
_ENTITY_CUES: frozenset[str] = frozenset({"from", "at", "to", "with", "via", "using"})
_ENTITY_ALLOW: frozenset[str] = frozenset(
    {
        "i",
        "miriam",
        "nigeria",
        "lagos",
        "naira",
        "dollar",
        "dollars",
        "usd",
        "ngn",
        "gbp",
        "eur",
        "ok",
        "okay",
        "yes",
        "no",
        "but",
        "and",
        "so",
        "the",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    }
)


def _has_evidence(text: str, markers: frozenset[str]) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in markers)


def forecast_claims(reply: str, grounded: str) -> list[str]:
    """Predictions about the user's money with no projection behind them."""
    if not reply or _has_evidence(grounded, _FORECAST_EVIDENCE):
        return []
    found: list[str] = []
    for pattern in _FORECAST_PATTERNS:
        found.extend(match.group(0).strip() for match in pattern.finditer(reply))
    return found


def change_claims(reply: str, grounded: str) -> list[str]:
    """Claims that something changed, with nothing to compare against.

    Three sources of comparison count as evidence: the corpus holding more than
    one value under the same field (the thing itself), a field named like a
    comparison ("previous_rent", "opening_balance"), or prose that says so.
    """
    if not reply:
        return []
    if _comparison_evidence(grounded):
        return []
    found: list[str] = []
    for pattern in _CHANGE_PATTERNS:
        found.extend(match.group(0).strip() for match in pattern.finditer(reply))
    return found


def _comparison_evidence(grounded: str) -> bool:
    """Whether the turn holds anything a change could be measured against."""
    if not grounded:
        return False
    facts = structured_facts(grounded)
    if facts.contested:
        return True
    for field in facts.scalars:
        if any(marker in field for marker in _CHANGE_FIELD_MARKERS):
            return True
    lowered = grounded.lower()
    return any(marker in lowered for marker in _CHANGE_TEXT_MARKERS)


def observation_claims(reply: str, grounded: str) -> list[str]:
    """Claims to have read the account, in a turn with no account data.

    "No data" is not a judgement call: it is exactly "the corpus contains no
    figure at all". A turn that produced a tool result or carried a balance has
    data; one that did not cannot have been looked at.
    """
    if not reply or grounded_values(grounded):
        return []
    found: list[str] = []
    for pattern in _OBSERVATION_PATTERNS:
        found.extend(match.group(0).strip() for match in pattern.finditer(reply))
    return found


def novel_entities(reply: str, grounded: str) -> list[str]:
    """Named merchants, banks, or billers that appear nowhere in the sources."""
    if not reply:
        return []
    corpus = (grounded or "").lower()
    sentence_starts = {match.end() for match in _SENTENCE_START_RE.finditer(reply)}
    words = _word_spans(reply)
    found: list[str] = []
    for match in _ENTITY_RUN_RE.finditer(reply):
        surface = match.group(0)
        tokens = surface.split()
        if surface.lower() in _ENTITY_ALLOW:
            continue
        if match.start() in sentence_starts:
            # The run is capitalized because it opens a sentence; the words
            # after that one still might be a name ("Your Netflix Premium").
            if len(tokens) == 1:
                continue
            surface = " ".join(tokens[1:])
            tokens = tokens[1:]
        if len(tokens) == 1:
            previous = ""
            for word, _start, end in words:
                if end <= match.start():
                    previous = word
                else:
                    break
            if previous not in _ENTITY_CUES:
                continue
        if surface.lower() in corpus:
            continue
        found.append(surface)
    return found


def contested_figures(reply: str, grounded: str) -> list[Figure]:
    """Figures whose own label is one the sources disagree about.

    The stale-source class: the plan says rent is 350,000 and the ledger says
    380,000. Both are "grounded", and repeating either is how a confidently
    wrong number reaches the user. When sources contradict each other the
    honest answer is that she cannot state the figure.
    """
    if not reply or not grounded:
        return []
    facts = structured_facts(grounded)
    if not facts.contested:
        return []
    flagged: list[Figure] = []
    for claim in figure_claims(reply):
        fields = facts.paths_by_value.get(claim.value, set())
        if fields & facts.contested:
            flagged.append(claim)
    return flagged

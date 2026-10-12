"""Label and structured-fact machinery for grounding.

Extracted from ``miriam_agent.safety.grounding`` as a pure move for the
blob-ratchet split: proximity and structured labels, label conflicts, and the
per-field bookkeeping that decides when sources contradict each other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from miriam_agent.safety.number_words import (
    _WORD_RE,
    NUMBER_TOKEN_RE,
    Figure,
    _word_spans,
    figure_claims,
    normalise_figure,
)

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

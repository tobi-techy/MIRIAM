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

import re
from typing import Any

from miriam_agent.safety.grounding_labels import (
    StructuredFacts,
    label_conflicts,
    structured_facts,
    structured_labels,
)
from miriam_agent.safety.number_words import (
    NUMBER_TOKEN_RE,
    ORDINAL_PREFIX_RE,
    Figure,
    _word_spans,
    figure_claims,
    grounded_values,
    normalise_figure,
    ungrounded_figures,
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
        if re.search(rf"\b{re.escape(surface.lower())}\b", corpus):
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

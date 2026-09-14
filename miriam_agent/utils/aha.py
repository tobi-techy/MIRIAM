"""Aha-moment detection (spec v1.1 §50-§51).

The plan engine engineers the moment; this module detects when it *lands*.
A reply like "damn, that's actually true" or "so that's why i never look at
the numbers" marks the moment the user's read and Miriam's read merged, and is
what the analytics/success metrics and the "bright spot" memory are built on.

Detection is deliberately conservative: it matches full phrases and
word-bounded exclamations, so marketing-flavored "wow! amazing" gets caught
while "wait, one more thing" is never a false positive. Kinds:

  recognition -- they agree the read is their own ("that's actually true"),
  insight     -- a causal door opened ("i never realized the trigger"),
  reframe     -- a standalone lightbulb exclamation ("wait", "damn", "huh"),
  relief      -- the fix turned out easy ("that was way easier than i thought").

Returns the first kind that matches, in that priority order, so a phrase that
could match several ("wait, that's actually it") lands as the strongest signal
rather than a throwaway exclamation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_RECOGNITION_PHRASES = (
    "that's actually true",
    "that is actually true",
    "that's actually it",
    "that is actually it",
    "you're right",
    "you are right",
    "you're exactly right",
    "you are exactly right",
    "now i get it",
    "now i see it",
    "now it makes sense",
    "that makes sense now",
    "that makes so much sense",
    "that makes a lot of sense",
    "it makes sense now",
    "it all makes sense now",
    "i get it now",
    "i see it now",
    "i never thought about it that way",
    "never thought of it that way",
    "never saw it that way",
    "that's it",
    "that is it",
    "that's me",
    "that is me",
    "that's literally me",
    "that is literally me",
    "so that's why",
    "that's why i always",
    "that explains a lot",
    "there it is",
    "there it is.",
    "oh there it is",
)

_INSIGHT_PHRASES = (
    "i never realized",
    "i never realised",
    "i never noticed",
    "i didn't realize",
    "i didn't realise",
    "i didn't know that",
    "didn't know that",
    "interesting how",
    "i see what you mean now",
)

_REFRAME_RE = re.compile(
    r"\b(wait(?: a minute)?|damn|wow|huh|woah|whoa|ooh|hold on(?: a second)?)\b"
    r"[!.,]?\s*$"
)

_RELIEF_PHRASES = (
    "that was easy",
    "that was easier",
    "that was way easier",
    "this actually helped",
    "that actually helped",
    "genuinely helpful",
    "actually helpful",
    "exactly what i needed",
    "this is so helpful",
    "this was so helpful",
    "love this",
    "that helped",
)

_KIND_ORDER = ("recognition", "insight", "reframe", "relief")


@dataclass(frozen=True)
class AhaSignal:
    """A detected aha moment, with the exact phrase that fired it."""

    kind: str
    phrase: str
    text: str


def detect_aha(text: str) -> AhaSignal | None:
    """Return the strongest aha signal in ``text``, or ``None``. The whole
    message is considered (an agreement buried mid-sentence still counts) but
    the check is exact-phrase and word-bounded, so generic enthusiasm or a
    "wait" used as a filler connective never registers."""
    if not text or not text.strip():
        return None
    original = text
    message = text.casefold().strip()
    if any(phrase in message for phrase in _RECOGNITION_PHRASES):
        phrase = next(p for p in _RECOGNITION_PHRASES if p in message)
        return AhaSignal(kind="recognition", phrase=phrase, text=original)
    if any(phrase in message for phrase in _INSIGHT_PHRASES):
        phrase = next(p for p in _INSIGHT_PHRASES if p in message)
        return AhaSignal(kind="insight", phrase=phrase, text=original)
    # Only a *standalone* exclamation is a lightbulb. "wait, but that would
    # mean..." mid-thought is not an aha.
    cleaned = re.sub(r"^[^\w]+|[^\w]+$", "", message)
    match = _REFRAME_RE.search(cleaned)
    if match:
        return AhaSignal(kind="reframe", phrase=match.group(0), text=original)
    if any(phrase in message for phrase in _RELIEF_PHRASES):
        phrase = next(p for p in _RELIEF_PHRASES if p in message)
        return AhaSignal(kind="relief", phrase=phrase, text=original)
    return None


def aha_kind(text: str) -> str:
    """The detected kind as a label, or "" when there is no aha. Convenience
    for metrics labels."""
    signal = detect_aha(text)
    return signal.kind if signal is not None else ""

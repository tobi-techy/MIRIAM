"""Text helpers shared by the agent and onboarding flows.

Two jobs:

1. ``clean_text`` strips the typographic tells that make Miriam read like a
   document instead of a person texting: em/en dashes, curly quotes, single-char
   ellipses, non-breaking spaces. It mirrors the Go backend's ``humanizeText``
   replacement table exactly (``RAIL_BACKEND/.../platform/humanize.go``) so the
   in-app copy and the stored text behave the same as every iMessage bubble.

2. Chatty-turn affordances the executor relays as real native gestures:
   tapback reactions (whitelisted to the six universal iMessage tapbacks) and
   splitting a long reply into short wrapper bubbles.

The whitelist is a hard mirror of the executor's gate: anything not in the six
universal tapbacks is dropped rather than rendered as a sticker or a message.
"""

from __future__ import annotations

import re
from typing import Final

EM_DASH = "\u2014"
EN_DASH = "\u2013"
ELLIPSIS = "\u2026"
NBSP = "\u00a0"
LEFT_SINGLE = "\u2018"
RIGHT_SINGLE = "\u2019"
LEFT_DOUBLE = "\u201c"
RIGHT_DOUBLE = "\u201d"

_DASH_RE = re.compile(rf"\s*[{EM_DASH}{EN_DASH}]\s*")
_CURLY_RE = re.compile(f"[{LEFT_SINGLE}{RIGHT_SINGLE}{LEFT_DOUBLE}{RIGHT_DOUBLE}]")
_ELLIPSIS_RE = re.compile(ELLIPSIS)

_STRAIGHT_SINGLE = "'"
_STRAIGHT_DOUBLE = '"'


def clean_text(text: str) -> str:
    """Replace typographic tells with their plain-text form, mirroring the Go
    backend's humanizer so every surface reads the same. Safe to run more than
    once and on already-clean text (a no-op for ordinary prose)."""
    if not text:
        return text
    out = _DASH_RE.sub(", ", text)
    out = _ELLIPSIS_RE.sub("...", out)
    out = _CURLY_RE.sub(
        lambda m: (
            _STRAIGHT_SINGLE
            if m.group(0) in (LEFT_SINGLE, RIGHT_SINGLE)
            else _STRAIGHT_DOUBLE
        ),
        out,
    )
    out = out.replace(NBSP, " ")
    for bad, good in (
        (",,", ","),
        (", .", "."),
        (", ?", "?"),
        (", !", "!"),
        (": ,", ":"),
        (", ,", ","),
    ):
        out = out.replace(bad, good)
    return out.rstrip(" ")


# The six universal emoji iMessage converts to native tapbacks (love, like,
# dislike, laugh, emphasize, question). Anything else would surface as a sticker
# or a plain message, so it is never emitted. Mirrors the executor whitelist.
REACTION_WHITELIST: Final[frozenset[str]] = frozenset(
    {"❤️", "👍", "👎", "😂", "‼️", "❓"}
)

# How many extra bubbles a chatty turn may carry beyond the main reply. The Go
# executor re-validates this cap before anything is sent.
MAX_EXTRA_MESSAGES: Final[int] = 2

_MIN_CHATTY_LENGTH = 140
_MIN_CHATTY_SENTENCES = 2
_MAX_BUBBLE_LENGTH = 120


def valid_reaction(emoji: str) -> bool:
    """True when the emoji is on the tapback whitelist. Whitespace-stripped,
    mirroring the executor gate."""
    return emoji.strip() in REACTION_WHITELIST


def lift_reaction(text: str) -> str:
    """Lift the first whitelisted tapback literally present in the reply. Never
    invents a reaction: if Miriam wrote one, it rides along; otherwise none is
    sent and the executor stays silent on the reaction channel."""
    if not text:
        return ""
    for emoji in REACTION_WHITELIST:
        if emoji in text:
            return emoji
    return ""


_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END_RE.split(text.strip()) if s.strip()]


def chatty_bubbles(text: str) -> list[str]:
    """Split a long reply into short, text-message-sized bubbles.

    Only a "gold" reply earns splitting: long enough to feel like a wall of text
    AND more than one sentence. The first bubble becomes the main message and the
    rest (up to two) ride as wrapper bubbles. Short replies stay one message --
    splitting quick answers would make Miriam feel spammy. Returns an empty list
    when the reply should stay whole.
    """
    t = text or ""
    clean = clean_text(t)
    if len(clean) < _MIN_CHATTY_LENGTH:
        return []
    sentences = _sentences(clean)
    if len(sentences) < _MIN_CHATTY_SENTENCES:
        return []
    bubbles: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= _MAX_BUBBLE_LENGTH:
            current = candidate
            continue
        if current:
            bubbles.append(current)
            current = sentence
        else:
            bubbles.append(sentence)
    if current:
        bubbles.append(current)
    return bubbles[: MAX_EXTRA_MESSAGES + 1]


def bubble_sets(text: str) -> tuple[str, list[str]]:
    """Return ``(main, extras)`` for a reply. Long replies split into short
    bubbles (extras capped at two); everything else stays one message."""
    bubbles = chatty_bubbles(text)
    if not bubbles:
        return (text or "").strip(), []
    return bubbles[0], bubbles[1:MAX_EXTRA_MESSAGES]

"""Voice enforcement for anything Miriam says about money.

The product rule is plain: no em dashes. This module is the net that catches
them, so a dash typed into a docstring, a template or an LLM reply cannot reach
a user even if nobody notices it in review.

It is deliberately narrower than ``miriam_agent.utils.text.clean_text`` (which
mirrors the Go humanizer and rewrites dashes to ", " for iMessage bubbles). Here
the goal is only to make our own money copy read as typed speech: dashes become
hyphens, quotes go straight, ellipses lose their single glyph. Safe to run twice.
"""

from __future__ import annotations

import re

EM_DASH = "\u2014"
EN_DASH = "\u2013"
ELLIPSIS = "\u2026"
NBSP = "\u00a0"
LEFT_SINGLE = "\u2018"
RIGHT_SINGLE = "\u2019"
LEFT_DOUBLE = "\u201c"
RIGHT_DOUBLE = "\u201d"

# A dash used as punctuation becomes a plain hyphen, spaces preserved so
# "a - b" reads the same as the original "a — b".
_DASH_RE = re.compile(rf"[{EM_DASH}{EN_DASH}]")
_CURLY_RE = re.compile(f"[{LEFT_SINGLE}{RIGHT_SINGLE}{LEFT_DOUBLE}{RIGHT_DOUBLE}]")
_ELLIPSIS_RE = re.compile(ELLIPSIS)
_SPACES_RE = re.compile(r"[ \t]{2,}")


def scrub_voice(text: str) -> str:
    """Replace typographic tells with the plain-text form a person would type."""
    if not text:
        return text
    out = _DASH_RE.sub("-", text)
    out = _ELLIPSIS_RE.sub("...", out)
    out = _CURLY_RE.sub(
        lambda m: ("'" if m.group(0) in (LEFT_SINGLE, RIGHT_SINGLE) else '"'),
        out,
    )
    out = out.replace(NBSP, " ")
    return _SPACES_RE.sub(" ", out)


def has_em_dash(text: str) -> bool:
    """Whether text carries an em or en dash. Used by the voice tests."""
    return bool(text) and bool(_DASH_RE.search(text))


def scrub_lines(lines: list[str]) -> list[str]:
    """Scrub a rendered block, line by line."""
    return [scrub_voice(line) for line in lines]

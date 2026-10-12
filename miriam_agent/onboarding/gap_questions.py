"""Poll, question, and reply plumbing for onboarding.

Extracted from ``miriam_agent.onboarding.service`` as a pure move for the
blob-ratchet split: the poll/answer contracts, the deterministic fallback
vocabulary, short-answer and read-back detection, and the small text/poll
helpers the fallback path shares.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Deterministic fallback vocabulary when the LLM is unavailable.
_ABANDON = {
    "skip",
    "skip it",
    "stop",
    "stop this",
    "never mind",
    "forget it",
    "not now",
    "later",
    "quit",
    "i'm done",
    "im done",
}
_YES_PHRASES = {
    "yes",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "go",
    "go for it",
    "set it up",
    "do it",
    "agree",
    "looks good",
    "sounds good",
    "autopilot",
}
_NO_PHRASES = {"no", "nope", "nah", "not now", "later", "skip it", "no thanks"}
_ADJUST_HINTS = ("adjust", "change", "tweak", "edit", "rework", "instead")
# A short yes on the plan. "Okay" and "yeah would love to" are consent.
# A longer sentence ("okay, but change the rent") is not.
_SHORT_YES = re.compile(
    r"^(?:(?:"
    r"yeah would love to|i would love to|let's do it|lets do it|"
    r"ok|okay|k|yeah|yep|yup|yes|sure|alright|all right|"
    r"thanks|thank you|no wahala"
    r")[\s!.]*){1,4}$",
    re.IGNORECASE,
)


def _is_short_yes(text: str) -> bool:
    return bool(_SHORT_YES.match((text or "").strip()))


_READBACK_YES = re.compile(
    r"^(?:yes|yeah|yep|yup|ok|okay|sure|correct|right)\b",
    re.IGNORECASE,
)
_READBACK_AMOUNT = re.compile(r"(?:\$|₦|£|€|\d)")


def _confirms_readback(text: str) -> bool:
    """A yes to the fact read-back. A new amount or a change request is not a yes."""
    raw = (text or "").strip()
    if not raw:
        return False
    if any(hint in raw.casefold() for hint in _ADJUST_HINTS):
        return False
    if _READBACK_AMOUNT.search(raw):
        return False
    if _is_short_yes(raw):
        return True
    return bool(_READBACK_YES.match(raw))


# Phrases that restart an already-finished/abandoned interview. Deliberately
# narrow: post-completion casual chit-chat ("let's go", "try again") must never
# wipe a finished interview, so only unambiguous redo/revision phrasings count.
_REENTER = re.compile(
    r"\b(redo|restart|start\s+over|start\s+again|from\s+scratch|"
    r"revisit\s+(my\s+)?plan|rework\s+(my\s+)?plan|change\s+(my\s+)?plan)\b",
    re.IGNORECASE,
)

# Explicit name introductions are safe to honor mid-interview ("call me Tobi");
# a bare "Tobi" is already a legitimate answer, so we never guess names from
# free text without one of these markers.
_VOLUNTEERED_NAME = re.compile(
    r"(?:\bmy name\s+(?:is|'s)\b|\bcall me\b|\bpeople call me\b|\bi am\b|\bi'm\b)",
    re.IGNORECASE,
)


@dataclass
class PollSpec:
    title: str
    options: list[str]


STATEMENT_POLL = PollSpec(
    title="Send me a bank statement for a real deep dive?",
    options=["Yes, send it now", "Skip for now"],
)
CONSENT_POLL = PollSpec(
    title="Lock this in and put the invest slice into Apple, Nvidia, and Tesla?",
    options=["Yes, set it up", "Let's adjust it first", "Not now"],
)


@dataclass
class OnboardingTurn:
    """What the handler should do with this turn.

    ``took_over`` True means the onboarding flow handled it: return
    ``response`` (persist it as the assistant message) and, when ``poll`` is
    set, surface the poll. False means Miriam's normal agent should run.
    """

    took_over: bool = False
    response: str = ""
    poll: dict[str, Any] | None = None
    conversation_id: str = field(default="")
    stage: str = ""
    completed: bool = False
    automated: bool = False
    name: str = ""
    # Chatty-turn affordances relayed to the Go executor as native gestures:
    # short wrapper bubbles behind the main reply, one tapback reaction on the
    # user's message, and an optional rich-link share.
    messages: list[str] = field(default_factory=list)
    reaction: str = ""
    share: dict[str, Any] | None = None

    def to_payload(self, conversation_id: str) -> dict[str, Any]:
        # No `requires_confirmation` and no `cards`: that approval-card protocol
        # is deleted, and onboarding never moved money anyway. A money turn is a
        # different response shape entirely (see api/chat._serialize_money_result).
        return {
            "response": self.response,
            "conversation_id": self.conversation_id or conversation_id,
            "poll": self.poll,
            "onboarding": {
                "stage": self.stage,
                "completed": self.completed,
                "automated": self.automated,
            },
            "name": self.name or "",
            "messages": list(self.messages),
            "reaction": self.reaction,
            "share": self.share,
        }


_PLAN_CTA_OPTIONS = {"build my savings plan", "how much can i save"}


def _taps_for_reply(reply: str, taps: list[str]) -> list[str]:
    """Drop plan-button taps when the question is asking for a number."""
    cleaned = [item.strip() for item in taps if item and str(item).strip()]
    if not cleaned:
        return []
    cta_only = all(
        item.casefold().rstrip(" ?") in _PLAN_CTA_OPTIONS for item in cleaned
    )
    if not cta_only:
        return cleaned
    folded = (reply or "").casefold()
    if any(phrase in folded for phrase in ("plan", "lock", "set this up", "set it up")):
        return cleaned
    return []


def _same_poll(state: Any, title: str, taps: list[str]) -> bool:
    prev_title = str(getattr(state, "last_poll_title", "") or "").strip()
    prev = [str(item) for item in (getattr(state, "last_poll_options", None) or [])]
    return prev_title == title.strip() and prev == list(taps)


def _match_option(text: str, options: list[str] | tuple[str, ...]) -> str | None:
    """Match user text (a poll vote or typed answer) to an option."""
    c = text.strip().casefold()
    if not c:
        return None
    for o in options:
        if c == o.casefold():
            return o
    if len(c) >= 3:
        for o in options:
            oc = o.casefold()
            if c in oc:
                return o
    return None


def _lower(text: str) -> str:
    return text.strip().casefold()

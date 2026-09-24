"""Layer 1 - HANDS. Save-rule (automation) updates.

Deterministic code only. No LLM, no JEV.

"park 15% of this inflow" / "change the save rule to 5000" parses here into
a ``save_rule`` action. Like every money movement it needs a confirm tap: the
challenge binds the percentage/amount in its meta, and the settle step
resolves the automation (listed from Go at settle time, never guessed from
chat) and calls ``update_automation``.

Lock/unlock live elsewhere on purpose: they are internal, reversible sleeve
moves and keep the existing chat confirm, never a Face ID card.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from miriam_agent.hands.state import ProposedAction

# Words that name the save rule. Tight on purpose: "stash" alone must not
# trigger ("move 5k to stash" is an internal_move), and bare "save" must not
# ("save 5k" with no rule word is not a rule change).
_SAVE_RULE_WORDS = frozenset({"park", "sweep", "autosave", "auto-save"})
_SAVE_RULE_PHRASES = ("save rule", "savings rule", "auto save", "save sweep")

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _mentions_save_rule(lowered: str) -> bool:
    words = set(re.findall(r"[a-z-]+", lowered))
    if words & _SAVE_RULE_WORDS:
        return True
    return any(phrase in lowered for phrase in _SAVE_RULE_PHRASES)


def parse_save_rule_utterance(text: str) -> ProposedAction | None:
    """Turn a save-rule sentence into a save_rule action, with regex.

    A ``%`` sign binds a percentage ("park 15%"); otherwise the number is a
    flat amount per inflow ("park 5000"). Returns None when the sentence does
    not name the rule or carries no number — Judgment then asks.
    """
    from miriam_agent.hands.transfer import parse_amount

    lowered = (text or "").casefold()
    if not lowered.strip() or not _mentions_save_rule(lowered):
        return None
    pct = _PERCENT_RE.search(lowered)
    if pct is not None:
        return ProposedAction(
            type="save_rule",
            amount=Decimal(pct.group(1)),
            sleeve="savings",
            raw=text,
            source="user",
        )
    amount = parse_amount(lowered)
    if amount is None:
        return None
    return ProposedAction(
        type="save_rule",
        amount=amount,
        sleeve="savings",
        raw=text,
        source="user",
    )


def save_rule_binding(raw: str) -> tuple[str, str] | None:
    """The challenge binding for a save_rule action: ("percentage", N) or
    ("amount", N). Re-derived from the user's own words, never from a card.
    """
    from miriam_agent.hands.transfer import parse_amount

    lowered = (raw or "").casefold()
    pct = _PERCENT_RE.search(lowered)
    if pct is not None:
        return ("percentage", pct.group(1))
    amount = parse_amount(lowered)
    if amount is None:
        return None
    return ("amount", str(amount))


def select_save_automation(
    automations: list[dict[str, Any]], *, automation_id: str = ""
) -> dict[str, Any] | None:
    """Pick the automation a save_rule settle must update. Pure function.

    An explicit id wins. Otherwise the single automation wins (nothing to
    confuse it with), else the first whose kind/name/action reads as a
    save/sweep rule. None means fail closed: the receipt says NO_SAVE_RULE
    instead of updating whatever happened to be listed first.
    """
    if automation_id:
        for auto in automations:
            if str(auto.get("id") or auto.get("automation_id") or "") == automation_id:
                return auto
        return None
    if len(automations) == 1:
        return automations[0]
    for auto in automations:
        haystack = " ".join(
            str(auto.get(key) or "")
            for key in ("type", "name", "trigger_type", "action_type", "description")
        ).casefold()
        if any(
            word in haystack for word in ("sweep", "save", "stash", "inflow", "park")
        ):
            return auto
    return None


__all__ = [
    "parse_save_rule_utterance",
    "save_rule_binding",
    "select_save_automation",
]

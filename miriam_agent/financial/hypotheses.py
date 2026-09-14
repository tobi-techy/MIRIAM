"""Ranked hypothesis engine for the money conversation (spec v1.1 §12).

Miriam works from *hypotheses*, not vibes. Each turn she holds a ranked read
on the structural cause behind what the user says, and her next question is
chosen to eliminate several hypotheses at once -- not to fish for feelings.

This module is the deterministic arm of that read. It sniffs the joined text
of everything we know (the money moment, the goal, the learned facts, the
current message) against a fixed catalogue of structural causes and returns
them ranked, with the exact snippets that drove each score. The LLM steers
against these in the conductor prompt, so its top hypothesis is auditably
grounded and it can never invent a reason the user never gave.

The catalogue mirrors the spec's leak categories (overspending, unexpected
expenses, helping people, not knowing where the money went) plus the deeper
structural causes the interview is there to distinguish: irregular income,
debt, fixed-cost pressure, lifestyle inflation, missing allocation, income
that genuinely doesn't cover the base, and the money dying before payday.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_MAX_EVIDENCE = 48
_BASE_CONFIDENCE = 0.2
_MAX_CONFIDENCE = 0.95
_MIN_MATCHES = 1


@dataclass(frozen=True)
class Hypothesis:
    """One structural cause we can sniff for, with its probe phrasing."""

    code: str
    label: str
    probe: str
    indicators: tuple[tuple[str, float], ...]

    @property
    def probe_or_label(self) -> str:
        return self.probe or self.label


# The four concrete leak categories spec §5 / §53 wants offered in one
# high-information probe. Ordered exactly as a natural choice reads.
PROBE_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("spending too much", "overspending"),
    ("unexpected expenses", "unexpected_expenses"),
    ("helping other people", "family_obligations"),
    ("not really knowing where the money went", "lack_of_visibility"),
)

_CATALOG: tuple[Hypothesis, ...] = (
    Hypothesis(
        code="lack_of_visibility",
        label="not really knowing where the money went",
        probe="not really knowing where the money went",
        indicators=(
            (r"\bdon'?t (?:track|check|look|know)", 0.3),
            (r"(?:never|not) (?:track|check|look)", 0.3),
            (r"(?:no idea|don'?t know)\s+where", 0.35),
            (r"where (?:does|did|do) (?:the money|it|everything)", 0.35),
            (r"\bdisappears?\b|\bvanishes?\b|\bgoes (?:missing|elsewhere)\b", 0.4),
            (r"slips? (?:through|away)|\bget(?:s|ting)?\s+(?:away|ahead of me)\b", 0.4),
            (r"\bin check\b|\bunder control\b|\bout of control\b", 0.4),
            (r"avoid(?:ing)? (?:the )?numbers|don'?t want to look", 0.3),
            (r"lose track|lost track", 0.4),
        ),
    ),
    Hypothesis(
        code="overspending",
        label="spending too much",
        probe="spending too much",
        indicators=(
            (r"spend(?:ing|s)? (?:too|way too) much", 0.45),
            (r"overspend(?:ing|s)?", 0.45),
            (
                r"too much (?:on|for) (?:random|stuff|things|clothes|food|online|"
                r"shopping)",
                0.3,
            ),
            (r"stress (?:buy|spend|shop)", 0.35),
            (r"(?:shopping|buying) (?:addiction|problem)", 0.35),
        ),
    ),
    Hypothesis(
        code="unexpected_expenses",
        label="unexpected expenses",
        probe="unexpected expenses",
        indicators=(
            (r"\bunexpected\b|\bunplanned\b|\bsurprise\b|\bsudden\b", 0.3),
            (r"\bemergencies?\b", 0.3),
            (r"(?:always|keeps?|keep) something (?:coming up|comes up)", 0.4),
            (r"things (?:keep |just )?(?:coming|popping) up", 0.4),
            (r"out of the blue", 0.35),
            (r"life (?:gets|is) expensive", 0.2),
        ),
    ),
    Hypothesis(
        code="family_obligations",
        label="helping other people",
        probe="helping other people",
        indicators=(
            (
                r"\b(family|mother|mum|mom|father|dad|parents|siblings?|brothers?|"
                r"sisters?|children|kids|dependents?)\b",
                0.25,
            ),
            (
                r"(?:support|send(?:ing)?|help(?:ing)?|taking care of|look after) "
                r"(?:my )?(?:family|mother|mum|mom|father|dad|parents|home|siblings?|"
                r"brothers?|sisters?)",
                0.45,
            ),
            (
                r"everyone needs|people lean on|help(?:ing)? people|give(?:ing)? to"
                r"|everyone (?:asks|comes) to me",
                0.4,
            ),
        ),
    ),
    Hypothesis(
        code="irregular_income",
        label="income arrives unevenly",
        probe="",
        indicators=(
            (
                r"\b(unpredict|irregular|varies|volatile|seasonal|inconsistent|"
                r"lumpy)\b",
                0.4,
            ),
            (r"not steady|up and down|all over the place|goes up and down", 0.4),
            (r"\b(freelance|commission|contract|gig)\b", 0.3),
            (r"comes? (?:in )?(?:late|in lumps|unevenly)", 0.4),
        ),
    ),
    Hypothesis(
        code="high_fixed_expenses",
        label="fixed costs eat the month",
        probe="",
        indicators=(
            (r"\b(rent|mortgage|school fees|tuition)\b", 0.25),
            (
                r"(?:most|half|majority|everything) of (?:my )?(?:income|salary|money|"
                r"pay)",
                0.35,
            ),
            (r"fixed (?:costs|expenses|bills|overheads)", 0.35),
            (r"(?:bills|expenses) eat", 0.3),
        ),
    ),
    Hypothesis(
        code="lifestyle_inflation",
        label="every raise becomes spending",
        probe="",
        indicators=(
            (r"lifestyle creep", 0.5),
            (r"keep(?:ing)? up with", 0.4),
            (
                r"(?:got|get|received|walked into) (?:a |my )?(?:raise|promotion|"
                r"bonus)",
                0.4,
            ),
            (r"earn(?:ed|s)? more|income went up|the more i earn|as i earn", 0.4),
            (r"new salary|bigger (?:salary|paycheck|pay)", 0.3),
        ),
    ),
    Hypothesis(
        code="impulse_spending",
        label="impulse purchases",
        probe="",
        indicators=(
            (r"impulse (?:buy|spend|purchase)", 0.5),
            (r"buy(?:ing)? too much (?:stuff|things|random)", 0.4),
            (r"can'?t (?:help|resist) buying", 0.4),
            (r"buy(?:ing)? (?:on|in) a whim|spur of the moment", 0.4),
        ),
    ),
    Hypothesis(
        code="debt",
        label="debt is eating the margin",
        probe="",
        indicators=(
            (r"\b(debts?|loans?|credit card|overdraft|mortgage)\b", 0.4),
            (r"\b(owe|owing|repaying?|borrowing?)\b", 0.4),
            (r"minimum (?:payments?|balance)|interest (?:is|keeps)", 0.35),
        ),
    ),
    Hypothesis(
        code="lack_of_allocation_system",
        label="no default split",
        probe="",
        indicators=(
            (
                r"(?:budget|budgeting|plan) (?:never|doesn'?t|won'?t|does not) "
                r"(?:stick|work|hold|last)",
                0.45,
            ),
            (r"(?:can'?t|struggle to|keep failing|hard to) budget", 0.4),
            (r"no (?:system|structure|method)", 0.35),
            (
                r"(?:everything|all) (?:is|sits|goes) (?:in|to) one "
                r"(?:account|place)",
                0.35,
            ),
            (r"\b(?:start|starting) over\b|\bback to zero\b|every month again", 0.35),
            (r"manual|remember(?:ing)? to save|forget(?:ting)? to save", 0.3),
        ),
    ),
    Hypothesis(
        code="broke_before_payday",
        label="the money dies before the month does",
        probe="",
        indicators=(
            (r"runs? out (?:of (?:money|cash|everything))?|ran out", 0.4),
            (r"\bbroke\b", 0.25),
            (r"paycheck to paycheck", 0.5),
            (r"(?:before|by) (?:the )?(?:end of the month|payday)", 0.35),
            (r"money (?:never|doesn'?t) (?:last|gets) (?:the|to)", 0.4),
            (r"(?:nothing|zero|empty) (?:left|by)", 0.4),
            (r"month (?:ends|runs out) before", 0.45),
        ),
    ),
    Hypothesis(
        code="insufficient_income",
        label="income doesn't cover the base",
        probe="",
        indicators=(
            (r"\bunderpaid\b|salary too (?:small|low)|pay is (?:too )?low", 0.35),
            (r"don'?t earn enough|not enough (?:income|money|coming in)", 0.45),
            (
                r"can'?t (?:even )?(?:cover|afford|pay) (?:the )?(?:basics|bills|rent|"
                r"everything)",
                0.4,
            ),
        ),
    ),
)


@dataclass(frozen=True)
class HypothesisRank:
    """One scored hypothesis: the cause, how confident we are, and the exact
    snippets of user text that support it."""

    code: str
    label: str
    probe: str
    confidence: float
    evidence: tuple[str, ...]


def _evidence_from(match: re.Match[str]) -> str:
    snippet = (match.group(0) or "").strip()
    if len(snippet) > _MAX_EVIDENCE:
        snippet = snippet[:_MAX_EVIDENCE] + "\u2026"
    return snippet


def rank_hypotheses(text: str) -> list[HypothesisRank]:
    """Score every hypothesis against the joined ``text`` (already lowercased
    by the caller or not -- patterns are case-insensitive where it matters) and
    return them from most to least confident. Deterministic: ties break on the
    catalogue order."""
    t = (text or "").casefold()
    scored: list[HypothesisRank] = []
    for hypothesis in _CATALOG:
        weights: list[float] = []
        evidence: list[str] = []
        for pattern, weight in hypothesis.indicators:
            match = re.search(pattern, t)
            if match is None:
                continue
            weights.append(weight)
            snippet = _evidence_from(match)
            if snippet not in evidence:
                evidence.append(snippet)
        if len(weights) < _MIN_MATCHES:
            continue
        confidence = min(_MAX_CONFIDENCE, _BASE_CONFIDENCE + sum(weights))
        scored.append(
            HypothesisRank(
                code=hypothesis.code,
                label=hypothesis.label,
                probe=hypothesis.probe,
                confidence=round(confidence, 2),
                evidence=tuple(evidence),
            )
        )
    scored.sort(key=lambda h: (-h.confidence, _code_order(h.code)))
    return scored


def _code_order(code: str) -> int:
    for i, hypothesis in enumerate(_CATALOG):
        if hypothesis.code == code:
            return i
    return len(_CATALOG)


def top_hypothesis(text: str) -> HypothesisRank | None:
    ranked = rank_hypotheses(text)
    return ranked[0] if ranked else None


def top_hypotheses(text: str, limit: int = 4) -> list[HypothesisRank]:
    return rank_hypotheses(text)[:limit]


_LEAK_CODES = {probe_code for _, probe_code in PROBE_CATEGORIES}
_LEAK_OPEN_CONFIDENCE = 0.65
_LEAK_DONE_THRESHOLD = 2
# The "leak/control" theme in their own words: the money getting away even
# though they don't know where it goes.
_LEAK_THEME_RE = re.compile(
    r"\bcontrol\b|disappears?|\bvanishes?|\bget(?:ting)?s? away|"
    r"\bkeep(?:s|ing)?\b.*\bin check\b|goes (?:sideways|elsewhere)|\bleak"
)
_LEAK_THEME_CODE = "lack_of_visibility"


def leak_probe_open(text: str) -> bool:
    """spec §5 / §53: when the leak/control theme is present but no single
    leak channel has clearly resolved, Miriam should offer the concrete
    categories in one high-information question ("is it usually spending too
    much, unexpected expenses, helping other people, or not really knowing
    where the money went?") instead of fishing for feelings. ``False`` when
    the theme is absent or the user has already named the channel."""
    t = (text or "").casefold()
    ranked = rank_hypotheses(text)
    theme_present = any(h.code == _LEAK_THEME_CODE for h in ranked)
    if not theme_present and not _LEAK_THEME_RE.search(t):
        return False
    resolved = {
        h.code
        for h in ranked
        if h.code in _LEAK_CODES and h.confidence > _LEAK_OPEN_CONFIDENCE
    }
    return len(resolved) < _LEAK_DONE_THRESHOLD


def probe_categories_open(text: str) -> list[str]:
    """The leak-category choices worth offering right now. When a leak channel
    has already resolved (its hypothesis is confident), asking about it again
    is a waste of a question, so it is dropped."""
    ranked = rank_hypotheses(text)
    confident = {
        h.code
        for h in ranked
        if h.code in _LEAK_CODES and h.confidence > _LEAK_OPEN_CONFIDENCE
    }
    return [label for label, code in PROBE_CATEGORIES if code not in confident]


def leaked_text(text: str) -> str:
    """A compact, deterministic block for the conductor's context: the ranked
    hypotheses with confidence and one supporting snippet each. Empty when
    there is nothing to work with."""
    ranked = top_hypotheses(text)
    if not ranked:
        return ""
    lines: list[str] = []
    for hypothesis in ranked:
        snippet = hypothesis.evidence[0]
        lines.append(
            f"- {hypothesis.code} ({hypothesis.confidence:.2f}) " f'["{snippet}"]'
        )
    return "\n".join(lines)


def json_snapshot(text: str) -> dict[str, Any]:
    """Structured view for traces/analytics: the top hypothesis and the leak
    channels still in play."""
    ranked = rank_hypotheses(text)
    top = ranked[0] if ranked else None
    return {
        "top_hypothesis": top.code if top else "",
        "top_confidence": top.confidence if top else 0.0,
        "leak_probe_open": leak_probe_open(text),
        "open_categories": probe_categories_open(text),
    }

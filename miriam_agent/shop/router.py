"""Read routing: pick a tier before the read, decide escalation after it.

Jev is the router. Fail-closed means every uncertainty escalates: a Jev failure
starts on the big model, and cannot lower an escalation. The deterministic
heuristic below is only a fallback for the doc kind when Jev is unavailable, so
Go's own classifier stays the authority and a receipt never becomes a stock
book by accident.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from miriam_agent.config.settings import get_settings
from miriam_agent.judgment.reading import (
    QualityState,
    ReadingJudge,
    TierState,
)
from miriam_agent.judgment.service import JudgmentUnavailableError
from miriam_agent.shop.schemas import DocType

logger = logging.getLogger(__name__)

TIER_SMALL = "small"
TIER_BIG = "big"

# More than this many rows failing a stock-left check is enough on its own to
# re-read on the big model.
ROWS_FAILING_ESCALATE = 1
MISALIGNMENT_ESCALATE = 0.5

# Go classifies first and only sends these three kinds here, so its hint is the
# routing decision of record. Jev still picks the tier, but it may not route a
# document out of the feature: on a phone photo of an invoice (no native text to
# read) it answered "other" with high confidence and the owner got silence.
_SELF_READ_KINDS = (
    DocType.STOCK_BOOK.value,
    DocType.INVOICE.value,
    DocType.DELIVERY_NOTE.value,
)

_KIND_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (DocType.STOCK_BOOK.value, ("opening", "supplied", "sold", "left", "stock")),
    (DocType.DELIVERY_NOTE.value, ("delivery note", "waybill", "dn-", "delivered")),
    (DocType.INVOICE.value, ("invoice", "bill to", "subtotal", "payment terms")),
    (DocType.BANK_STATEMENT.value, ("opening balance", "closing balance", "statement")),
    (DocType.RECEIPT.value, ("receipt", "cashier", "change due")),
)


def guess_doc_kind(native_text: str, caption: str = "") -> str:
    """A deterministic keyword guess, used only when Jev cannot classify."""
    haystack = f"{caption}\n{native_text}".lower()
    best = DocType.OTHER.value
    best_hits = 0
    for kind, keywords in _KIND_KEYWORDS:
        hits = sum(1 for kw in keywords if kw in haystack)
        if hits > best_hits:
            best, best_hits = kind, hits
    return best


@dataclass(frozen=True)
class TierDecision:
    tier: str
    doc_kind: str
    difficulty: str = ""
    confidence: float = 0.0
    reason: str = ""
    degraded: bool = False
    probabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EscalationDecision:
    escalate: bool
    quality: int | None = None
    misalignment: float = 0.0
    reason: str = ""
    degraded: bool = False


async def decide_starting_tier(
    facts: Any,
    *,
    caption: str = "",
    native_text: str = "",
    doc_hint: str = "",
    judge: ReadingJudge | None = None,
    router_enabled: bool | None = None,
    min_confidence: float | None = None,
) -> TierDecision:
    """Choose the starting tier. Hard, or low confidence, starts on READ_BIG."""
    settings = get_settings()
    if router_enabled is None:
        router_enabled = settings.ROUTER_ENABLED
    if min_confidence is None:
        min_confidence = settings.JEV_MIN_CONFIDENCE

    fallback_kind = doc_hint or guess_doc_kind(native_text, caption)

    if not router_enabled:
        return TierDecision(
            tier=TIER_BIG,
            doc_kind=fallback_kind,
            reason="router disabled",
            degraded=True,
        )

    state = TierState(
        caption=caption,
        native_text=native_text[:4000],
        file_type=getattr(facts, "file_type", ""),
        width=getattr(facts, "width", 0),
        height=getattr(facts, "height", 0),
        blur=getattr(facts, "blur", 0.0),
        brightness=getattr(facts, "brightness", 0.0),
        page_count=getattr(facts, "page_count", 1),
    )
    judge = judge or ReadingJudge()
    try:
        judgment = await judge.choose_tier(state)
    except JudgmentUnavailableError as exc:
        # Fail closed: no router answer, no cheap read.
        logger.warning("tier router unavailable, starting on big: %s", exc)
        return TierDecision(
            tier=TIER_BIG,
            doc_kind=fallback_kind,
            reason="jev unavailable",
            degraded=True,
        )

    kind = judgment.doc_kind.choice or fallback_kind
    difficulty = judgment.difficulty.choice or "hard"
    confidence = float(judgment.doc_kind.confidence or 0.0)
    probabilities = getattr(judgment.doc_kind, "probabilities", {}) or {}

    overruled = False
    hint = (doc_hint or "").strip()
    if hint in _SELF_READ_KINDS and kind not in _SELF_READ_KINDS:
        logger.info(
            "jev kind ignored: Go's classifier owns the routing decision",
            extra={"jev_kind": kind, "go_hint": hint, "confidence": confidence},
        )
        kind = hint
        overruled = True

    escalate = difficulty == "hard" or confidence < min_confidence
    tier = TIER_BIG if escalate else TIER_SMALL
    reason = f"difficulty={difficulty} confidence={confidence:.2f}"
    if overruled:
        reason += f" (jev said {judgment.doc_kind.choice}, kept Go hint {hint})"
    return TierDecision(
        tier=tier,
        doc_kind=kind,
        difficulty=difficulty,
        confidence=confidence,
        reason=reason,
        # Jev disagreed with the classifier of record, so say so and let the
        # caller escalate rather than trust a cheap read.
        degraded=overruled,
        probabilities=probabilities,
    )


async def decide_escalation(
    extraction: dict[str, Any],
    checks: list[dict[str, Any]],
    *,
    unclear_cells: list[str] | None = None,
    rows_failing: int = 0,
    quality_max: int | None = None,
    judge: ReadingJudge | None = None,
) -> EscalationDecision:
    """Escalate when Jev rates the read low, rows likely misalign, or >1 row fails."""
    settings = get_settings()
    if quality_max is None:
        quality_max = settings.JEV_ESCALATE_QUALITY_MAX

    # A hard arithmetic failure beyond one row is enough without asking Jev.
    if rows_failing > ROWS_FAILING_ESCALATE:
        return EscalationDecision(
            escalate=True, reason=f"{rows_failing} rows fail the stock check"
        )

    state = QualityState(
        extraction=extraction,
        checks=checks,
        unclear_cells=list(unclear_cells or []),
        rows_failing=rows_failing,
    )
    judge = judge or ReadingJudge()
    try:
        judgment = await judge.check_quality(state)
    except JudgmentUnavailableError as exc:
        logger.warning("quality judge unavailable, escalating: %s", exc)
        return EscalationDecision(
            escalate=True, reason="jev unavailable", degraded=True
        )

    quality = int(round(judgment.extraction_quality.score))
    misalignment = float(judgment.misalignment_likely.noul or 0.0)
    reasons: list[str] = []
    if quality <= quality_max:
        reasons.append(f"quality {quality}")
    if misalignment > MISALIGNMENT_ESCALATE:
        reasons.append(f"misalignment {misalignment:.2f}")
    return EscalationDecision(
        escalate=bool(reasons),
        quality=quality,
        misalignment=misalignment,
        reason="+".join(reasons),
    )


async def same_cells_agree(
    first: dict[str, Any],
    second: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Cell-by-cell agreement: keep what both read, mark the rest unclear.

    Both models read the same image; where they agree the digits are trustworthy,
    where they disagree the cell is unclear and the owner is asked. Numbers are
    never averaged.
    """
    agreed: dict[str, Any] = {}
    disagreements: list[str] = []
    for key, value in first.items():
        other = second.get(key)
        if other == value:
            agreed[key] = value
        else:
            disagreements.append(key)
    return agreed, disagreements


__all__ = [
    "MISALIGNMENT_ESCALATE",
    "ROWS_FAILING_ESCALATE",
    "TIER_BIG",
    "TIER_SMALL",
    "EscalationDecision",
    "TierDecision",
    "decide_escalation",
    "decide_starting_tier",
    "guess_doc_kind",
    "same_cells_agree",
]

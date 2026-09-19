"""Evidence + confidence builders (Stage 3).

Evidence: every extracted field keeps document_id, page, region (OCR
bbox flattened), extraction method, and confidence. Transactions point
at their source page/line.

Confidence: composed from observable signals — OCR mean confidence,
field presence, schema validity, reconciliation outcome — never from
an LLM's self-report. Returned as a 0..1 score with the contributing
factors recorded for debugging.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from miriam_agent.documents.models import (
    ExtractedText,
    ExtractionMethod,
    ReconResult,
    StatementExtraction,
)
from miriam_agent.documents.schemas import DocumentEvidence


@dataclass
class ConfidenceBreakdown:
    score: float
    factors: dict[str, float] = field(default_factory=dict)


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def build_evidence(
    document_id: str,
    text: ExtractedText,
    ext: StatementExtraction,
    method: ExtractionMethod,
) -> list[DocumentEvidence]:
    evidence: list[DocumentEvidence] = []
    bbox_by_line: dict[tuple[int, int], list[float]] = {}
    for page in text.pages:
        for idx, ln in enumerate(page.lines):
            flat: list[float] = []
            for row in ln.bbox:
                flat.extend(float(x) for x in row)
            bbox_by_line[(page.page, idx)] = flat

    def region(page: int, line_idx: int) -> list[float]:
        return bbox_by_line.get((page, line_idx), [])

    def add(fname: str, page: int, line_idx: int, conf: float) -> None:
        evidence.append(
            DocumentEvidence(
                field=fname,
                page=page,
                region=region(page, line_idx),
                engine=text.engine or method,
                confidence=_clamp(conf),
            )
        )

    ocr_conf = text.mean_confidence or 0.5
    if ext.opening_balance is not None:
        add("opening_balance", 1, 0, ocr_conf)
    if ext.closing_balance is not None:
        add("closing_balance", 1, 0, ocr_conf)
    for txn in ext.transactions:
        evidence.append(
            DocumentEvidence(
                field=f"transaction:{txn.date.normalized.isoformat() if txn.date else '?'}:{txn.description[:40]}",
                page=txn.page,
                region=region(txn.page, txn.line_index),
                engine=text.engine or method,
                confidence=_clamp(txn.confidence),
            )
        )
    return evidence


def compose_confidence(
    text: ExtractedText,
    ext: StatementExtraction,
    recon: ReconResult,
    classify_conf: float,
) -> ConfidenceBreakdown:
    factors: dict[str, float] = {}
    factors["ocr"] = _clamp(text.mean_confidence) if text.mean_confidence else 0.5
    present = sum(
        1
        for v in (
            ext.institution,
            ext.account_name,
            ext.currency,
            ext.opening_balance,
            ext.closing_balance,
        )
        if v
    )
    factors["field_presence"] = present / 5.0
    factors["classification"] = _clamp(classify_conf)
    if recon.status == "reconciled":
        factors["reconciliation"] = 1.0
    elif recon.status == "mismatch":
        factors["reconciliation"] = 0.3
    else:
        factors["reconciliation"] = 0.6
    with_amounts = [t for t in ext.transactions if t.amount is not None]
    factors["txns"] = len(with_amounts) / max(1, len(ext.transactions)) if ext.transactions else 0.0
    score = (
        0.25 * factors["ocr"]
        + 0.20 * factors["field_presence"]
        + 0.15 * factors["classification"]
        + 0.25 * factors["reconciliation"]
        + 0.15 * factors["txns"]
    )
    return ConfidenceBreakdown(score=round(_clamp(score), 4), factors=factors)

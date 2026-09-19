"""Stage 3 document pipeline orchestrator.

Composes small stages — inspect, native PDF, OCR fallback, classify,
extract, validate, reconcile, evidence/confidence, contract v1. Each
stage is a pure function or thin injected dependency; this module only
sequences them and records observability. Idempotent: same bytes in
gives equivalent results out; no persistence here (Go owns that).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from miriam_agent.core.exceptions import IntegrationError, ValidationError
from miriam_agent.documents import evidence as evidence_mod
from miriam_agent.documents.classify import classify
from miriam_agent.documents.extraction.native_pdf import assess_quality, extract_native_text
from miriam_agent.documents.models import (
    DocumentKind,
    ExtractedText,
    ExtractionMethod,
    ReconResult,
    StatementExtraction,
)
from miriam_agent.documents.processors import registry as processor_registry
from miriam_agent.documents.reconcile import reconcile
from miriam_agent.observability.correlation import current_trace_id

logger = logging.getLogger(__name__)


class OCRProvider(Protocol):
    async def recognize(self, data: bytes, mime_type: str, *, doc_hint: str = "") -> ExtractedText: ...


@dataclass
class PipelineConfig:
    native_pdf_enabled: bool = True
    ocr_enabled: bool = True
    min_text_chars: int = 120
    reconciliation_tolerance: Decimal = Decimal("1.00")
    llm_enabled: bool = False


@dataclass
class PipelineRun:
    result: Any
    method: ExtractionMethod
    stages: dict[str, Any] = field(default_factory=dict)


def detect_mime(data: bytes) -> str:
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"\x89PNG":
        return "image/png"
    return "application/octet-stream"


def redact_for_llm(text: str) -> str:
    """PII minimization for future LLM calls: mask 8+ digit runs (account
    numbers, BVN/NIN, cards). Deterministic stages see original text."""
    return re.sub(r"\b\d{8,}\b", lambda m: "*" * (len(m.group(0)) - 4) + m.group(0)[-4:], text)


async def process_document(data, *, document_id, mime_type="", ocr=None, config=None, llm_extractor=None):
    from miriam_agent.documents.processors.statement_dict import statement_to_dict
    from miriam_agent.documents.schemas import CONTRACT_VERSION, DocumentData, DocumentResult, DocumentValidation

    cfg = config or PipelineConfig()
    started = time.perf_counter()
    stages: dict[str, Any] = {}
    mime = mime_type or detect_mime(data)
    stages["mime"] = mime
    text, method = await _extract_text(data, mime, ocr, cfg, stages)
    kind, kind_conf = classify(text)
    stages["classification"] = {"type": kind, "confidence": kind_conf}
    handler = processor_registry.get_processor(kind)
    if handler is not None:
        ext = handler(text)
        stages["extraction"] = {"processor": kind, "transactions": len(ext.transactions)}
    else:
        void = (llm_extractor, cfg)
        del void
        ext = StatementExtraction()
        stages["extraction"] = {"processor": "none", "transactions": 0}
    recon = reconcile(ext, tolerance=cfg.reconciliation_tolerance)
    evidence = evidence_mod.build_evidence(document_id, text, ext, method)
    conf = evidence_mod.compose_confidence(text, ext, recon, kind_conf)
    if kind == "bank_statement" and ext.transactions:
        status = "completed"
    elif kind in ("receipt", "invoice"):
        status = "completed"
    else:
        status = "failed"
    raw: dict[str, Any] = {"extraction_method": method, "pages": len(text.pages)}
    if kind == "bank_statement":
        raw["statement"] = statement_to_dict(ext)
    data_out = DocumentData(merchant=ext.institution or None, amount=str(ext.closing_balance.normalized) if ext.closing_balance else None, currency=ext.currency or None, document_date=ext.period_end.normalized.isoformat() if ext.period_end else None, account_name=ext.account_name or None, opening_balance=str(ext.opening_balance.normalized) if ext.opening_balance else None, closing_balance=str(ext.closing_balance.normalized) if ext.closing_balance else None, raw=raw)
    if recon.status == "reconciled":
        vstatus = "valid"
    elif recon.status == "mismatch":
        vstatus = "invalid"
    else:
        vstatus = "unknown"
    validation = DocumentValidation(status=vstatus, reconciled=recon.status == "reconciled", difference=str(recon.difference.quantize(Decimal("0.00"))), checks=[{"name": c.name, "passed": c.passed, "message": c.message} for c in recon.checks], errors=list(recon.errors))
    result = DocumentResult(schema_version=CONTRACT_VERSION, document_id=document_id, document_type=kind, status=status, confidence=conf.score, data=data_out, validation=validation, evidence=evidence)
    stages["duration_s"] = round(time.perf_counter() - started, 3)
    stages["trace_id"] = current_trace_id()
    logger.info("document pipeline complete", extra={"document_id": document_id, "type": kind, "method": method, "status": status})
    return PipelineRun(result=result, method=method, stages=stages)


async def _extract_text(data, mime, ocr, cfg, stages):
    if not data:
        raise ValidationError("empty document")
    if mime == "application/pdf" and cfg.native_pdf_enabled:
        try:
            native = extract_native_text(data)
        except ValueError as e:
            stages["native_pdf"] = {"ok": False, "error": str(e)}
            native = None
        if native is not None:
            quality = assess_quality(native, min_chars=cfg.min_text_chars)
            stages["native_pdf"] = {"ok": True, "chars": quality.chars, "lines": quality.lines, "usable": quality.usable, "term_hits": quality.term_hits}
            if quality.usable or not cfg.ocr_enabled or ocr is None:
                native.method = "native_pdf"
                return native, "native_pdf"
            stages["native_pdf"]["escalated"] = True
    if ocr is None or not cfg.ocr_enabled:
        raise IntegrationError("no usable text and OCR unavailable")
    ocr_text = await ocr.recognize(data, mime)
    stages["ocr"] = {"engine": ocr_text.engine, "pages": len(ocr_text.pages)}
    if not ocr_text.full_text.strip():
        raise IntegrationError("OCR produced no text")
    escalated = stages.get("native_pdf", {}).get("escalated", False)
    method = "native_pdf_then_ocr" if escalated else "ocr"
    ocr_text.method = method  # type: ignore[assignment]
    return ocr_text, method

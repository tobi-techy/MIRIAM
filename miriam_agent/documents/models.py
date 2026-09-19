"""Shared internal models for the Stage 3 document pipeline.

These are the processing-plane working types, not the wire contract:
stages produce/consume these, and only ``pipeline.py`` maps the final
outcome into ``schemas.DocumentResult`` (contract v1). Money is
``Decimal`` everywhere; raw strings are preserved alongside normalized
values for evidence/debugging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

DocumentKind = Literal["bank_statement", "receipt", "invoice", "unknown"]
ExtractionMethod = Literal["native_pdf", "ocr", "native_pdf_then_ocr", "none"]


@dataclass
class OCRLine:
    text: str
    page: int
    confidence: float = 0.0
    bbox: list[list[float]] = field(default_factory=list)


@dataclass
class PageText:
    page: int
    text: str
    lines: list[OCRLine] = field(default_factory=list)


@dataclass
class ExtractedText:
    """Unified text from native PDF and/or OCR, with provenance."""

    full_text: str
    pages: list[PageText] = field(default_factory=list)
    method: ExtractionMethod = "none"
    mean_confidence: float = 0.0
    engine: str = ""


@dataclass
class RawMoney:
    raw: str
    normalized: Decimal


@dataclass
class RawDate:
    raw: str
    normalized: date


@dataclass
class ParsedTransaction:
    date: RawDate | None = None
    description: str = ""
    raw_description: str = ""
    amount: RawMoney | None = None
    direction: Literal["credit", "debit"] | None = None
    currency: str = ""
    balance_after: RawMoney | None = None
    merchant: str = ""
    reference: str = ""
    page: int = 1
    line_index: int = 0
    confidence: float = 0.0


@dataclass
class StatementExtraction:
    institution: str = ""
    account_name: str = ""
    masked_account_number: str = ""
    currency: str = ""
    period_start: RawDate | None = None
    period_end: RawDate | None = None
    opening_balance: RawMoney | None = None
    closing_balance: RawMoney | None = None
    total_credits: Decimal | None = None
    total_debits: Decimal | None = None
    transactions: list[ParsedTransaction] = field(default_factory=list)


@dataclass
class ReconCheck:
    name: str
    passed: bool
    message: str = ""


@dataclass
class ReconResult:
    status: Literal["reconciled", "mismatch", "skipped"]
    difference: Decimal = Decimal("0.00")
    checks: list[ReconCheck] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

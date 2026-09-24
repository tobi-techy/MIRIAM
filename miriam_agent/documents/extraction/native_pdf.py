"""Native PDF text extraction (Stage 3, cheapest rung).

Uses pypdf only — no OCR, no network. Pages stream one at a time and text is
capped so a pathological PDF cannot blow memory. "Usable" is deterministic:
callers need char count, line count, density, and statement-term hits, not a
bare ``if text`` check.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from miriam_agent.documents.models import ExtractedText, PageText

logger = logging.getLogger(__name__)

STATEMENT_TERMS = (
    "opening balance",
    "closing balance",
    "account number",
    "statement period",
    "transaction date",
    "value date",
    "balance b/f",
    "balance c/f",
    "debit",
    "credit",
)

MAX_PAGES = 30
MAX_CHARS = 500_000


@dataclass
class NativeTextQuality:
    usable: bool
    chars: int
    lines: int
    density: float
    term_hits: int
    reason: str


def extract_native_text(data: bytes, *, max_pages: int = MAX_PAGES) -> ExtractedText:
    """Extract embedded text page-by-page. Raises on corrupt/encrypted PDFs."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:
        raise ValueError(f"unreadable PDF: {e}") from e
    if getattr(reader, "is_encrypted", False):
        raise ValueError("encrypted PDF: password required")

    pages: list[PageText] = []
    total_chars = 0
    for idx, page in enumerate(reader.pages[:max_pages]):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = text.strip()
        if total_chars + len(text) > MAX_CHARS:
            text = text[: max(0, MAX_CHARS - total_chars)]
        total_chars += len(text)
        lines = [ln for ln in (ln.strip() for ln in text.splitlines()) if ln]
        pages.append(PageText(page=idx + 1, text=text, lines=[]))
        void = lines  # lines counted in quality; OCR lines carry bboxes later
        del void
        if total_chars >= MAX_CHARS:
            break
    full = "\n".join(p.text for p in pages).strip()
    return ExtractedText(
        full_text=full, pages=pages, method="native_pdf", engine="pypdf"
    )


def assess_quality(extracted: ExtractedText, *, min_chars: int) -> NativeTextQuality:
    """Deterministic usability gate for native text."""
    chars = len(extracted.full_text)
    lines = sum(1 for ln in extracted.full_text.splitlines() if ln.strip())
    pages = max(1, len(extracted.pages))
    density = chars / pages
    lower = extracted.full_text.lower()
    term_hits = sum(1 for term in STATEMENT_TERMS if term in lower)
    if chars < min_chars:
        return NativeTextQuality(
            False, chars, lines, density, term_hits, "too_few_chars"
        )
    if lines < 5:
        return NativeTextQuality(
            False, chars, lines, density, term_hits, "too_few_lines"
        )
    if density < 50:
        return NativeTextQuality(False, chars, lines, density, term_hits, "low_density")
    return NativeTextQuality(True, chars, lines, density, term_hits, "usable")

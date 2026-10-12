"""Orchestration for one shop read: route, read, check, note, reply, save.

The flow is deliberately linear and boring:

1. decode and preprocess (deterministic),
2. pick the tier with Jev (fail closed to the big model),
3. two-pass read on that tier,
4. deterministic checks and note classification,
5. escalate once if Jev or the checks say the read is weak, then merge cell by
   cell and mark disagreements unclear,
6. build the reply from checked data only, and
7. save idempotently on (user_id, sha256).

No step ever emits a partial read: a failure anywhere becomes the one retake
message.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import ValidationError

from miriam_agent.config.settings import get_settings
from miriam_agent.core.exceptions import AgentError, ConfigurationError
from miriam_agent.judgment.reading import ReadingJudge
from miriam_agent.shop import reply as reply_mod
from miriam_agent.shop.checks import (
    check_stock_book,
    check_supplier_document,
    checks_as_state,
)
from miriam_agent.shop.notes import (
    NoteEvaluation,
    classify_and_parse,
    is_blank_note,
)
from miriam_agent.shop.preprocess import prepare
from miriam_agent.shop.reader import (
    ReadBudget,
    ReadCapExceeded,
    ReadFailed,
    ReadResult,
    classify_page,
    read_document,
)
from miriam_agent.shop.router import (
    TIER_BIG,
    decide_escalation,
    decide_starting_tier,
)
from miriam_agent.shop.schemas import (
    CrossedOut,
    DocType,
    InvoiceLine,
    StockBook,
    StockRow,
    SupplierDocument,
)
from miriam_agent.shop.store import ShopDocumentStore, default_store

logger = logging.getLogger(__name__)

_SELF_READ_KINDS = {
    DocType.STOCK_BOOK.value,
    DocType.INVOICE.value,
    DocType.DELIVERY_NOTE.value,
}

# Go's own word for "I could not classify this page, you decide". A photo of a
# handwritten book whose text no OCR pass could read arrives like this: without
# it the reader would fail closed and the owner would be told Miriam cannot look
# at photos, which is no longer true.
UNKNOWN_IMAGE_HINT = "unknown_image"

_STOCK_CELLS = ("opening", "supplied", "sold", "left", "price_naira")
_LINE_CELLS = ("qty", "unit_price_naira", "line_total_naira")


@dataclass
class ShopReadResult:
    reply_text: str
    doc_id: str
    doc_type: str
    flags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    model_path: list[str] = field(default_factory=list)


def _new_crossed(old: str | None, new: str | None) -> CrossedOut:
    return CrossedOut(old=old, new=new)


async def read_shop_document(
    *,
    file_b64: str,
    mime: str,
    user_id: str,
    message_id: str = "",
    doc_hint: str = "",
    caption: str = "",
    store: ShopDocumentStore | None = None,
    judge: ReadingJudge | None = None,
    provider_factory: Any = None,
    router_enabled: bool | None = None,
) -> ShopReadResult:
    """Read one inbound file and return the checked reply."""
    store = store or default_store()
    settings = get_settings()

    try:
        data = base64.b64decode(file_b64, validate=True)
    except (ValueError, TypeError) as e:
        raise ReadFailed(reply_mod.retake_reply()) from e
    if not data:
        raise ReadFailed(reply_mod.retake_reply())

    prepared = prepare(data, mime, caption)
    sha = prepared.sha256

    existing = await store.find_by_sha(user_id, sha)
    if existing is not None:
        logger.info("shop read dedupe hit", extra={"user_id": user_id, "sha256": sha})
        return ShopReadResult(
            reply_text=existing.reply_text,
            doc_id=existing.id,
            doc_type=existing.doc_type,
            flags=list(existing.flags),
            confidence=existing.confidence,
            model_path=list(existing.model_path),
        )

    _hint = (doc_hint or "").strip().lower()
    classify_flags: list[str] = []
    tier = await decide_starting_tier(
        prepared.facts,
        caption=caption,
        native_text=prepared.native_text,
        doc_hint=doc_hint,
        judge=judge,
        router_enabled=router_enabled,
    )
    logger.info(
        "shop read routed",
        extra={
            "doc_kind": tier.doc_kind,
            "tier": tier.tier,
            "difficulty": tier.difficulty,
            "confidence": tier.confidence,
            "probabilities": tier.probabilities,
            "degraded": tier.degraded,
        },
    )

    if tier.doc_kind not in _SELF_READ_KINDS and _hint == UNKNOWN_IMAGE_HINT:
        # Go has no classification and said so. One cheap model call decides the
        # kind before any read, and only the reader's own kinds are read: a
        # receipt or a statement still leaves with an empty reply.
        kind, confidence = await _classify_unknown_page(
            prepared, caption=caption, provider_factory=provider_factory
        )
        if kind:
            # The reader's own answer replaces "Go does not know". Only its
            # kinds are read; a receipt or a statement keeps the empty reply Go
            # already answers with, but the kind travels so the log says what
            # the page turned out to be.
            tier = replace(
                tier,
                doc_kind=kind,
                tier=TIER_BIG,
                confidence=confidence,
                reason=f"page kind from the reader ({kind})",
                degraded=True,
            )
            if kind in _SELF_READ_KINDS:
                classify_flags.append(f"page classified by the reader: {kind}")
            else:
                classify_flags.append(f"page classified by the reader as {kind}")

    if tier.doc_kind not in _SELF_READ_KINDS:
        # A receipt, statement or unknown page is not ours to answer: Go keeps
        # its existing path for those. Return the kind and an empty reply.
        return ShopReadResult(
            reply_text="",
            doc_id="",
            doc_type=tier.doc_kind,
            flags=[f"not a shop document ({tier.doc_kind})"],
            confidence=tier.confidence,
            model_path=[],
        )

    budget = ReadBudget(max_extractions=settings.SHOP_READ_MAX_EXTRACTIONS)
    try:
        read = await read_document(
            prepared,
            tier.doc_kind,
            tier.tier,
            caption=caption,
            budget=budget,
            provider_factory=provider_factory,
        )
    except (AgentError, ConfigurationError, ReadCapExceeded, ReadFailed) as e:
        logger.warning("shop read failed: %s", e)
        raise ReadFailed(reply_mod.retake_reply()) from e

    model_path = list(read.model_path)
    flags = list(read.flags) + classify_flags
    degraded = read.degraded or tier.degraded

    if tier.doc_kind == DocType.STOCK_BOOK.value:
        read, escalated = await _escalate_stock_book(
            read, prepared, caption, budget, provider_factory, judge, tier.tier
        )
        model_path.extend(read.model_path[len(model_path) :])
        flags.extend(read.flags[len(flags) :])
        return await _finish_stock_book(
            read,
            user_id=user_id,
            message_id=message_id,
            mime=mime,
            sha=sha,
            model_path=model_path,
            flags=flags,
            degraded=degraded,
            judge=judge,
            store=store,
            router_state=_router_state(tier),
            escalated=escalated,
            prompt_version=settings.SHOP_READ_PROMPT_VERSION,
        )

    read, escalated = await _escalate_supplier(
        read, prepared, caption, budget, provider_factory, judge, tier.tier
    )
    model_path.extend(read.model_path[len(model_path) :])
    flags.extend(read.flags[len(flags) :])
    return await _finish_supplier(
        read,
        user_id=user_id,
        message_id=message_id,
        mime=mime,
        sha=sha,
        model_path=model_path,
        flags=flags,
        degraded=degraded,
        store=store,
        router_state=_router_state(tier),
        escalated=escalated,
        prompt_version=settings.SHOP_READ_PROMPT_VERSION,
    )


def _router_state(tier: Any) -> dict[str, Any]:
    return {
        "doc_kind": tier.doc_kind,
        "difficulty": tier.difficulty,
        "confidence": tier.confidence,
        "reason": tier.reason,
        "probabilities": dict(tier.probabilities or {}),
        "degraded": tier.degraded,
    }


async def _escalate_stock_book(
    read: ReadResult,
    prepared: Any,
    caption: str,
    budget: ReadBudget,
    provider_factory: Any,
    judge: ReadingJudge | None,
    starting_tier: str,
) -> tuple[ReadResult, bool]:
    """Escalate a weak small read to the big model, then merge cell by cell."""
    if starting_tier != "small":
        return read, False
    book = read.extraction
    if not isinstance(book, StockBook):
        return read, False
    report = check_stock_book(book)
    decision = await decide_escalation(
        book.model_dump(),
        checks_as_state(report.checks),
        unclear_cells=report.unclear,
        rows_failing=report.rows_failing,
        judge=judge,
    )
    if not decision.escalate:
        return read, False
    try:
        second = await read_document(
            prepared,
            DocType.STOCK_BOOK.value,
            TIER_BIG,
            caption=caption,
            budget=budget,
            provider_factory=provider_factory,
        )
    except (AgentError, ReadCapExceeded, ReadFailed) as e:
        # Escalation could not run: keep the first read, say so.
        read.flags.append(f"escalation failed: {e}")
        return read, False
    merged, disagreements = _merge_stock_books(book, second.extraction)
    merged_flags = list(read.flags) + list(second.flags)
    if disagreements:
        merged_flags.append(f"models disagreed on {len(disagreements)} cell(s)")
    return (
        ReadResult(
            schema_kind=read.schema_kind,
            extraction=merged,
            model_path=list(read.model_path) + list(second.model_path),
            reads_used=budget.used,
            flags=merged_flags,
            confidence=_min_confidence(book, second.extraction),
            degraded=True,
        ),
        True,
    )


async def _escalate_supplier(
    read: ReadResult,
    prepared: Any,
    caption: str,
    budget: ReadBudget,
    provider_factory: Any,
    judge: ReadingJudge | None,
    starting_tier: str,
) -> tuple[ReadResult, bool]:
    if starting_tier != "small":
        return read, False
    doc = read.extraction
    if not isinstance(doc, SupplierDocument):
        return read, False
    report = check_supplier_document(doc)
    decision = await decide_escalation(
        doc.model_dump(),
        checks_as_state(report.checks),
        judge=judge,
    )
    if not decision.escalate:
        return read, False
    try:
        second = await read_document(
            prepared,
            read.schema_kind,
            TIER_BIG,
            caption=caption,
            budget=budget,
            provider_factory=provider_factory,
        )
    except (AgentError, ReadCapExceeded, ReadFailed) as e:
        read.flags.append(f"escalation failed: {e}")
        return read, False
    merged, disagreements = _merge_supplier(doc, second.extraction)
    merged_flags = list(read.flags) + list(second.flags)
    if disagreements:
        merged_flags.append(f"models disagreed on {len(disagreements)} cell(s)")
    return (
        ReadResult(
            schema_kind=read.schema_kind,
            extraction=merged,
            model_path=list(read.model_path) + list(second.model_path),
            reads_used=budget.used,
            flags=merged_flags,
            confidence=_min_confidence(doc, second.extraction),
            degraded=True,
        ),
        True,
    )


def _merge_stock_books(first: StockBook, second: Any) -> tuple[StockBook, list[str]]:
    """Keep agreed cells, mark disagreements unclear. Never average."""
    if not isinstance(second, StockBook):
        return first, ["second read shape mismatch"]
    disagreements: list[str] = []
    if len(first.rows) != len(second.rows):
        disagreements.append("row_count")
    rows: list[StockRow] = []
    for idx, row in enumerate(first.rows):
        other = second.rows[idx] if idx < len(second.rows) else None
        unclear = list(row.unclear)
        updates: dict[str, Any] = {}
        for cell in _STOCK_CELLS:
            left = getattr(row, cell)
            right = getattr(other, cell) if other is not None else None
            if other is not None and left != right:
                unclear.append(cell)
                disagreements.append(f"rows[{idx}].{cell}")
        if unclear != row.unclear:
            updates["unclear"] = sorted(set(unclear))
        updates["confidence"] = min(
            row.confidence,
            other.confidence if other is not None else 1.0,
        )
        rows.append(row.model_copy(update=updates))
    merged = first.model_copy(update={"rows": rows})
    return merged, disagreements


def _merge_supplier(
    first: SupplierDocument, second: Any
) -> tuple[SupplierDocument, list[str]]:
    if not isinstance(second, SupplierDocument):
        return first, ["second read shape mismatch"]
    disagreements: list[str] = []
    if len(first.lines) != len(second.lines):
        disagreements.append("line_count")
    lines: list[InvoiceLine] = []
    for idx, line in enumerate(first.lines):
        other = second.lines[idx] if idx < len(second.lines) else None
        unclear = list(line.unclear)
        for cell in _LINE_CELLS:
            left = getattr(line, cell)
            right = getattr(other, cell) if other is not None else None
            if other is not None and left != right:
                unclear.append(cell)
                disagreements.append(f"lines[{idx}].{cell}")
        lines.append(line.model_copy(update={"unclear": sorted(set(unclear))}))
    merged = first.model_copy(update={"lines": lines})
    return merged, disagreements


def _min_confidence(first: Any, second: Any) -> float:
    values = [
        float(getattr(first, "confidence", 0.0) or 0.0),
        float(getattr(second, "confidence", 0.0) or 0.0),
    ]
    rows = getattr(first, "rows", None) or getattr(first, "lines", None) or []
    values.extend(float(getattr(r, "confidence", 0.0) or 0.0) for r in rows)
    return min(values) if values else 0.0


async def _classify_notes(
    raw_notes: list[str], *, judge: ReadingJudge | None
) -> list[NoteEvaluation]:
    evaluations: list[NoteEvaluation] = []
    for line in raw_notes:
        # A separator the reader mistook for a note is not a note. Dropped here
        # it costs no judgment call, leaves no row, raises no flag, and never
        # reaches the reply as "I could not classify one note: — '' —".
        if is_blank_note(line):
            continue
        evaluations.append(await classify_and_parse(line, judge=judge))
    return evaluations


async def _classify_unknown_page(
    prepared: Any,
    *,
    caption: str,
    provider_factory: Any = None,
) -> tuple[str, float]:
    """Ask the model what a page is, for a page Go could not classify.

    Fail closed: no answer, no read. The caller keeps its existing answer for
    that page, which is what a photo nobody can read has always got.
    """
    provider = provider_factory("small") if provider_factory is not None else None
    try:
        return await classify_page(prepared, caption=caption, provider=provider)
    except (AgentError, ConfigurationError, ValidationError) as e:
        # An unusable answer is not a read: a gateway that ignores the schema
        # leaves the page exactly where it was, declined.
        logger.warning("page classification failed, declining the page: %s", e)
        return "", 0.0


def _note_rows(notes: list[NoteEvaluation]) -> list[dict[str, Any]]:
    return [
        {
            "text": n.text,
            "kind": n.kind.value,
            "party": n.parse.party,
            "amount_naira": n.parse.amount_naira,
            "due": n.parse.due,
        }
        for n in notes
    ]


async def _finish_stock_book(
    read: ReadResult,
    *,
    user_id: str,
    message_id: str,
    mime: str,
    sha: str,
    model_path: list[str],
    flags: list[str],
    degraded: bool,
    judge: ReadingJudge | None,
    store: ShopDocumentStore,
    router_state: dict[str, Any],
    escalated: bool,
    prompt_version: str,
) -> ShopReadResult:
    book = read.extraction
    assert isinstance(book, StockBook)
    report = check_stock_book(book)
    notes = await _classify_notes(book.notes, judge=judge)
    all_flags = list(flags)
    all_flags.extend(report.flags)
    for note in notes:
        all_flags.extend(note.flags)
    if escalated:
        all_flags.append("escalated to big model")
    reply_text = reply_mod.stock_book_reply(
        book,
        report,
        notes,
        disagreements=read.disagreements,
        trust_label=reply_mod.TRUST_SELF_REPORTED,
    )
    stored = await store.save(
        user_id=user_id,
        message_id=message_id,
        doc_type=DocType.STOCK_BOOK.value,
        trust_label=reply_mod.TRUST_SELF_REPORTED,
        sha256=sha,
        mime=mime,
        model_path=model_path,
        models_used=_models_used(model_path),
        jev_decision=router_state,
        flags=all_flags,
        prompt_version=prompt_version,
        confidence=read.confidence,
        reply_text=reply_text,
        raw_extraction=book.model_dump(),
        rows=_stock_row_dicts(book),
        lines=[],
        notes=_note_rows(notes),
    )
    return ShopReadResult(
        reply_text=stored.reply_text,
        doc_id=stored.id,
        doc_type=DocType.STOCK_BOOK.value,
        flags=all_flags,
        confidence=read.confidence,
        model_path=model_path,
    )


async def _finish_supplier(
    read: ReadResult,
    *,
    user_id: str,
    message_id: str,
    mime: str,
    sha: str,
    model_path: list[str],
    flags: list[str],
    degraded: bool,
    store: ShopDocumentStore,
    router_state: dict[str, Any],
    escalated: bool,
    prompt_version: str,
) -> ShopReadResult:
    doc = read.extraction
    assert isinstance(doc, SupplierDocument)
    report = check_supplier_document(doc)
    all_flags = list(flags)
    all_flags.extend(report.flags)
    if escalated:
        all_flags.append("escalated to big model")
    doc_type = (
        DocType.DELIVERY_NOTE.value
        if doc.doc_kind == "delivery_note"
        else DocType.INVOICE.value
    )
    reply_text = reply_mod.supplier_reply(
        doc, report, trust_label=reply_mod.TRUST_SUPPLIER_DOCUMENT
    )
    stored = await store.save(
        user_id=user_id,
        message_id=message_id,
        doc_type=doc_type,
        trust_label=reply_mod.TRUST_SUPPLIER_DOCUMENT,
        sha256=sha,
        mime=mime,
        model_path=model_path,
        models_used=_models_used(model_path),
        jev_decision=router_state,
        flags=all_flags,
        prompt_version=prompt_version,
        confidence=read.confidence,
        reply_text=reply_text,
        raw_extraction=doc.model_dump(),
        rows=[],
        lines=_invoice_line_dicts(doc),
        notes=[],
    )
    return ShopReadResult(
        reply_text=stored.reply_text,
        doc_id=stored.id,
        doc_type=doc_type,
        flags=all_flags,
        confidence=read.confidence,
        model_path=model_path,
    )


def _models_used(model_path: list[str]) -> dict[str, Any]:
    settings = get_settings()
    used: dict[str, Any] = {}
    joined = " ".join(model_path)
    if "small" in joined:
        used["small"] = settings.READ_SMALL_MODEL
    if "big" in joined:
        used["big"] = settings.READ_BIG_MODEL
    return used


def _stock_row_dicts(book: StockBook) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in book.rows:
        out.append(
            {
                "item": row.item,
                "opening": row.opening,
                "supplied": row.supplied,
                "sold": row.sold,
                "left": row.left,
                "price_naira": row.price_naira,
                "crossed_out_old": row.crossed_out.old if row.crossed_out else None,
                "crossed_out_new": row.crossed_out.new if row.crossed_out else None,
                "unclear": row.unclear,
                "confidence": row.confidence,
            }
        )
    return out


def _invoice_line_dicts(doc: SupplierDocument) -> list[dict[str, Any]]:
    return [
        {
            "item": line.item,
            "qty": line.qty,
            "unit_price_naira": line.unit_price_naira,
            "line_total_naira": line.line_total_naira,
        }
        for line in doc.lines
    ]


__all__ = ["ShopReadResult", "read_shop_document"]

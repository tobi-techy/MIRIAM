"""Reply templates. Built from checked data, never from model prose.

Every figure that reaches the owner here was computed in ``shop.checks`` from
digits the model read. The template cannot invent a number because it has no
generator: it only formats values that already passed a check. Texting style,
short lines, and no em dashes anywhere.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from miriam_agent.shop.checks import (
    CURRENCY_SYMBOL,
    InvoiceReport,
    StockBookReport,
    naira,
)
from miriam_agent.shop.notes import NoteEvaluation, is_blank_note
from miriam_agent.shop.schemas import (
    Disagreement,
    NoteKind,
    StockBook,
    SupplierDocument,
    cell_words,
)

TRUST_SELF_REPORTED = "self_reported"
TRUST_SUPPLIER_DOCUMENT = "supplier_document"


def _trust_line(label: str) -> str:
    if label == TRUST_SUPPLIER_DOCUMENT:
        return (
            "Saved as a supplier document, so it can back your stock record "
            "once it matches a supply line."
        )
    return (
        "Saved as your own book (self reported), so it is your record only and "
        "never feeds credit on its own."
    )


def _note_line(note: NoteEvaluation) -> str:
    parse = note.parse
    amount = naira(int(Decimal(parse.amount_naira) * 100)) if parse.amount_naira else ""
    party = parse.party or "someone"
    due = f" due {parse.due}" if parse.due else ""
    if note.kind is NoteKind.CUSTOMER_DEBT:
        return f"{party} owes {amount}{due}".strip()
    if note.kind is NoteKind.PAYMENT_OUT:
        return f"Paid {party} {amount}".strip()
    if note.kind is NoteKind.PAYMENT_IN_UNVERIFIED:
        return (
            f"{party} says {amount or 'a payment'} came in, not confirmed yet, "
            "I will match it against the bank"
        )
    if note.kind is NoteKind.SUPPLIER_DELIVERY:
        return f"{note.text} (delivery not verified, no receipt)"
    if note.kind is NoteKind.PRICE_CHANGE:
        return f"Price change noted: {note.text}"
    return f"I did not understand this note: {note.text}"


def _questions(
    *,
    low_confidence: list[str],
    failed_rows: list[tuple[str, str]],
    unclear: list[str],
    needs_amount: list[str],
) -> list[str]:
    out: list[str] = []
    for item, cells in failed_rows:
        out.append(f"{item}: check the numbers, they do not add up")
    for item in low_confidence:
        out.append(f"{item}: I am not sure I read that row right, please confirm")
    for entry in unclear:
        out.append(f"{entry}: tell me the value and I will fix it")
    for line in needs_amount:
        out.append(f"How much was this one: {line}?")
    return out


def _held_cell_line(line: str, held: list[Disagreement]) -> bool:
    """True when a low-stock line prints a figure the two reads disagree on."""
    return any(line.startswith(d.label.strip() + " (") for d in held)


def _disagreement_question(item: Disagreement) -> str:
    """The two figures, and no choice made between them."""
    return (
        f"I couldn't read the {cell_words(item.cell)} figure for "
        f"{item.label.strip()}, is it {item.first} or {item.second}?"
    )


def _short_join(items: list[str], limit: int = 8) -> str:
    shown = items[:limit]
    text = "; ".join(shown)
    if len(items) > limit:
        text += f"; and {len(items) - limit} more"
    return text


def _written_total_echo(
    notes: list[NoteEvaluation], book: StockBook
) -> list[NoteEvaluation]:
    """Drop a note that only restates the total the reply already shows.

    A live read answered with both "You wrote ₦254,400..." and, as a note,
    "Total sales today: ₦254,400" — the same figure twice, once as a quote of
    the page. The gap line is the figure that matters.
    """
    written = (book.written_total_sales_naira or "").strip()
    if not written:
        return notes
    try:
        written_minor = int(Decimal(written.replace(",", "")) * 100)
    except (InvalidOperation, ValueError):
        return notes
    kept: list[NoteEvaluation] = []
    for note in notes:
        amount = note.parse.amount_naira
        if amount is not None:
            try:
                if int(Decimal(amount) * 100) == written_minor:
                    continue
            except (InvalidOperation, ValueError):
                pass
        kept.append(note)
    return kept


_PAGE_CURRENCY = re.compile(r"(?<![A-Za-z0-9])(?:N|₦|#)\s?(?=\d)")


def _page_line(text: str) -> str:
    """A line quoted from the page, with one currency symbol.

    The page is the owner's own hand: "Ns", "₦s" and "#s" all appear. Nothing
    but that one symbol changes; every figure stays exactly as written.
    """
    return _PAGE_CURRENCY.sub(CURRENCY_SYMBOL, text)


def _note_lines(notes: list[NoteEvaluation]) -> list[str]:
    """One line per note, whatever the reader made of it: the kinds it understood
    as Miriam says them, the rest verbatim. No repeated "I could not classify"."""
    out: list[str] = []
    for note in notes:
        if is_blank_note(note.text):
            continue
        out.append(_page_line(note.text) if note.unclassified else _note_line(note))
    return out


def _held_cells(disagreements: Sequence[Disagreement]) -> list[Disagreement]:
    """The cells the reply must not state a figure for.

    A "left" cell is the one figure the reply prints as a fact on its own: in
    the row that does not add up ("book says 9") and in low stock ("7 left").
    When the two reads disagree on it, both of those stay out and the question
    carries the two candidates instead. A disputed price or quantity would
    change the money maths, which this template never touches.
    """
    return [d for d in disagreements if d.cell == "left" and d.label.strip()]


def _is_held(name: str, held: list[Disagreement]) -> bool:
    return any(name.strip().lower() == d.label.strip().lower() for d in held)


def stock_book_reply(
    book: StockBook,
    report: StockBookReport,
    notes: list[NoteEvaluation],
    *,
    disagreements: Sequence[Disagreement] = (),
    trust_label: str = TRUST_SELF_REPORTED,
) -> str:
    """The whole stock-book reply, from checked data only.

    One section per thing, one row per line, a blank line between sections. This
    is read on a phone: a paragraph of twelve semicolon-joined figures is not a
    reply, it is a wall.
    """
    notes = _written_total_echo(notes, book)
    held = _held_cells(disagreements)
    sections: list[str] = []

    header = book.shop.strip() or "Your stock book"
    if book.date.strip():
        header += f", {book.date.strip()}"
    sections.append(f"{header}. I read {len(book.rows)} items.")

    if report.item_sales:
        sales = ["Sales from your prices:"]
        sales += [
            f"{s.item} {s.sold} x {naira(int(s.price_naira * 100))} = "
            f"{naira(s.sales_minor)}"
            for s in report.item_sales
        ]
        sections.append("\n".join(sales))

        totals = [f"Total from your prices: {naira(report.computed_total_minor)}."]
        if report.written_total_minor is not None:
            totals.append(f"You wrote {naira(report.written_total_minor)}.")
            totals.append(f"Gap {naira(report.gap_minor)}.")
        sections.append("\n".join(totals))

    failed_rows = [
        (c.name.split(":", 1)[1], c.message)
        for c in report.checks
        if not c.passed
        and c.name.startswith("stock_left:")
        and not _is_held(c.name.split(":", 1)[1], held)
    ]
    if failed_rows:
        rows = ["Rows that do not add up:"]
        rows += [f"{name} ({message})." for name, message in failed_rows]
        sections.append("\n".join(rows))

    # The written/computed gap is already stated above; repeating the check
    # message would print the same three figures twice in one reply.
    sales_only_failures = [
        c
        for c in report.checks
        if not c.passed
        and not c.name.startswith("stock_left:")
        and c.name != "written_total_vs_computed"
    ]
    for check in sales_only_failures:
        if check.message:
            sections.append(check.message + ".")

    low_stock = [s for s in report.low_stock if not _held_cell_line(s, held)]
    if low_stock:
        sections.append("Low stock: " + _short_join(low_stock) + ".")
    if report.crossed_out:
        sections.append("Crossed out: " + _short_join(report.crossed_out) + ".")

    note_lines = _note_lines(notes)
    if note_lines:
        sections.append("\n".join(["Notes from your page:"] + note_lines))

    questions = _questions(
        low_confidence=report.low_confidence,
        failed_rows=failed_rows,
        unclear=report.unclear,
        needs_amount=[n.text for n in notes if n.needs_amount],
    )
    questions += [_disagreement_question(d) for d in disagreements if d.label.strip()]
    if questions:
        confirm = ["Please confirm:"]
        confirm += [q if q.endswith("?") else q + "." for q in questions]
        sections.append("\n".join(confirm))

    sections.append(_trust_line(trust_label))
    return "\n\n".join(sections)


def supplier_reply(
    doc: SupplierDocument,
    report: InvoiceReport,
    *,
    trust_label: str = TRUST_SUPPLIER_DOCUMENT,
    matched_supply: str | None = None,
) -> str:
    """The whole invoice or delivery-note reply, from checked data only."""
    kind = "Delivery note" if doc.doc_kind == "delivery_note" else "Invoice"
    header = kind
    if doc.supplier.strip():
        header += f" from {doc.supplier.strip()}"
    if doc.number.strip():
        header += f" {doc.number.strip()}"
    if doc.date.strip():
        header += f", {doc.date.strip()}"
    sections: list[str] = [header + "."]

    if doc.lines:
        sections.append(f"I read {len(doc.lines)} lines.")

    if report.line_mismatches:
        mismatches = ["Lines that do not match:"]
        mismatches += [f"{m}." for m in report.line_mismatches]
        sections.append("\n".join(mismatches))

    subtotal: list[str] = []
    if doc.lines:
        correct = naira(report.computed_subtotal_minor)
        subtotal.append(f"Correct lines total: {correct}.")
    if report.printed_subtotal_minor is not None:
        printed = naira(report.printed_subtotal_minor)
        subtotal.append(f"Printed subtotal: {printed}.")
        if report.subtotal_delta_minor:
            subtotal.append(
                f"The printed subtotal is overstated by "
                f"{naira(report.subtotal_delta_minor)}."
            )
    if subtotal:
        sections.append("\n".join(subtotal))

    for check in report.checks:
        if not check.passed and check.name == "discount_and_total" and check.message:
            sections.append(check.message + ".")

    money: list[str] = []
    if doc.discount_naira is not None:
        money.append(f"Discount {naira(int(Decimal(doc.discount_naira) * 100))}.")
    if doc.total_naira is not None:
        money.append(f"Total to pay {naira(int(Decimal(doc.total_naira) * 100))}.")
    if doc.paid_naira is not None:
        money.append(f"Paid {naira(int(Decimal(doc.paid_naira) * 100))}.")
    if doc.balance_naira is not None:
        due = f" due {doc.due_date.strip()}" if doc.due_date.strip() else ""
        money.append(f"Balance {naira(int(Decimal(doc.balance_naira) * 100))}{due}.")
    if money:
        sections.append("\n".join(money))

    if doc.notes:
        page_notes = ["Notes from your page:"]
        page_notes += [_page_line(n) for n in doc.notes[:4]]
        sections.append("\n".join(page_notes))

    if matched_supply:
        sections.append(f"Matched to your stock book: {matched_supply}.")
    else:
        sections.append(
            "No matching stock-book supply line yet, so the quantity stays "
            "unverified until the book shows it."
        )

    sections.append(_trust_line(trust_label))
    return "\n\n".join(sections)


def retake_reply() -> str:
    """The one fail-closed message for a timeout, error or unreadable page."""
    return (
        "I couldn't read that page. Try a clearer photo, top down, with the "
        "whole table in view."
    )


__all__ = [
    "TRUST_SELF_REPORTED",
    "TRUST_SUPPLIER_DOCUMENT",
    "retake_reply",
    "stock_book_reply",
    "supplier_reply",
]

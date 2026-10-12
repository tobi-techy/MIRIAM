"""Reply templates. Built from checked data, never from model prose.

Every figure that reaches the owner here was computed in ``shop.checks`` from
digits the model read. The template cannot invent a number because it has no
generator: it only formats values that already passed a check. Texting style,
short lines, and no em dashes anywhere.
"""

from __future__ import annotations

from decimal import Decimal

from miriam_agent.shop.checks import (
    InvoiceReport,
    StockBookReport,
    naira,
)
from miriam_agent.shop.notes import NoteEvaluation
from miriam_agent.shop.schemas import NoteKind, StockBook, SupplierDocument

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


def _short_join(items: list[str], limit: int = 8) -> str:
    shown = items[:limit]
    text = "; ".join(shown)
    if len(items) > limit:
        text += f"; and {len(items) - limit} more"
    return text


def stock_book_reply(
    book: StockBook,
    report: StockBookReport,
    notes: list[NoteEvaluation],
    *,
    trust_label: str = TRUST_SELF_REPORTED,
) -> str:
    """The whole stock-book reply, from checked data only."""
    lines: list[str] = []
    header = book.shop.strip() or "Your stock book"
    if book.date.strip():
        header += f", {book.date.strip()}"
    lines.append(f"{header}. I read {len(book.rows)} items.")

    if report.item_sales:
        sales = [
            f"{s.item} {s.sold} x {naira(int(s.price_naira * 100))} = "
            f"{naira(s.sales_minor)}"
            for s in report.item_sales
        ]
        lines.append("Sales from your prices: " + _short_join(sales) + ".")
        lines.append(f"Total from your prices: {naira(report.computed_total_minor)}.")
        if report.written_total_minor is not None:
            lines.append(
                f"You wrote {naira(report.written_total_minor)}. "
                f"Gap {naira(report.gap_minor)}."
            )

    failed_rows = [
        (c.name.split(":", 1)[1], c.message)
        for c in report.checks
        if not c.passed and c.name.startswith("stock_left:")
    ]
    if failed_rows:
        lines.append(
            "Rows that do not add up: "
            + _short_join([f"{n} ({m})" for n, m in failed_rows])
            + "."
        )

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
            lines.append(check.message + ".")

    if report.low_stock:
        lines.append("Low stock: " + _short_join(report.low_stock) + ".")
    if report.crossed_out:
        lines.append("Crossed out: " + _short_join(report.crossed_out) + ".")

    understood = [_note_line(n) for n in notes if not n.unclassified]
    if understood:
        lines.append("Notes: " + _short_join(understood, limit=6) + ".")
    for note in notes:
        if note.unclassified:
            lines.append(f"I could not classify one note: {note.text}")

    questions = _questions(
        low_confidence=report.low_confidence,
        failed_rows=failed_rows,
        unclear=report.unclear,
        needs_amount=[n.text for n in notes if n.needs_amount],
    )
    if questions:
        lines.append("Please confirm: " + _short_join(questions, limit=6) + ".")

    lines.append(_trust_line(trust_label))
    return "\n".join(lines)


def supplier_reply(
    doc: SupplierDocument,
    report: InvoiceReport,
    *,
    trust_label: str = TRUST_SUPPLIER_DOCUMENT,
    matched_supply: str | None = None,
) -> str:
    """The whole invoice or delivery-note reply, from checked data only."""
    lines: list[str] = []
    kind = "Delivery note" if doc.doc_kind == "delivery_note" else "Invoice"
    header = kind
    if doc.supplier.strip():
        header += f" from {doc.supplier.strip()}"
    if doc.number.strip():
        header += f" {doc.number.strip()}"
    if doc.date.strip():
        header += f", {doc.date.strip()}"
    lines.append(header + ".")

    if doc.lines:
        lines.append(f"I read {len(doc.lines)} lines.")

    if report.line_mismatches:
        lines.append(
            "Lines that do not match: " + _short_join(report.line_mismatches) + "."
        )

    if doc.lines:
        correct_total = naira(report.computed_subtotal_minor)
        lines.append(f"Correct lines total: {correct_total}.")
    if report.printed_subtotal_minor is not None:
        lines.append(f"Printed subtotal: {naira(report.printed_subtotal_minor)}.")
        if report.subtotal_delta_minor:
            lines.append(
                f"The printed subtotal is overstated by "
                f"{naira(report.subtotal_delta_minor)}."
            )
    for check in report.checks:
        if not check.passed and check.name == "discount_and_total" and check.message:
            lines.append(check.message + ".")

    if doc.discount_naira is not None:
        lines.append(f"Discount {naira(int(Decimal(doc.discount_naira) * 100))}.")
    if doc.total_naira is not None:
        lines.append(f"Total to pay {naira(int(Decimal(doc.total_naira) * 100))}.")
    if doc.paid_naira is not None:
        lines.append(f"Paid {naira(int(Decimal(doc.paid_naira) * 100))}.")
    if doc.balance_naira is not None:
        due = f" due {doc.due_date.strip()}" if doc.due_date.strip() else ""
        lines.append(f"Balance {naira(int(Decimal(doc.balance_naira) * 100))}{due}.")

    if doc.notes:
        lines.append("Notes: " + _short_join(doc.notes, limit=4) + ".")

    if matched_supply:
        lines.append(f"Matched to your stock book: {matched_supply}.")
    else:
        lines.append(
            "No matching stock-book supply line yet, so the quantity stays "
            "unverified until the book shows it."
        )

    lines.append(_trust_line(trust_label))
    return "\n".join(lines)


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

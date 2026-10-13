"""Deterministic checks for shop reads. Plain Python, no model.

Every naira figure is computed in kobo via
:func:`miriam_agent.hands.payment_refs.amount_to_minor`. The vision model copies
what is written; this module is what decides whether the copy adds up. Nothing
here calls a model, and a number that arrives here was read, never invented.

Outputs are report objects the reply template and the Jev quality judge both
consume, so the reply and the escalate decision read the same truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from miriam_agent.hands.payment_refs import amount_to_minor
from miriam_agent.shop.schemas import StockBook, StockRow, SupplierDocument

# Stock at or below this is called out as low. A shop rule of thumb, not a
# policy number: it is what the owner asks to be told about.
LOW_STOCK_MAX = 7
# A row below this confidence is asked about, never quietly accepted.
LOW_CONFIDENCE = 0.7


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    message: str = ""


@dataclass(frozen=True)
class ItemSale:
    item: str
    sold: int
    price_naira: Decimal
    sales_minor: int


@dataclass(frozen=True)
class StockBookReport:
    checks: list[Check] = field(default_factory=list)
    item_sales: list[ItemSale] = field(default_factory=list)
    computed_total_minor: int = 0
    written_total_minor: int | None = None
    gap_minor: int = 0
    low_stock: list[str] = field(default_factory=list)
    crossed_out: list[str] = field(default_factory=list)
    unclear: list[str] = field(default_factory=list)
    low_confidence: list[str] = field(default_factory=list)
    # Rows the page left without an opening balance and no earlier row of the
    # same item to carry one from. Not failures: questions.
    missing_opening: list[str] = field(default_factory=list)
    rows_failing: int = 0
    flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InvoiceReport:
    checks: list[Check] = field(default_factory=list)
    line_mismatches: list[str] = field(default_factory=list)
    computed_subtotal_minor: int = 0
    printed_subtotal_minor: int | None = None
    subtotal_delta_minor: int = 0
    flags: list[str] = field(default_factory=list)


# One currency symbol, everywhere a figure of ours is printed. The naira sign
# travels as UTF-8 through JSON end to end; if a channel ever mangles it, flip
# this constant and every reply follows in the same commit.
CURRENCY_SYMBOL = "₦"


def naira(minor: int) -> str:
    """Render kobo as a naira string, e.g. ``₦286,300``."""
    sign = "-" if minor < 0 else ""
    naira_value = abs(minor) // 100
    remainder = abs(minor) % 100
    text = f"{naira_value:,}"
    if remainder:
        text = f"{text}.{remainder:02d}"
    return f"{sign}{CURRENCY_SYMBOL}{text}"


def _minor(value: str | int | None) -> int:
    if value is None or value == "":
        return 0
    return amount_to_minor(value)


def _row_cell_unclear(row: StockRow, cell: str) -> bool:
    return cell in {u.strip().lower() for u in row.unclear}


def _norm_item(item: str) -> str:
    return " ".join(item.split()).casefold()


def same_item(a: str, b: str) -> bool:
    """Item names match on case and spacing only.

    "Indomie (Chicken)" must never match "Indomie (Curry)": a carried opening
    from a different pack size is a made-up number with extra steps, which is
    exactly the failure this rule exists to prevent.
    """
    return _norm_item(a) == _norm_item(b)


def check_stock_book(book: StockBook) -> StockBookReport:
    """Opening + supplied - sold = left per row, and sold x price vs the total."""
    checks: list[Check] = []
    item_sales: list[ItemSale] = []
    low_stock: list[str] = []
    crossed_out: list[str] = []
    unclear: list[str] = []
    low_confidence: list[str] = []
    missing_opening: list[str] = []
    rows_failing = 0
    # Closing balances already read on this page, by item. A row with no opening
    # balance takes the latest earlier closing of the *same* item: the page's own
    # record carried forward by code, never by the model.
    earlier_closing: dict[str, int] = {}

    for idx, row in enumerate(book.rows, start=1):
        label = row.item.strip() or f"row {idx}"
        opening = row.opening
        if opening is None and row.left is not None:
            opening = earlier_closing.get(_norm_item(label))
        if (
            opening is not None
            and row.supplied is not None
            and row.sold is not None
            and row.left is not None
        ):
            expected = opening + row.supplied - row.sold
            ok = expected == row.left
            checks.append(
                Check(
                    name=f"stock_left:{label}",
                    passed=ok,
                    message=(
                        ""
                        if ok
                        else f"{opening} + {row.supplied} - {row.sold} "
                        f"= {expected}, book says {row.left}"
                    ),
                )
            )
            if not ok:
                rows_failing += 1
        elif row.opening is None and (
            row.supplied is not None or row.sold is not None or row.left is not None
        ):
            # No opening on the page and no earlier row of this item to carry
            # one from, so the arithmetic cannot be checked at all. The row is
            # not wrong, it is unanswerable, and it becomes a question.
            missing_opening.append(label)
        if row.opening is not None and row.left is not None:
            earlier_closing[_norm_item(label)] = row.left
        if row.price_naira is not None and row.sold is not None:
            price = Decimal(row.price_naira)
            sales = amount_to_minor(row.price_naira) * row.sold
            item_sales.append(
                ItemSale(
                    item=label, sold=row.sold, price_naira=price, sales_minor=sales
                )
            )
        if row.left is not None and row.left <= LOW_STOCK_MAX:
            low_stock.append(f"{label} ({row.left} left)")
        if row.crossed_out is not None:
            old = row.crossed_out.old or "?"
            new = row.crossed_out.new or "?"
            crossed_out.append(f"{label}: {old} changed to {new}")
            checks.append(
                Check(
                    name=f"crossed_out:{label}",
                    passed=True,
                    message=f"{old} changed to {new}",
                )
            )
        row_unclear = [c for c in row.unclear if c.strip()]
        if row_unclear:
            unclear.append(f"{label}: {', '.join(row_unclear)}")
        if row.confidence and row.confidence < LOW_CONFIDENCE:
            low_confidence.append(f"{label} ({row.confidence:.2f})")

    computed_total_minor = sum(s.sales_minor for s in item_sales)
    written_total_minor = (
        _minor(book.written_total_sales_naira)
        if book.written_total_sales_naira is not None
        else None
    )
    gap_minor = 0
    if written_total_minor is not None:
        gap_minor = computed_total_minor - written_total_minor
        checks.append(
            Check(
                name="written_total_vs_computed",
                passed=gap_minor == 0,
                message=(
                    ""
                    if gap_minor == 0
                    else f"written {naira(written_total_minor)}, computed "
                    f"{naira(computed_total_minor)}, gap {naira(gap_minor)}"
                ),
            )
        )

    flags: list[str] = []
    if rows_failing:
        flags.append(f"{rows_failing} row(s) do not add up")
    if gap_minor:
        flags.append(f"total gap {naira(gap_minor)}")
    if low_stock:
        flags.append("low stock")
    if crossed_out:
        flags.append("crossed out values")
    if unclear:
        flags.append("unclear cells")
    if low_confidence:
        flags.append("low confidence rows")
    if missing_opening:
        flags.append(f"{len(missing_opening)} row(s) with no opening balance")

    return StockBookReport(
        checks=checks,
        item_sales=item_sales,
        missing_opening=missing_opening,
        computed_total_minor=computed_total_minor,
        written_total_minor=written_total_minor,
        gap_minor=gap_minor,
        low_stock=low_stock,
        crossed_out=crossed_out,
        unclear=unclear,
        low_confidence=low_confidence,
        rows_failing=rows_failing,
        flags=flags,
    )


def check_supplier_document(doc: SupplierDocument) -> InvoiceReport:
    """qty x unit = line total, lines sum to subtotal, subtotal - discount = total."""
    checks: list[Check] = []
    line_mismatches: list[str] = []
    computed_subtotal = 0

    for idx, line in enumerate(doc.lines, start=1):
        label = line.item.strip() or f"line {idx}"
        if line.qty is not None and line.unit_price_naira is not None:
            correct = amount_to_minor(line.unit_price_naira) * line.qty
            computed_subtotal += correct
            unit_text = naira(amount_to_minor(line.unit_price_naira))
            if line.line_total_naira is not None:
                printed = _minor(line.line_total_naira)
                ok = printed == correct
                checks.append(
                    Check(
                        name=f"line_total:{label}",
                        passed=ok,
                        message=(
                            ""
                            if ok
                            else f"{line.qty} x {unit_text} = "
                            f"{naira(correct)}, printed {naira(printed)}"
                        ),
                    )
                )
                if not ok:
                    line_mismatches.append(
                        f"{label}: {line.qty} x {unit_text} = "
                        f"{naira(correct)}, printed {naira(printed)}, "
                        f"mismatch {naira(printed - correct)}"
                    )

    printed_subtotal_minor: int | None = None
    subtotal_delta = 0
    if doc.subtotal_naira is not None:
        printed_subtotal_minor = _minor(doc.subtotal_naira)
        subtotal_delta = printed_subtotal_minor - computed_subtotal
        checks.append(
            Check(
                name="lines_vs_subtotal",
                passed=subtotal_delta == 0,
                message=(
                    ""
                    if subtotal_delta == 0
                    else f"printed subtotal {naira(printed_subtotal_minor)} "
                    f"overstated by {naira(subtotal_delta)} against correct "
                    f"{naira(computed_subtotal)}"
                ),
            )
        )

    if (
        doc.subtotal_naira is not None
        and doc.discount_naira is not None
        and (doc.total_naira is not None)
    ):
        expected_total = _minor(doc.subtotal_naira) - _minor(doc.discount_naira)
        printed_total = _minor(doc.total_naira)
        ok = expected_total == printed_total
        checks.append(
            Check(
                name="discount_and_total",
                passed=ok,
                message=(
                    ""
                    if ok
                    else f"subtotal - discount = {naira(expected_total)}, "
                    f"printed total {naira(printed_total)}"
                ),
            )
        )

    flags: list[str] = []
    if line_mismatches:
        flags.append(f"{len(line_mismatches)} line(s) mis-priced")
    if subtotal_delta:
        flags.append(f"subtotal overstated {naira(subtotal_delta)}")

    return InvoiceReport(
        checks=checks,
        line_mismatches=line_mismatches,
        computed_subtotal_minor=computed_subtotal,
        printed_subtotal_minor=printed_subtotal_minor,
        subtotal_delta_minor=subtotal_delta,
        flags=flags,
    )


def checks_as_state(checks: list[Check]) -> list[dict[str, object]]:
    """The check list as a JSON-safe payload for the Jev quality question."""
    return [
        {"name": c.name, "passed": c.passed, "message": c.message}
        for c in checks
        if not c.passed
    ]


__all__ = [
    "LOW_CONFIDENCE",
    "LOW_STOCK_MAX",
    "Check",
    "InvoiceReport",
    "ItemSale",
    "StockBookReport",
    "check_stock_book",
    "check_supplier_document",
    "checks_as_state",
    "naira",
]

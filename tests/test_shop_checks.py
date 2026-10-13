"""Unit tests for the shop checks. Plain Python, no model, no network."""

from __future__ import annotations

from decimal import Decimal

from shop_fakes import mama_tobi_book, wigmore_invoice

from miriam_agent.hands.payment_refs import amount_to_minor
from miriam_agent.shop.checks import (
    check_stock_book,
    check_supplier_document,
    checks_as_state,
    naira,
)
from miriam_agent.shop.schemas import (
    InvoiceLine,
    StockBook,
    StockRow,
    SupplierDocument,
)


def test_naira_renders_plain_naira() -> None:
    assert naira(254_400_00) == "₦254,400"
    assert naira(31_900_00) == "₦31,900"
    assert naira(0) == "₦0"


def test_money_goes_through_amount_to_minor() -> None:
    assert amount_to_minor("254400") == 254_400_00
    assert amount_to_minor(Decimal("3800")) == 3_800_00


def test_stock_book_total_gap_matches_the_page() -> None:
    report = check_stock_book(mama_tobi_book())
    assert report.computed_total_minor == 286_300_00
    assert report.written_total_minor == 254_400_00
    assert report.gap_minor == 31_900_00
    assert report.rows_failing == 0
    assert "total gap ₦31,900" in report.flags

    gap_check = next(c for c in report.checks if c.name == "written_total_vs_computed")
    assert gap_check.passed is False
    assert gap_check.message == ("written ₦254,400, computed ₦286,300, gap ₦31,900")


def test_stock_left_is_opening_plus_supplied_minus_sold() -> None:
    book = StockBook.model_validate(
        {
            "shop": "Test",
            "rows": [
                {
                    "item": "Rice",
                    "opening": 10,
                    "supplied": 5,
                    "sold": 12,
                    "left": 3,
                    "price_naira": "1000",
                    "confidence": 1.0,
                }
            ],
        }
    )
    report = check_stock_book(book)
    assert report.rows_failing == 0
    assert report.computed_total_minor == 12_000_00


def test_stock_left_mismatch_is_reported_not_corrected() -> None:
    book = StockBook.model_validate(
        {
            "rows": [
                {
                    "item": "Beans",
                    "opening": 10,
                    "supplied": 5,
                    "sold": 12,
                    "left": 4,
                    "price_naira": "500",
                    "confidence": 1.0,
                }
            ]
        }
    )
    report = check_stock_book(book)
    assert report.rows_failing == 1
    check = next(c for c in report.checks if c.name == "stock_left:Beans")
    assert check.message == "10 + 5 - 12 = 3, book says 4"
    assert "1 row(s) do not add up" in report.flags


def test_low_stock_crossed_out_and_unclear_are_reported() -> None:
    book = StockBook.model_validate(
        {
            "rows": [
                {
                    "item": "Sugar",
                    "opening": 2,
                    "supplied": 0,
                    "sold": 0,
                    "left": 2,
                    "price_naira": "1200",
                    "crossed_out": {"old": "1200", "new": "1500"},
                    "unclear": ["left"],
                    "confidence": 0.4,
                }
            ]
        }
    )
    report = check_stock_book(book)
    assert report.low_stock == ["Sugar (2 left)"]
    assert report.crossed_out == ["Sugar: 1200 changed to 1500"]
    assert report.unclear == ["Sugar: left"]
    assert report.low_confidence == ["Sugar (0.40)"]
    assert report.flags == [
        "low stock",
        "crossed out values",
        "unclear cells",
        "low confidence rows",
    ]


def test_invoice_finds_the_milo_line_and_overstated_subtotal() -> None:
    report = check_supplier_document(wigmore_invoice())
    assert report.computed_subtotal_minor == 180_600_00
    assert report.printed_subtotal_minor == 182_600_00
    assert report.subtotal_delta_minor == 2_000_00
    assert len(report.line_mismatches) == 1
    assert report.line_mismatches[0] == (
        "Milo 400g refill: 6 x ₦3,800 = ₦22,800, printed ₦24,800, " "mismatch ₦2,000"
    )
    assert "1 line(s) mis-priced" in report.flags
    assert "subtotal overstated ₦2,000" in report.flags

    discount_check = next(c for c in report.checks if c.name == "discount_and_total")
    assert discount_check.passed is True


def test_discount_and_total_mismatch_is_reported() -> None:
    doc = SupplierDocument.model_validate(
        {
            "lines": [
                {
                    "item": "Coke crate",
                    "qty": 10,
                    "unit_price_naira": "5000",
                    "line_total_naira": "50000",
                    "confidence": 1.0,
                }
            ],
            "subtotal_naira": "50000",
            "discount_naira": "2000",
            "total_naira": "50000",
        }
    )
    report = check_supplier_document(doc)
    check = next(c for c in report.checks if c.name == "discount_and_total")
    assert check.passed is False
    assert check.message == "subtotal - discount = ₦48,000, printed total ₦50,000"


def test_a_discount_and_payment_printed_with_a_minus_stay_positive() -> None:
    """A live read of the invoice returned "-2,600" and "-150,000".

    The minus is the column saying "subtracted", and keeping it made the reply
    read "Discount -₦2,600" and made the check compute subtotal - (-discount),
    which invented a ₦5,200 mismatch on an invoice whose total was right.
    """
    doc = wigmore_invoice(discount_naira="-2,600", paid_naira="-150,000")
    assert doc.discount_naira == "2600"
    assert doc.paid_naira == "150000"

    report = check_supplier_document(doc)
    check = next(c for c in report.checks if c.name == "discount_and_total")
    assert check.passed is True
    assert report.subtotal_delta_minor == 2_000_00


def test_a_negative_balance_is_left_alone_because_it_means_something() -> None:
    doc = wigmore_invoice(balance_naira="-500")
    assert doc.balance_naira == "-500"


def test_missing_line_total_is_not_counted_as_matching() -> None:
    doc = SupplierDocument.model_validate(
        {
            "lines": [
                {
                    "item": "Peak milk",
                    "qty": 24,
                    "unit_price_naira": "400",
                    "confidence": 1.0,
                }
            ]
        }
    )
    report = check_supplier_document(doc)
    assert report.computed_subtotal_minor == 9_600_00
    assert report.line_mismatches == []
    assert report.printed_subtotal_minor is None


def test_unclear_invoice_line_is_carried_on_the_line() -> None:
    doc = SupplierDocument.model_validate(
        {
            "lines": [
                InvoiceLine(
                    item="Kings oil",
                    qty=12,
                    unit_price_naira="3100",
                    line_total_naira="37200",
                    unclear=["unit_price_naira"],
                    confidence=0.5,
                )
            ]
        }
    )
    report = check_supplier_document(doc)
    assert report.line_mismatches == []
    assert doc.lines[0].unclear == ["unit_price_naira"]


def test_checks_as_state_keeps_only_failures() -> None:
    state = checks_as_state(check_stock_book(mama_tobi_book()).checks)
    assert [c["name"] for c in state] == ["written_total_vs_computed"]
    assert state[0]["passed"] is False


def test_empty_stock_book_reports_nothing() -> None:
    report = check_stock_book(StockBook())
    assert report.checks == []
    assert report.computed_total_minor == 0
    assert report.flags == []


def test_stock_row_without_price_is_read_but_not_counted() -> None:
    book = StockBook.model_validate(
        {"rows": [StockRow(item="Unpriced", opening=1, supplied=0, sold=0, left=1)]}
    )
    report = check_stock_book(book)
    assert report.item_sales == []
    assert report.computed_total_minor == 0

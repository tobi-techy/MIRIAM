"""Shop reading: typed extraction schemas.

These are the wire schemas for the vision passes and the shape persisted for a
read. Money is a decimal string everywhere (kobo maths happens in
``shop.checks`` via ``amount_to_minor``); a float never touches a naira value.

Every model forbids extra keys so a model that pads its answer with a helpful
sentence in a stray field fails validation instead of being half-read.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DocType(StrEnum):
    """The document kinds the shop endpoint can return."""

    STOCK_BOOK = "stock_book"
    INVOICE = "invoice"
    DELIVERY_NOTE = "delivery_note"
    RECEIPT = "receipt"
    BANK_STATEMENT = "bank_statement"
    OTHER = "other"


class NoteKind(StrEnum):
    CUSTOMER_DEBT = "customer_debt"
    PAYMENT_OUT = "payment_out"
    PAYMENT_IN_UNVERIFIED = "payment_in_unverified"
    SUPPLIER_DELIVERY = "supplier_delivery"
    PRICE_CHANGE = "price_change"
    OTHER = "other"


def _money_is_decimal_string(v: Any) -> str | None:
    """Accept a decimal string (or a bare int/float) and return a string."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError(f"money field must be a decimal string, got {v!r}")
    text = str(v).replace(",", "").strip()
    if text == "":
        return None
    try:
        Decimal(text)
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ValueError(f"money field must be a decimal string, got {v!r}") from e
    return text


def _deduction_is_positive(v: Any) -> str | None:
    """A discount or a payment the invoice prints as "-2,600" is still 2,600.

    The minus is the invoice column saying "this is subtracted", not a negative
    amount, and the field name already carries the direction. Keeping the sign
    made the reply read "Discount -N2,600" and, worse, sent the check to
    subtotal - (-discount), which reported a N5,200 total mismatch on an invoice
    whose total was right.
    """
    text = _money_is_decimal_string(v)
    if text is None:
        return None
    return text.lstrip("-+").strip()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Disagreement:
    """One cell the two reads of a page did not agree on.

    The extraction pass and the verify pass are two independent reads of the
    same image. When they name different figures for one cell, the figure is
    not a fact and the reply must not print it: it goes to the owner as a
    question with both candidates. Nothing is averaged, nothing is guessed.
    """

    path: str
    cell: str
    label: str
    first: str
    second: str


def cell_words(cell: str) -> str:
    """The cell name as the owner's words, for a question in the reply."""
    words = {
        "left": "left",
        "opening": "opening",
        "supplied": "supplied",
        "sold": "sold",
        "price_naira": "price",
        "qty": "quantity",
        "unit_price_naira": "unit price",
        "line_total_naira": "line total",
        "printed_total_naira": "printed line total",
        "discount_naira": "discount",
        "total_naira": "total",
        "paid_naira": "paid",
        "balance_naira": "balance",
    }
    return words.get(cell, cell.removesuffix("_naira").replace("_", " "))


class CrossedOut(_Strict):
    """A corrected cell: the struck-through value and the one written beside it."""

    old: str | None = None
    new: str | None = None


class StockRow(_Strict):
    item: str = ""
    # The block, month or date range this row sits under, when the page holds
    # more than one ("October", "01/11-29/11"). Empty on a single-block page.
    # The checks use it to ask the owner about the right period, and the reply
    # to name every period the page covers.
    block: str = ""
    # Null when the page has no opening figure for this row. A missing opening
    # is never filled in: an invented opening is an invented failure.
    opening: int | None = None
    supplied: int | None = None
    sold: int | None = None
    left: int | None = None
    price_naira: str | None = None
    crossed_out: CrossedOut | None = None
    # Names of the cells the model is unsure about, e.g. ["sold", "left"].
    unclear: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    _price = field_validator("price_naira", mode="before")(_money_is_decimal_string)

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"confidence {v!r} out of range [0,1]")
        return v


class ParsedNote(_Strict):
    kind: NoteKind = NoteKind.OTHER
    party: str = ""
    amount_naira: str | None = None
    due: str | None = None
    text: str = ""

    _amount = field_validator("amount_naira", mode="before")(_money_is_decimal_string)


class StockBook(_Strict):
    """A handwritten shop stock page."""

    shop: str = ""
    date: str = ""
    rows: list[StockRow] = Field(default_factory=list)
    written_total_sales_naira: str | None = None
    notes: list[str] = Field(default_factory=list)
    parsed_notes: list[ParsedNote] = Field(default_factory=list)

    _written_total = field_validator("written_total_sales_naira", mode="before")(
        _money_is_decimal_string
    )


class InvoiceLine(_Strict):
    item: str = ""
    qty: int | None = None
    unit_price_naira: str | None = None
    line_total_naira: str | None = None
    unclear: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    _unit = field_validator("unit_price_naira", mode="before")(_money_is_decimal_string)
    _total = field_validator("line_total_naira", mode="before")(
        _money_is_decimal_string
    )


class SupplierDocument(_Strict):
    """A distributor invoice or delivery note."""

    doc_kind: Literal["invoice", "delivery_note"] = "invoice"
    supplier: str = ""
    number: str = ""
    date: str = ""
    buyer: str = ""
    lines: list[InvoiceLine] = Field(default_factory=list)
    subtotal_naira: str | None = None
    discount_naira: str | None = None
    total_naira: str | None = None
    paid_naira: str | None = None
    balance_naira: str | None = None
    due_date: str = ""
    payment_terms: str = ""
    notes: list[str] = Field(default_factory=list)

    _sub = field_validator("subtotal_naira", mode="before")(_money_is_decimal_string)
    _disc = field_validator("discount_naira", mode="before")(_deduction_is_positive)
    _tot = field_validator("total_naira", mode="before")(_money_is_decimal_string)
    _paid = field_validator("paid_naira", mode="before")(_deduction_is_positive)
    _bal = field_validator("balance_naira", mode="before")(_money_is_decimal_string)


# The schema each document kind is read with.
SCHEMA_BY_KIND: dict[str, type[BaseModel]] = {
    DocType.STOCK_BOOK.value: StockBook,
    DocType.INVOICE.value: SupplierDocument,
    DocType.DELIVERY_NOTE.value: SupplierDocument,
}


# A generic shape for "other"/receipt/statement so the model still returns
# valid JSON we can discard safely rather than erroring on a schema mismatch.
class OtherDocument(_Strict):
    summary: str = ""
    notes: list[str] = Field(default_factory=list)


__all__ = [
    "SCHEMA_BY_KIND",
    "CrossedOut",
    "DocType",
    "InvoiceLine",
    "NoteKind",
    "OtherDocument",
    "ParsedNote",
    "StockBook",
    "StockRow",
    "SupplierDocument",
]

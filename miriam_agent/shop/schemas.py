"""Shop reading: typed extraction schemas.

These are the wire schemas for the vision passes and the shape persisted for a
read. Money is a decimal string everywhere (kobo maths happens in
``shop.checks`` via ``amount_to_minor``); a float never touches a naira value.

Every model forbids extra keys so a model that pads its answer with a helpful
sentence in a stray field fails validation instead of being half-read.
"""

from __future__ import annotations

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


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CrossedOut(_Strict):
    """A corrected cell: the struck-through value and the one written beside it."""

    old: str | None = None
    new: str | None = None


class StockRow(_Strict):
    item: str = ""
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
    _disc = field_validator("discount_naira", mode="before")(_money_is_decimal_string)
    _tot = field_validator("total_naira", mode="before")(_money_is_decimal_string)
    _paid = field_validator("paid_naira", mode="before")(_money_is_decimal_string)
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

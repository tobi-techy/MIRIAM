"""Money and percentage formatting shared by the engine output.

One place decides how an amount becomes text, so the blunt diagnosis sentence
and the rendered plan agree. Amounts are whole units of currency: Miriam says
"₦50,000", never "₦49,999.37".
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_SYMBOLS: dict[str, str] = {
    "NGN": "\u20a6",
    "USD": "$",
    "GBP": "\u00a3",
    "EUR": "\u20ac",
    "KES": "KSh ",
    "GHS": "GH\u20b5",
    "ZAR": "R",
}

# Decimal places for the symbol form; NGN/USD etc. are conventionally whole.
_DECIMALS: dict[str, int] = {}


def symbol(currency: str) -> str:
    """The display symbol for a currency, or its code when we have no symbol."""
    code = (currency or "").upper()
    return _SYMBOLS.get(code, f"{code} " if code else "")


def _round_amount(amount: Decimal, currency: str) -> Decimal:
    places = _DECIMALS.get((currency or "").upper(), 0)
    quant = Decimal(1).scaleb(-places)
    return amount.quantize(quant, rounding=ROUND_HALF_UP)


def format_amount(amount: Decimal | int | float | None, currency: str = "") -> str:
    """``500000`` -> ``"₦500,000"``. ``None`` -> ``"—"``.

    Rounding happens here, at the edge, so engines keep full precision for their
    arithmetic and only the rendered text is whole.
    """
    if amount is None:
        return "\u2014"
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    rounded = _round_amount(value, currency)
    return f"{symbol(currency)}{rounded:,.0f}"


def format_pct(value: Decimal | int | float | None, places: int = 0) -> str:
    """``65`` -> ``"65%"``. ``None`` -> ``"—"``."""
    if value is None:
        return "\u2014"
    shown = value if isinstance(value, Decimal) else Decimal(str(value))
    if places <= 0:
        return f"{shown.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,.0f}%"
    quant = Decimal(1).scaleb(-places)
    return f"{shown.quantize(quant, rounding=ROUND_HALF_UP):,.{places}f}%"


def as_float(value: Decimal | None) -> float:
    """Decimal -> float, for the few fields the schema keeps numeric (shares)."""
    return float(value) if value is not None else 0.0


def format_months(value: Decimal | int | float | None) -> str:
    """``1`` -> ``"1 month"``, ``3`` -> ``"3 months"``."""
    if value is None:
        return "\u2014"
    shown = value if isinstance(value, Decimal) else Decimal(str(value))
    rounded = shown.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    unit = "month" if rounded == 1 else "months"
    return f"{rounded:,.0f} {unit}"


def as_sentence(text: str) -> str:
    """Capitalize and terminate a fragment, so joined reasons read properly."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    cleaned = cleaned[0].upper() + cleaned[1:]
    if not cleaned.endswith((".", "!", "?")):
        cleaned += "."
    return cleaned


def join_sentences(parts: list[str]) -> str:
    """Join fragments into readable sentences instead of a run-on."""
    return " ".join(as_sentence(part) for part in parts if part and part.strip())


def weight_str(weight: Decimal) -> str:
    """A Glider allocation weight: percent, at most 2 decimals, zeros trimmed.

    Glider accepts weights as percent strings that must sum to exactly 100, so
    this is the formatter both the templates and the draft payload use.
    """
    quantized = weight.quantize(Decimal("0.01"))
    text = f"{quantized:.2f}".rstrip("0").rstrip(".")
    return text or "0"

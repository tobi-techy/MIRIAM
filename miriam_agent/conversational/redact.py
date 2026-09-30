"""Memory-safe redaction of money figures from assistant output.

Supermemory carries *facts* about a person, never *balances*
(``docs/MEMORY-ARCHITECTURE.md``). A money turn's narration is full of computed
figures — "you're at ₦720", "sent ₦50,000 to @bisi" — and if those land in the
graph they become durable "facts" that a later turn will quote as *current*,
violating TRUTH RULE #1 (never state a number you can't point to a fresh source).

This module strips money figures from the *assistant* side of the ingest only.
The *user's* words are left verbatim: "I earn ₦250k" is the user's own truth and
is legitimate memory material (income shape is a fact, not a balance). Miriam's
narration is a computed value, so it is redacted to ``[amount]`` / ``[pct]``.

Bare numbers ("3 months", "2 moves") are deliberately left alone: they are
ambiguous between facts and figures, and the money layer always renders a true
balance with a currency mark via ``money/formatting.format_amount``, so the
mark/code forms are the unambiguous leak this module closes.
"""

from __future__ import annotations

import re

# Currency marks the money layer renders (miriam_agent/money/formatting.py
# ``symbol()``): ₦/$/€/£/¥/₹ plus the multi-char GH₵ and KSh, and ZAR's bare "R"
# only when glued to a digit (format_amount emits "R500", never "R 500", so the
# lookahead keeps ordinary "Room"/"R&D" prose from matching).
_CURRENCY_MARK = r"(?:GH₵|KSh|₦|\$|€|£|¥|₹|₵|R(?=\d))"
# Same set, minus bare "R", for the trailing form ("500₦", never "500 R").
_CURRENCY_MARK_POST = r"(?:GH₵|KSh|₦|\$|€|£|¥|₹|₵)"

# ISO codes the money layer may emit alongside a figure (receipts/voice use NGN,
# and other supported currencies can appear in narration).
_CURRENCY_CODE = r"(?:NGN|USD|EUR|GBP|JPY|INR|ZAR|GHS|KES|CAD|AUD|XOF|XAF)"

# A number with optional thousand separators, decimals, and a compact
# magnitude suffix: 50,000 / 50000 / 1,234.56 / 250k / 1.5m.
_NUMBER = r"\d[\d,]*(?:\.\d+)?[kKmMbB]?"

# mark or code either side of a number: ₦50,000 / 50,000₦ / NGN 50,000 / 50,000 NGN
_AMOUNT_MARK_PRE = re.compile(rf"{_CURRENCY_MARK}\s*{_NUMBER}")
_AMOUNT_MARK_POST = re.compile(rf"{_NUMBER}\s*{_CURRENCY_MARK_POST}")
_AMOUNT_CODE_PRE = re.compile(rf"{_CURRENCY_CODE}\s+{_NUMBER}")
_AMOUNT_CODE_POST = re.compile(rf"{_NUMBER}\s+{_CURRENCY_CODE}")

# percentages: 15% / 12.5% / 5 %
_PERCENT = re.compile(rf"{_NUMBER}\s*%")

_PLACEHOLDER_AMOUNT = "[amount]"
_PLACEHOLDER_PCT = "[pct]"


def redact_money_figures(text: str) -> str:
    """Strip money figures, keeping the facts around them.

    Deterministic and idempotent: running it on already-redacted text is a
    no-op. Empty or non-string input is returned unchanged.
    """
    if not text:
        return text
    out = text
    # Mark/code-adjacent amounts first, then percentages, then the bare-code
    # post form. Order matters: a "₦15" must be consumed as an amount before the
    # percent pass could ever split it.
    out = _AMOUNT_MARK_PRE.sub(_PLACEHOLDER_AMOUNT, out)
    out = _AMOUNT_MARK_POST.sub(_PLACEHOLDER_AMOUNT, out)
    out = _AMOUNT_CODE_PRE.sub(_PLACEHOLDER_AMOUNT, out)
    out = _AMOUNT_CODE_POST.sub(_PLACEHOLDER_AMOUNT, out)
    out = _PERCENT.sub(_PLACEHOLDER_PCT, out)
    return out


__all__ = ["redact_money_figures"]

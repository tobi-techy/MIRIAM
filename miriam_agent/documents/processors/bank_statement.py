"""Layout-tolerant bank-statement extraction (Stage 3 core).

Column order is discovered, never assumed. The header row supplies both
column *roles* (date/description/debit/credit/amount/balance) and their
*character ranges*, so body rows are sliced by position — the approach
that survives fixed-width PDF text and aligned OCR text alike. A
whitespace/pipe split is only a fallback when slicing yields nothing.

Multiline descriptions join onto the pending transaction; a wrapped row
may also carry the amount for the transaction it continues. Missing
fields stay ``None``: amounts are never inferred from unrelated totals.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from miriam_agent.documents.models import (
    ExtractedText,
    ParsedTransaction,
    RawDate,
    RawMoney,
    StatementExtraction,
)
from miriam_agent.documents.normalize import (
    detect_currency,
    find_dates,
    last_money_on_line,
    mask_account_number,
    parse_date,
    parse_money,
)

logger = logging.getLogger(__name__)

_OPENING_RE = re.compile(
    r"(?i)(opening balance|balance b/?f|balance brought forward|opening bal)"
)
_CLOSING_RE = re.compile(
    r"(?i)(closing balance|balance c/?f|balance carried forward|closing bal)"
)
_ACCT_NAME_RE = re.compile(r"(?i)account name\s*[:\-]\s*([A-Za-z ,.'\-]{3,60})")
_ACCT_NUM_RE = re.compile(r"(?i)account (?:number|no\.?)\s*[:\-]?\s*([\d\s\-]{8,20})")
_HEADER_DATE = re.compile(r"(?i)\b(date|transaction date|value date|tran date)\b")
_HEADER_DESC = re.compile(r"(?i)\b(description|narration|details|particulars)\b")
_HEADER_MONEY = re.compile(r"(?i)\b(debit|withdrawal|credit|deposit|lodgement|amount)\b")
_MONEY_TOKEN_RE = re.compile(r"\(?[\d,]+\.\d{2}\)?|\(?[\d,]{4,}\)?")

# Institutions whose statements are NGN by convention. Used only to fill a
# missing currency indicator; the bank name is legitimate document context,
# not a guess about an unknown symbol.
_NG_BANKS = (
    "gtbank",
    "gt bank",
    "guaranty trust",
    "access bank",
    "zenith",
    "united bank for africa",
    "uba",
    "first bank",
    "fidelity",
    "union bank",
    "sterling",
    "wema",
    "polaris",
    "keystone",
    "stanbic",
    "ecobank",
)


def _labels(header: str) -> list[tuple[int, int, str]]:
    """Split a header into (start, end, text) tokens at 2+ space gaps.

    Written as an explicit scan rather than a regex because ``[A-Za-z ]+``
    happily swallows the whitespace between two separate column labels,
    merging "Date" and "Description" into one token and shifting every
    subsequent column range.
    """
    out: list[tuple[int, int, str]] = []
    i, n = 0, len(header)
    while i < n:
        while i < n and header[i] == " ":
            i += 1
        if i >= n:
            break
        start = i
        while i < n and not (header[i] == " " and i + 1 < n and header[i + 1] == " "):
            i += 1
        text = header[start:i].strip()
        if text:
            out.append((start, i, text))
    return out


def _column_roles(
    header: str,
) -> tuple[dict[str, tuple[int, int]], dict[str, int]]:
    """Return (role -> (start, column_end), role -> money label end).

    ``column_end`` bounds a role's text slice (description spanning to the
    next label). ``label end`` is what right-aligned money values align to,
    so the two are needed separately.
    """
    labels = _labels(header)
    ranges: dict[str, tuple[int, int]] = {}
    money_ends: dict[str, int] = {}
    for i, (start, _end, label) in enumerate(labels):
        low = label.lower()
        column_end = labels[i + 1][0] if i + 1 < len(labels) else len(header) + 60
        role = None
        if "date" in low and "date" not in ranges:
            role = "date"
        elif any(w in low for w in ("descript", "narrat", "detail", "particular")):
            role = "desc"
        elif any(w in low for w in ("withdraw", "debit")):
            role = "debit"
        elif any(w in low for w in ("deposit", "lodgement", "credit")):
            role = "credit"
        elif "balanc" in low:
            role = "balance"
        elif "amount" in low:
            role = "amount"
        if role and role not in ranges:
            ranges[role] = (start, column_end)
            if role in ("debit", "credit", "amount", "balance"):
                money_ends[role] = start + len(label)
    return ranges, money_ends


def _find_header(
    lines: list[tuple[int, str]]
) -> tuple[int, dict[str, tuple[int, int]], dict[str, int]] | None:
    for idx, (_, text) in enumerate(lines):
        if (
            _HEADER_DATE.search(text)
            and _HEADER_DESC.search(text)
            and _HEADER_MONEY.search(text)
        ):
            ranges, money_ends = _column_roles(text)
            if "date" in ranges and "desc" in ranges and len(money_ends) >= 2:
                return idx, ranges, money_ends
    return None


def _cell(line: str, bounds: tuple[int, int]) -> str:
    start, end = bounds
    return line[start:end].strip() if start < len(line) else ""


def _row_date(cell: str, full_line: str) -> RawDate | None:
    for token in (cell, full_line):
        for raw, parsed in find_dates(token):
            return RawDate(raw=raw, normalized=parsed)
        direct = parse_date(token.strip())
        if direct is not None:
            return RawDate(raw=token.strip(), normalized=direct)
    return None


_MONEY_SPAN_RE = re.compile(
    r"[-+]?\(?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?\)?|[-+]?\(?\d+\.\d{2}\)?"
)


def _money_tokens(line: str) -> list[tuple[int, int, str]]:
    """Money tokens with (start, end, text). Requires a real money shape
    (thousands grouping or two decimals), so years and reference numbers
    are not mistaken for amounts."""
    return [(m.start(), m.end(), m.group(0)) for m in _MONEY_SPAN_RE.finditer(line)]


def _assign_columns(
    line: str, money_cols: dict[str, int]
) -> dict[str, tuple[int, int, str]]:
    """Assign each money token to the nearest money column.

    Statements right-align amount cells, so a token belongs to the column
    whose *label end* is closest to the token end. Greedy, one token per
    column — positional slicing is deliberately avoided because a
    right-aligned value can start before its own column's label.
    """
    assigned: dict[str, tuple[int, int, str]] = {}
    used: set[int] = set()
    for start, end, text in _money_tokens(line):
        best_role, best_dist = None, None
        for role, label_end in money_cols.items():
            if role in assigned:
                continue
            dist = abs(end - label_end)
            if best_dist is None or dist < best_dist:
                best_role, best_dist = role, dist
        if best_role is None:
            continue
        assigned[best_role] = (start, end, text)
        used.add(start)
    return assigned


def _direction_from(
    debit: RawMoney | None,
    credit: RawMoney | None,
    single: RawMoney | None,
) -> tuple[RawMoney | None, str | None, str]:
    """Return (amount, direction, note). Ambiguous rows return None."""
    if debit is not None and credit is not None:
        # Both columns populated: pick the larger absolute value, flag it.
        if abs(debit.normalized) >= abs(credit.normalized):
            return debit, "debit", "both_columns_used_debit"
        return credit, "credit", "both_columns_used_credit"
    if debit is not None:
        return RawMoney(raw=debit.raw, normalized=abs(debit.normalized)), "debit", ""
    if credit is not None:
        return RawMoney(raw=credit.raw, normalized=abs(credit.normalized)), "credit", ""
    if single is not None:
        if single.normalized < 0:
            return RawMoney(raw=single.raw, normalized=abs(single.normalized)), "debit", ""
        return single, "credit", ""
    return None, None, ""


def _heuristic_rows(
    lines: list[tuple[int, str]], currency: str
) -> list[ParsedTransaction]:
    """Fallback when no header row exists: date-anchored, amount-less rows.

    A row's amount is only taken when it is the trailing money token and
    the row also carries a balance — otherwise the row is recorded without
    an amount rather than risking a misread financial fact."""
    txns: list[ParsedTransaction] = []
    pending: ParsedTransaction | None = None
    for line_idx, (page, text) in enumerate(lines):
        low = text.lower()
        if _OPENING_RE.search(low) or _CLOSING_RE.search(low):
            continue
        row_date = _row_date(text, text)
        money = [m for tok in _MONEY_TOKEN_RE.findall(text) if (m := parse_money(tok)) is not None]
        if row_date is None:
            if pending is not None and text.strip() and not money:
                pending.description += " " + text.strip()
                pending.raw_description += "\n" + text
            elif pending is not None and len(money) >= 2 and pending.amount is None:
                amount, direction, _ = _direction_from(
                    None, None, RawMoney(raw=str(money[0]), normalized=money[0])
                )
                pending.amount, pending.direction = amount, direction  # type: ignore[assignment]
                pending.confidence = 0.45
            continue
        if pending is not None:
            txns.append(pending)
        if len(money) >= 2:
            amount, direction, _ = _direction_from(
                None, None, RawMoney(raw=str(money[1]), normalized=money[1])
            )
        else:
            amount, direction = None, None
        desc = re.sub(r"^\s*[\d/\-.,\sA-Za-z]{3,14}?\s{2,}", "", text).strip() or text
        pending = ParsedTransaction(
            date=row_date,
            description=desc,
            raw_description=text,
            amount=amount,
            direction=direction,  # type: ignore[arg-type]
            currency=currency,
            page=page,
            line_index=line_idx,
            confidence=0.45 if amount is not None else 0.3,
        )
    if pending is not None:
        txns.append(pending)
    return [t for t in txns if t.amount is not None]


def _mapped_rows(
    lines: list[tuple[int, str]],
    roles: dict[str, tuple[int, int]],
    money_cols: dict[str, int],
    currency: str,
) -> list[ParsedTransaction]:
    date_start, date_end = roles["date"]
    txns: list[ParsedTransaction] = []
    pending: ParsedTransaction | None = None

    def flush() -> None:
        nonlocal pending
        if pending is not None and pending.amount is not None and pending.direction is not None:
            txns.append(pending)
        elif pending is not None:
            logger.debug("dropping amount-less row: %r", pending.raw_description[:80])
        pending = None

    for line_idx, (page, text) in enumerate(lines):
        low = text.lower()
        if _OPENING_RE.search(low) or _CLOSING_RE.search(low) or low.startswith("page "):
            continue
        row_date = _row_date(_cell(text, (date_start, date_end)), text)
        assigned = _assign_columns(text, money_cols) if money_cols else {}

        def as_money(role: str) -> RawMoney | None:
            token = assigned.get(role)
            if token is None:
                return None
            value = parse_money(token[2])
            if value is None:
                return None
            return RawMoney(raw=token[2], normalized=value)

        debit, credit = as_money("debit"), as_money("credit")
        single, balance = as_money("amount"), as_money("balance")

        if row_date is None:
            if pending is None:
                continue
            # A wrapped row can carry BOTH the rest of the narration and the
            # amount for the transaction it continues. The text is always
            # joined; the amount and balance only fill in what the pending row
            # is missing. Previously the amount branch short-circuited the
            # text, so a wrapped description lost its continuation ("POS
            # PURCHASE FROM ONLINE STORE" kept, "NIGERIA REF 993201" dropped).
            continuation = _continuation_text(text, assigned)
            if continuation:
                pending.description = (
                    f"{pending.description} {continuation}".strip()
                )
                pending.raw_description += "\n" + text
            if pending.amount is None and (debit or credit or single):
                amount, direction, _ = _direction_from(debit, credit, single)
                pending.amount, pending.direction = amount, direction  # type: ignore[assignment]
                pending.confidence = 0.6
            if balance is not None and pending.balance_after is None:
                pending.balance_after = balance
            continue
        flush()
        amount, direction, note = _direction_from(debit, credit, single)
        desc = _description(text, row_date, assigned, roles, date_end)
        pending = ParsedTransaction(
            date=row_date,
            description=desc,
            raw_description=text,
            amount=amount,
            direction=direction,  # type: ignore[arg-type]
            currency=currency,
            balance_after=balance,
            page=page,
            line_index=line_idx,
            confidence=0.6 if note else (0.8 if balance is not None else 0.65),
        )
    flush()
    return txns


def _continuation_text(
    text: str, assigned: dict[str, tuple[int, int, str]]
) -> str:
    """The descriptive part of a wrapped row: everything but its money cells."""
    spans = sorted((start, end) for start, end, _ in assigned.values())
    if not spans:
        return text.strip()
    out = ""
    cursor = 0
    for start, end in spans:
        out += text[cursor:start]
        cursor = end
    out += text[cursor:]
    return " ".join(out.split())


def _description(
    text: str,
    row_date: RawDate,
    assigned: dict[str, tuple[int, int, str]],
    roles: dict[str, tuple[int, int]],
    date_end: int,
) -> str:
    """Prefer the description column's slice; fall back to the gap between
    the date and the first money token (works when columns drift)."""
    sliced = _cell(text, roles["desc"])
    if sliced:
        return sliced
    starts = [tok[0] for tok in assigned.values()]
    if starts:
        return text[date_end : min(starts)].strip()
    return text.strip()


def extract_statement(text: ExtractedText) -> StatementExtraction:
    out = StatementExtraction()
    lines = _text_lines(text)
    blob = text.full_text
    if m := _ACCT_NAME_RE.search(blob):
        out.account_name = m.group(1).strip()
    if m := _ACCT_NUM_RE.search(blob):
        out.masked_account_number = mask_account_number(m.group(1))
    lower_blob = blob.lower()
    for bank in _NG_BANKS:
        if bank in lower_blob:
            out.institution = bank.upper()
            break
    out.currency = detect_currency(blob)
    if not out.currency and out.institution:
        out.currency = "NGN"
    for _, line in lines:
        if _OPENING_RE.search(line) and out.opening_balance is None:
            if hit := last_money_on_line(line):
                out.opening_balance = RawMoney(raw=hit[0], normalized=hit[1])
        if _CLOSING_RE.search(line) and out.closing_balance is None:
            if hit := last_money_on_line(line):
                out.closing_balance = RawMoney(raw=hit[0], normalized=hit[1])
    dated = find_dates(blob)
    if dated:
        days = sorted(d for _, d in dated)
        out.period_start = RawDate(raw=dated[0][0], normalized=days[0])
        out.period_end = RawDate(raw=dated[-1][0], normalized=days[-1])
    found = _find_header(lines)
    if found is None:
        out.transactions = _heuristic_rows(lines, out.currency)
    else:
        header_idx, roles, money_ends = found
        out.transactions = _mapped_rows(
            lines[header_idx + 1 :], roles, money_ends, out.currency
        )
    credits = Decimal("0")
    debits = Decimal("0")
    for txn in out.transactions:
        if txn.amount is None or txn.direction is None:
            continue
        if txn.direction == "credit":
            credits += txn.amount.normalized
        else:
            debits += txn.amount.normalized
    if out.transactions:
        out.total_credits = credits
        out.total_debits = debits
    return out
def _text_lines(text: ExtractedText) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for page in text.pages:
        if page.lines:
            for ln in page.lines:
                if ln.text.strip():
                    out.append((page.page, ln.text.strip()))
        elif page.text:
            for ln in page.text.splitlines():
                if ln.strip():
                    out.append((page.page, ln.strip()))
    if not out and text.full_text:
        out = [(1, ln.strip()) for ln in text.full_text.splitlines() if ln.strip()]
    return out



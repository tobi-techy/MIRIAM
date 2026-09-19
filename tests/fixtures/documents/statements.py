"""Synthetic bank-statement fixtures (Stage 3).

All names/accounts/amounts are fabricated — no customer data. Three text
layouts plus a helper that renders them into real PDF bytes via pypdf so
the native-PDF path is exercised end-to-end.
"""

from __future__ import annotations

import io

# Layout A: separate Debit / Credit columns (common NG retail format).
GTB_DEBIT_CREDIT = """GUARANTY TRUST BANK PLC
ACCOUNT STATEMENT
Account Name: ADA OKAFOR
Account Number: 0123456789
Statement Period: 01/08/2026 to 31/08/2026
Currency: NGN

Date        Description                      Debit        Credit       Balance
01/08/2026  OPENING BALANCE                                           250000.00
02/08/2026  POS PURCHASE CHICKEN REPUBLIC   8500.00                   241500.00
03/08/2026  SALARY PAYMENT FROM RAIL LTD                  450000.00   691500.00
05/08/2026  ATM WITHDRAWAL LAGOS            20000.00                  671500.00
07/08/2026  POS PURCHASE SHOPRITE           15400.00                  656100.00
10/08/2026  TRANSFER TO UBER BV             3200.00                   652900.00
12/08/2026  BANK CHARGE - MAINTENANCE       250.00                    652650.00
15/08/2026  REFUND FROM JUMIA                          12000.00    664650.00
20/08/2026  REVERSAL OF FAILED TRANSFER     5000.00                   659650.00
25/08/2026  SALARY PAYMENT FROM RAIL LTD                  450000.00   1109650.00
31/08/2026  CLOSING BALANCE                                           1109650.00
"""

# Layout B: single signed Amount column with running balance.
ACCESS_SINGLE_AMOUNT = """ACCESS BANK PLC
Statement of Account
Account Name: CHINEDU EZE
Account No: 0987654321
Period: 01/09/2026 - 30/09/2026

DATE        NARRATION                        AMOUNT        BALANCE
01/09/2026  Opening Balance                               1,250,000.00
03/09/2026  POS PURCHASE - EATERY            -12,500.00    1,237,500.00
06/09/2026  DEPOSIT - CASH                   +300,000.00   1,537,500.00
09/09/2026  ATM WITHDRAWAL                   -40,000.00    1,497,500.00
14/09/2026  SUBSCRIPTION NETFLIX             -6,500.00     1,491,000.00
21/09/2026  BANK CHARGE                      -1,000.00     1,490,000.00
30/09/2026  Closing Balance                               1,490,000.00
"""

# Layout C: Withdrawal / Deposit wording, USD, month-name dates.
ZENITH_WITHDRAWAL_DEPOSIT_USD = """ZENITH BANK PLC
ACCOUNT STATEMENT - USD
Account Name: FATIMA BELLO
Account Number: 5544332211
Statement Period: 1 Jan 2026 - 31 Jan 2026

DATE          NARRATION                        WITHDRAWAL     DEPOSIT      BALANCE
05 Jan 2026   OPENING BALANCE                                              5,000.00
08 Jan 2026   WIRE TRANSFER IN                               2,500.00     7,500.00
11 Jan 2026   CARD PURCHASE - AMAZON         120.50                       7,379.50
17 Jan 2026   SUBSCRIPTION - GITHUB          21.00                       7,358.50
22 Jan 2026   WIRE TRANSFER TO LANDLORD      1,200.00                    6,158.50
28 Jan 2026   INTEREST CREDIT                                12.75        6,171.25
31 Jan 2026   CLOSING BALANCE                                              6,171.25
"""

# Multiline / wrapped description with no balance column. Amounts are
# right-aligned to the end of the column their header declares: a
# fixed-width statement does not shift a value because the narration got
# longer, and the parser assigns a value to Debit/Credit by that alignment
# (never by keyword).
UBA_MULTILINE_NO_BALANCE = """UNITED BANK FOR AFRICA
Account Name: SAMUEL ADEYEMI
Account Number: 1122334455

Transaction Date   Narration                                Debit      Credit
02/08/2026         POS PURCHASE FROM ONLINE STORE LAGOS
                   NIGERIA REF 993201                   25,000.00
04/08/2026         INWARD TRANSFER FROM CLIENT A                   400,000.00
06/08/2026         CABLE SUBSCRIPTION DSTV              18,500.00
"""

# Deliberately inconsistent closing balance: proves we never "fix" numbers.
FIDELITY_MISMATCH = """FIDELITY BANK PLC
Account Name: BLESSING EKONG
Account Number: 2233445566
Statement Period: 01/10/2026 - 31/10/2026

Date        Description                      Debit        Credit       Balance
01/10/2026  Opening Balance                                            100,000.00
05/10/2026  POS PURCHASE                       30,000.00                70,000.00
10/10/2026  REFUND RECEIVED                                 5,000.00    75,000.00
15/10/2026  REVERSAL OF DEBIT                  10,000.00                65,000.00
20/10/2026  SERVICE FEE                            500.00               64,500.00
31/10/2026  Closing Balance                                            69,000.00
"""

# Not a statement: classification must not force a type.
INVOICE_SAMPLE = """ACME SUPPLIES LTD
INVOICE
Invoice Number: INV-2026-0912
Bill To: Rail Services Ltd
Due Date: 30/09/2026
Payment Terms: Net 30
Subtotal: 250,000.00
VAT: 18,750.00
Total: 268,750.00
"""


def make_pdf(lines: str) -> bytes:
    """Render fixture text into a real single-page PDF (pypdf writer)."""
    from pypdf import PdfWriter
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    parts = ["BT", "/F1 9 Tf", "40 750 Td", "12 TL"]
    for line in lines.splitlines():
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        parts.append(f"({safe}) Tj")
        parts.append("T*")
    parts.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(parts).encode("latin-1", "replace"))
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    page[NameObject("/Contents")] = writer._add_object(stream)  # type: ignore[attr-defined]
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}  # type: ignore[attr-defined]
            )
        }
    )
    page[NameObject("/Resources")] = resources
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()

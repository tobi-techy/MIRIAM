"""Rebuild the Wigmore Trading invoice fixtures.

``python tests/fixtures/inv.py`` writes the text-layer invoice PDF used by the
shop eval and (if it is not already present) the phone photo of the same
invoice. The figures match the invoice the owner sent, including the deliberate
mistake:

* Milo 400g refill: 6 x N3,800 = N22,800, but the sheet prints N24,800
  (N2,000 too high).
* The printed subtotal N182,600 is therefore N2,000 over the correct N180,600.
* Discount N2,600, total N180,000, paid N150,000, balance N30,000 due
  17/10/2026.

The PDF is written with a tiny hand-rolled writer so there is no reportlab or
fpdf dependency and the text stays a real, extractable text layer (which is
what the "native PDF text first" path needs).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PDF_PATH = HERE / "wigmore_invoice_WGT-2610-0417.pdf"
PHOTO_PATH = HERE / "wigmore_invoice_phone_photo.jpg"

INVOICE_LINES: list[str] = [
    "WIGMORE TRADING LTD",
    "FMCG Distribution | 14 Ogunlana Drive, Surulere, Lagos",
    "0803 412 7790  RC 1184522 | sales@wigmoretrading.ng",
    "",
    "INVOICE",
    "Invoice No: WGT/INV/2610-0417",
    "Date: 10/10/2026",
    "Delivery Note: DN-2610-0388",
    "Sales Rep: Kunle A.",
    "",
    "BILL TO",
    "Mama Tobi Stores",
    "Shop 6, Ojuwoye Market, Mushin, Lagos",
    "Tel: 0816 220 4419",
    "",
    "Item                                        Qty    Unit Price (N)   Amount (N)",
    "1 Indomie Instant Noodles 70g (ctn of 40)  10 ctn      9,200          92,000",
    "2 Peak Evaporated Milk 160g tin            24 tin        400           9,600",
    "3 Dangote Spaghetti 500g                    20 pk        950          19,000",
    "4 Milo 400g refill                           6 pk      3,800          24,800",
    "5 Kings Vegetable Oil 1L                    12 btl      3,100          37,200",
    "",
    "Subtotal                                                              182,600",
    "Trade discount                                                         -2,600",
    "TOTAL                                                                 180,000",
    "Paid (transfer, 10/10/2026)                                          -150,000",
    "Balance due 17/10/2026                                                 30,000",
    "",
    "Payment terms 7 days credit. Pay to Wigmore Trading Ltd, "
    "Zenith Bank 1014 553 820.",
    "Use invoice number as transfer reference.",
    "Goods received in good condition. Report shortages within 24 hours of delivery.",
    "",
    "Received by (customer)                    For Wigmore Trading Ltd",
    "Thank you for your business.",
]


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _content_stream(lines: list[str]) -> bytes:
    parts = ["BT", "/F1 10 Tf", "12 TL", "50 800 Td"]
    for line in lines:
        parts.append(f"({_escape(line)}) Tj")
        parts.append("T*")
    parts.append("ET")
    return "\n".join(parts).encode("latin-1", "replace")


def write_text_pdf(path: Path, lines: list[str]) -> None:
    content = _content_stream(lines)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))


def write_phone_photo(path: Path) -> None:
    from PIL import Image, ImageDraw

    width, height = 1200, 1600
    image = Image.new("RGB", (width, height), (245, 243, 236))
    draw = ImageDraw.Draw(image)
    y = 60
    for line in INVOICE_LINES:
        draw.text((60, y), line, fill=(20, 20, 20))
        y += 34
    image.save(path, quality=88)


def main() -> int:
    write_text_pdf(PDF_PATH, INVOICE_LINES)
    print(f"wrote {PDF_PATH}")
    if not PHOTO_PATH.exists():
        write_phone_photo(PHOTO_PATH)
        print(f"wrote {PHOTO_PATH}")
    else:
        print(f"kept existing {PHOTO_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

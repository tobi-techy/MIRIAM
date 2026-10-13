"""Rebuild the ledger v2 stock-book fixtures.

``python tests/fixtures/ledger.py`` writes the second stock-book page the shop
eval scores, in both forms the owner really sends:

* ``stock_book_ledger_v2.png`` — the page as scanned or photographed.
* ``stock_book_ledger_v2_scan.pdf`` — the same raster page inside a PDF, with no
  text layer at all, so the reader has to rasterise it rather than read native
  text.

The figures are the amendment's second page:

* Every row adds up: opening + supply - sold = left, so no row is a failure.
* The prices and quantities multiply out to N248,000 of sales.
* The total the owner wrote at the foot of the page is N236,000, so the page is
  N12,000 short of its own rows. That gap is the thing this page exists to show.

Nothing random: the same bytes come out of every run.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PNG_PATH = HERE / "stock_book_ledger_v2.png"
SCAN_PATH = HERE / "stock_book_ledger_v2_scan.pdf"

TITLE = "MAMA TOBI STORES"
SUBTITLE = "STOCK LEDGER"
DATE = "10/10/2026"

# item, opening, supplied, sold, left, price in naira
ROWS: list[tuple[str, int, int, int, int, str]] = [
    ("Indomie (carton)", 20, 12, 14, 18, "9,500"),
    ("Peak Milk tin", 50, 0, 26, 24, "450"),
    ("Golden Penny Sugar", 20, 4, 12, 12, "1,600"),
    ("Dangote Spag", 30, 9, 19, 20, "1,100"),
    ("Milo 400g", 10, 0, 4, 6, "4,200"),
    ("Coke 50cl", 30, 0, 11, 19, "400"),
    ("Kings Oil 1L", 24, 0, 12, 12, "3,500"),
]

WRITTEN_TOTAL = "236,000"

COLUMNS = ["ITEM", "OPEN", "SUPPLY", "SOLD", "LEFT", "PRICE (N)"]
# x positions of each column, in page pixels
COLUMN_X = [70, 560, 680, 800, 920, 1060]

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size: int):
    from PIL import ImageFont

    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def write_page(path: Path) -> None:
    from PIL import Image, ImageDraw

    width, height = 1408, 768
    image = Image.new("RGB", (width, height), (247, 244, 235))
    draw = ImageDraw.Draw(image)

    header = _font(38)
    body = _font(27)

    draw.text((COLUMN_X[0], 46), TITLE, fill=(20, 20, 20), font=header)
    draw.text((620, 48), SUBTITLE, fill=(20, 20, 20), font=header)
    draw.text((1180, 52), DATE, fill=(20, 20, 20), font=body)

    y = 140
    for label, x in zip(COLUMNS, COLUMN_X, strict=True):
        draw.text((x, y), label, fill=(60, 60, 60), font=body)
    y += 46
    draw.line((60, y - 8, width - 60, y - 8), fill=(120, 120, 120), width=2)

    for item, opening, supplied, sold, left, price in ROWS:
        values = [item, str(opening), str(supplied), str(sold), str(left), price]
        for value, x in zip(values, COLUMN_X, strict=True):
            draw.text((x, y), value, fill=(25, 25, 25), font=body)
        y += 52

    y += 20
    draw.line((600, y, width - 60, y), fill=(120, 120, 120), width=2)
    y += 18
    draw.text((620, y), "Total sales today", fill=(25, 25, 25), font=body)
    draw.text((1180, y), WRITTEN_TOTAL, fill=(25, 25, 25), font=body)

    image.save(path, format="PNG")


def write_scan(pdf_path: Path, png_path: Path) -> None:
    """The page as a scan: raster pixels in a PDF, no text layer to read."""
    from PIL import Image

    with Image.open(png_path) as image:
        image.convert("RGB").save(pdf_path, format="PDF", resolution=150.0)


def main() -> int:
    write_page(PNG_PATH)
    print(f"wrote {PNG_PATH}")
    write_scan(SCAN_PATH, PNG_PATH)
    print(f"wrote {SCAN_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

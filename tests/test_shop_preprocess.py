"""Preprocess tests: decode, rotate, downscale, rasterise. No network."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from miriam_agent.shop import preprocess

FIXTURES = Path(__file__).resolve().parent / "fixtures"
INVOICE_PDF = FIXTURES / "wigmore_invoice_WGT-2610-0417.pdf"
SCAN_PDF = FIXTURES / "stock_book_ledger_v2_scan.pdf"
PHONE_PHOTO = FIXTURES / "wigmore_invoice_phone_photo.jpg"
STOCK_PNG = FIXTURES / "stock_book_mama_tobi.png"


def make_png(
    size: tuple[int, int], color: tuple[int, int, int] = (240, 236, 220)
) -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", size, color)
    for x in range(0, size[0], 25):
        for y in range(0, size[1], 25):
            image.putpixel((x, y), (30, 30, 30))
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_png_becomes_one_rgb_page_with_facts() -> None:
    data = make_png((800, 500))
    prepared = preprocess.prepare(data, "image/png", "my stock book")

    assert len(prepared.pages) == 1
    assert prepared.mime == "image/png"
    assert prepared.sha256 == preprocess.sha256_hex(data)
    assert prepared.facts.width == 800
    assert prepared.facts.height == 500
    assert prepared.facts.page_count == 1
    assert prepared.facts.caption == "my stock book"
    assert prepared.facts.brightness > 0
    assert prepared.native_text == ""
    assert prepared.pages[0].png[:8] == b"\x89PNG\r\n\x1a\n"


def test_long_edge_is_downscaled_to_the_setting() -> None:
    from miriam_agent.config.settings import get_settings

    long_edge = get_settings().SHOP_READ_LONG_EDGE_PX
    prepared = preprocess.prepare(make_png((long_edge * 2, long_edge)), "image/png")
    image = Image.open(io.BytesIO(prepared.pages[0].png))
    assert max(image.size) == long_edge


def test_exif_rotation_is_applied() -> None:
    buffer = io.BytesIO()
    image = Image.new("RGB", (400, 200), (250, 250, 250))
    exif = image.getexif()
    exif[274] = 6  # rotate 90 degrees clockwise on display
    image.save(buffer, format="JPEG", exif=exif)
    prepared = preprocess.prepare(buffer.getvalue(), "image/jpeg")
    page = Image.open(io.BytesIO(prepared.pages[0].png))
    assert page.size[1] > page.size[0]
    assert prepared.facts.height > prepared.facts.width


def test_sha256_is_stable_and_differs_per_file() -> None:
    a = make_png((100, 100))
    b = make_png((100, 100), (10, 20, 30))
    assert preprocess.sha256_hex(a) == preprocess.sha256_hex(a)
    assert preprocess.sha256_hex(a) != preprocess.sha256_hex(b)


def test_empty_file_is_refused() -> None:
    with pytest.raises(ValueError):
        preprocess.prepare(b"", "image/png")


def test_unreadable_image_is_refused() -> None:
    with pytest.raises(ValueError):
        preprocess.prepare(b"not an image at all", "image/png")


@pytest.mark.skipif(not INVOICE_PDF.exists(), reason="invoice fixture missing")
def test_text_pdf_keeps_a_usable_text_layer() -> None:
    data = INVOICE_PDF.read_bytes()
    prepared = preprocess.prepare(data, "application/pdf")
    assert prepared.native_text_usable is True
    assert "WGT/INV/2610-0417" in prepared.native_text
    assert "24,800" in prepared.native_text
    assert "180,000" in prepared.native_text
    assert prepared.pages


@pytest.mark.skipif(not PHONE_PHOTO.exists(), reason="invoice photo missing")
def test_phone_photo_of_the_invoice_is_decoded() -> None:
    prepared = preprocess.prepare(PHONE_PHOTO.read_bytes(), "image/jpeg")
    assert len(prepared.pages) == 1
    assert prepared.facts.width > 0
    assert prepared.native_text == ""


@pytest.mark.skipif(not STOCK_PNG.exists(), reason="stock book fixture missing")
def test_handwritten_stock_photo_is_decoded_at_the_cap() -> None:
    from miriam_agent.config.settings import get_settings

    prepared = preprocess.prepare(STOCK_PNG.read_bytes(), "image/png")
    assert len(prepared.pages) == 1
    assert max(prepared.facts.width, prepared.facts.height) <= (
        get_settings().SHOP_READ_LONG_EDGE_PX
    )


@pytest.mark.skipif(not SCAN_PDF.exists(), reason="scanned stock book missing")
def test_scanned_pdf_is_rasterised_page_by_page() -> None:
    from miriam_agent.config.settings import get_settings

    data = SCAN_PDF.read_bytes()
    prepared = preprocess.prepare(data, "application/pdf")
    assert len(prepared.pages) <= get_settings().SHOP_READ_PDF_MAX_PAGES
    assert len(prepared.pages) >= 1
    assert prepared.native_text_usable is False
    assert prepared.pages[0].png[:8] == b"\x89PNG\r\n\x1a\n"

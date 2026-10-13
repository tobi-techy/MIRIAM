"""Preprocessing for shop images and PDFs.

Turns whatever arrives from iMessage into page images a vision model can read
and a set of cheap, deterministic facts the router can judge without a model:

* rotate for EXIF, convert HEIC to JPEG, downscale the long edge, and boost
  contrast on a dim photo;
* rasterise PDF pages (capped) and extract native text first where it exists;
* measure width, height, blur (Laplacian variance), brightness and page count.

No model runs here and no bytes are logged.
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # Pillow is imported lazily inside each helper
    from PIL.Image import Image as PILImage

logger = logging.getLogger(__name__)

# Blur/brightness thresholds are the deterministic part of the router signal.
BLUR_SHARP = 120.0  # Laplacian variance above this is a sharp image
BRIGHT_DIM = 0.45  # mean luma below this is a dim photo


@dataclass(frozen=True)
class ImageFacts:
    width: int = 0
    height: int = 0
    blur: float = 0.0
    brightness: float = 0.0
    page_count: int = 1
    file_type: str = ""
    caption: str = ""


@dataclass
class PreparedPage:
    index: int  # 1-based
    png: bytes
    native_text: str = ""


@dataclass
class PreparedDocument:
    pages: list[PreparedPage] = field(default_factory=list)
    facts: ImageFacts = field(default_factory=ImageFacts)
    sha256: str = ""
    mime: str = ""
    native_text: str = ""
    native_text_usable: bool = False


_HEIF_REGISTERED = False

_HEIC_MIMES = {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _register_heif() -> None:
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception as e:  # noqa: BLE001 - optional; failure surfaces on open
        logger.warning("HEIC support unavailable: %s", e)
    _HEIF_REGISTERED = True


def _normalise_mime(mime: str) -> str:
    return (mime or "").strip().lower()


def _is_pdf(mime: str, data: bytes) -> bool:
    return mime == "application/pdf" or data[:5] == b"%PDF-"


def _laplacian_variance(gray: object) -> float:
    """Variance of the 4-neighbour Laplacian. Low means blurry."""
    import numpy as np

    a = np.asarray(gray, dtype=np.float64)
    if a.shape[0] < 3 or a.shape[1] < 3:
        return 0.0
    out = a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:] - 4.0 * a[1:-1, 1:-1]
    return float(out.var())


def _downscale_long_edge(img: PILImage, long_edge: int) -> PILImage:
    from PIL import Image

    w, h = img.size
    longest = max(w, h)
    if longest <= long_edge:
        return img
    scale = long_edge / float(longest)
    return img.resize(
        (max(1, int(w * scale)), max(1, int(h * scale))),
        Image.Resampling.LANCZOS,
    )


def _boost_contrast(img: PILImage, brightness: float) -> PILImage:
    from PIL import ImageEnhance, ImageOps

    if brightness >= BRIGHT_DIM:
        return img
    bright = ImageOps.autocontrast(img, cutoff=1)
    return ImageEnhance.Contrast(bright).enhance(1.3)


def _facts_from_image(
    img: PILImage, *, file_type: str, caption: str, page_count: int
) -> ImageFacts:
    gray = img.convert("L")
    np_gray = gray
    import numpy as np

    arr = np.asarray(np_gray, dtype=np.float64)
    brightness = float(arr.mean() / 255.0) if arr.size else 0.0
    return ImageFacts(
        width=img.size[0],
        height=img.size[1],
        blur=_laplacian_variance(arr),
        brightness=brightness,
        page_count=page_count,
        file_type=file_type,
        caption=caption,
    )


def _encode_png(img: PILImage) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _prepare_image(
    data: bytes, mime: str, caption: str, *, long_edge: int
) -> PreparedDocument:
    from PIL import Image, ImageOps

    if mime in _HEIC_MIMES:
        _register_heif()
    try:
        opened = Image.open(io.BytesIO(data))
        opened.load()
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"unreadable image: {e}") from e

    page: PILImage = ImageOps.exif_transpose(opened)
    if page.mode not in ("RGB", "L"):
        page = page.convert("RGB")
    page = _downscale_long_edge(page, long_edge)
    gray = page.convert("L")
    import numpy as np

    brightness = float(np.asarray(gray, dtype=np.float64).mean() / 255.0)
    page = _boost_contrast(page.convert("RGB"), brightness)

    facts = _facts_from_image(
        page, file_type=mime or "image", caption=caption, page_count=1
    )
    return PreparedDocument(
        pages=[PreparedPage(index=1, png=_encode_png(page))],
        facts=facts,
        sha256=sha256_hex(data),
        mime=mime or "image/jpeg",
        native_text="",
        native_text_usable=False,
    )


def _native_pdf_text(data: bytes) -> tuple[str, bool]:
    """Native PDF text plus a deterministic usability verdict."""
    from miriam_agent.config.settings import get_settings
    from miriam_agent.documents.extraction.native_pdf import (
        assess_quality,
        extract_native_text,
    )

    try:
        extracted = extract_native_text(data)
    except Exception as e:  # noqa: BLE001 - scanned PDFs have no text at all
        logger.info("native PDF text unavailable: %s", e)
        return "", False
    quality = assess_quality(
        extracted, min_chars=get_settings().DOCUMENT_MIN_TEXT_QUALITY
    )
    return extracted.full_text, quality.usable


def _rasterise_pdf(data: bytes, *, max_pages: int, long_edge: int) -> list[PILImage]:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"unreadable PDF: {e}") from e
    images: list[PILImage] = []
    try:
        count = min(len(pdf), max_pages)
        for i in range(count):
            page = pdf[i]
            width_pt, height_pt = page.get_size()
            longest = max(width_pt, height_pt) or long_edge
            scale = long_edge / float(longest)
            bitmap = page.render(scale=scale)
            images.append(bitmap.to_pil())
    finally:
        pdf.close()
    return images


def _prepare_pdf(
    data: bytes, caption: str, *, max_pages: int, long_edge: int
) -> PreparedDocument:
    native_text, usable = _native_pdf_text(data)
    pages_img = _rasterise_pdf(data, max_pages=max_pages, long_edge=long_edge)
    if not pages_img:
        raise ValueError("PDF has no pages to read")

    pages: list[PreparedPage] = []
    for idx, img in enumerate(pages_img, start=1):
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img = _downscale_long_edge(img, long_edge)
        pages.append(PreparedPage(index=idx, png=_encode_png(img.convert("RGB"))))

    facts = _facts_from_image(
        pages_img[0].convert("RGB"),
        file_type="application/pdf",
        caption=caption,
        page_count=len(pages),
    )
    return PreparedDocument(
        pages=pages,
        facts=facts,
        sha256=sha256_hex(data),
        mime="application/pdf",
        native_text=native_text,
        native_text_usable=usable,
    )


def prepare(
    data: bytes,
    mime: str,
    caption: str = "",
    *,
    max_pages: int | None = None,
    long_edge: int | None = None,
) -> PreparedDocument:
    """Preprocess one inbound file into pages plus router facts."""
    from miriam_agent.config.settings import get_settings

    if not data:
        raise ValueError("empty file")
    settings = get_settings()
    max_pages = max_pages or settings.SHOP_READ_PDF_MAX_PAGES
    long_edge = long_edge or settings.SHOP_READ_LONG_EDGE_PX
    mime = _normalise_mime(mime)

    if _is_pdf(mime, data):
        return _prepare_pdf(data, caption, max_pages=max_pages, long_edge=long_edge)
    if mime.startswith("image/") or mime in _HEIC_MIMES:
        return _prepare_image(data, mime, caption, long_edge=long_edge)
    raise ValueError(f"unsupported file type: {mime or 'unknown'}")


__all__ = [
    "BRIGHT_DIM",
    "BLUR_SHARP",
    "ImageFacts",
    "PreparedDocument",
    "PreparedPage",
    "prepare",
    "sha256_hex",
]

"""OCR client for the existing PaddleOCR sidecar (Stage 3).

Mirrors ``RAIL_BACKEND/internal/domain/services/document/ocr_engine.go``:
POST {base}/ocr {file_b64, mime_type, doc_hint} with timeout, bounded
retries on transient failures only, and structured errors. Provenance
(page, bbox, confidence, order) is preserved on every line.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import httpx

from miriam_agent.core.exceptions import IntegrationError
from miriam_agent.documents.models import ExtractedText, OCRLine, PageText
from miriam_agent.observability.correlation import current_trace_id

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MAX_BYTES = 20 * 1024 * 1024


class SidecarOCRProvider:
    """OCRProvider implementation over the shared OCR sidecar."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._client = client

    async def recognize(
        self, data: bytes, mime_type: str, *, doc_hint: str = ""
    ) -> ExtractedText:
        if not data:
            raise IntegrationError("OCR: empty file")
        if len(data) > MAX_BYTES:
            raise IntegrationError("OCR: file too large")
        payload = {
            "file_b64": base64.b64encode(data).decode("ascii"),
            "mime_type": mime_type,
            "doc_hint": doc_hint,
        }
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                body = await self._post(payload)
                return self._parse(body)
            except IntegrationError as e:
                last_error = e
                if not self._retryable(e) or attempt >= self.max_retries:
                    raise
                await asyncio.sleep(0.5 * (2**attempt))
        raise last_error or IntegrationError("OCR failed")

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        trace_id = current_trace_id()
        headers = {"Content-Type": "application/json"}
        if trace_id:
            headers["X-Miriam-Trace-Id"] = trace_id
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        owned = self._client is None
        try:
            resp = await client.post(
                f"{self.base_url}/ocr", json=payload, headers=headers
            )
        except httpx.HTTPError as e:
            raise IntegrationError(f"OCR service unreachable: {e}") from e
        finally:
            if owned:
                await client.aclose()
        if resp.status_code != 200:
            raise IntegrationError(
                f"OCR service error {resp.status_code}: {resp.text[:200]}",
                {"http_status": resp.status_code, "trace_id": trace_id},
            )
        try:
            parsed: dict[str, Any] = resp.json()
        except ValueError as e:
            raise IntegrationError(f"OCR response not JSON: {e}") from e
        return parsed

    def _parse(self, body: dict[str, Any]) -> ExtractedText:
        raw_lines = body.get("lines") or []
        by_page: dict[int, list[OCRLine]] = {}
        confidences: list[float] = []
        for entry in raw_lines:
            if not isinstance(entry, dict) or not entry.get("text"):
                continue
            line = OCRLine(
                text=str(entry.get("text", "")),
                page=int(entry.get("page", 1)),
                confidence=float(entry.get("confidence", 0.0) or 0.0),
                bbox=[list(map(float, b)) for b in (entry.get("bbox") or []) if b],
            )
            by_page.setdefault(line.page, []).append(line)
            confidences.append(line.confidence)
        pages = [
            PageText(page=pg, text="\n".join(ln.text for ln in lines), lines=lines)
            for pg, lines in sorted(by_page.items())
        ]
        text = str(body.get("text", ""))
        if not pages and text:
            pages = [PageText(page=1, text=text)]
        mean_conf = float(body.get("mean_confidence", 0.0) or 0.0) or (
            sum(confidences) / len(confidences) if confidences else 0.0
        )
        started = time.perf_counter()
        void = started
        del void
        return ExtractedText(
            full_text=text,
            pages=pages,
            method="ocr",
            mean_confidence=mean_conf,
            engine="paddleocr",
        )

    def _retryable(self, e: IntegrationError) -> bool:
        status = e.details.get("http_status") if isinstance(e.details, dict) else None
        return status in RETRYABLE_STATUS

"""POST /api/v1/shop/documents/read.

One new endpoint, called by Go for a shop stock book, a distributor invoice or
a delivery note. The caller is the Go backend acting for the owner: it mints
the same short-lived per-user JWT it uses for ``/api/v1/chat``. The body's
``user_id`` must equal that token's subject, so a token for one owner can never
be used to read (or store) another owner's document.

Fail closed: a timeout, a provider failure or an unreadable page returns the one
retake message, never a partial read.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from miriam_agent.api.dependencies import get_current_user
from miriam_agent.config.settings import get_settings
from miriam_agent.database.models import User
from miriam_agent.shop.reader import ReadFailed
from miriam_agent.shop.service import read_shop_document
from miriam_agent.shop.store import ShopDocumentStore, default_store

logger = logging.getLogger(__name__)

router = APIRouter()

_store: ShopDocumentStore | None = None
# Test seams: the reader's Jev judge and vision provider can be swapped so the
# whole HTTP path runs with fakes and no network. Left None in production.
_judge_override: Any = None
_provider_factory_override: Any = None


def _get_store() -> ShopDocumentStore:
    global _store
    if _store is None:
        _store = default_store()
    return _store


def set_store(store: ShopDocumentStore | None) -> None:
    """Test seam: swap the document store (pass None to reset)."""
    global _store
    _store = store


def set_reader_overrides(
    *,
    judge: Any = None,
    provider_factory: Any = None,
    clear: bool = False,
) -> None:
    """Test seam: inject a fake judge and/or vision provider factory."""
    global _judge_override, _provider_factory_override
    if clear:
        _judge_override = None
        _provider_factory_override = None
        return
    if judge is not None:
        _judge_override = judge
    if provider_factory is not None:
        _provider_factory_override = provider_factory


class ShopReadRequest(BaseModel):
    file_b64: str = ""
    mime: str = ""
    user_id: str = ""
    message_id: str = ""
    # Go's own classifier result, sent as a hint so a Jev outage still routes
    # to the right schema. Python still classifies; the hint only breaks ties.
    doc_hint: str = ""
    caption: str = ""


class ShopReadResponse(BaseModel):
    reply_text: str
    doc_id: str
    doc_type: str
    flags: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    model_path: list[str] = Field(default_factory=list)


@router.post("/shop/documents/read", response_model=ShopReadResponse)
async def read_shop_documents(
    body: ShopReadRequest,
    user: User = Depends(get_current_user),
) -> ShopReadResponse:
    """Read one stock book, invoice or delivery note and return the reply."""
    if not body.file_b64:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="file_b64 is required",
        )
    # The token subject is the authority, not the body.
    if body.user_id and body.user_id != user.id:
        logger.warning(
            "shop read user mismatch",
            extra={"token_user": user.id, "body_user": body.user_id},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user_id does not match the authenticated subject",
        )

    settings = get_settings()
    max_bytes = settings.DOCUMENT_MAX_SIZE_MB * 1024 * 1024
    # Base64 expands by ~4/3; reject an obviously oversized payload early.
    if len(body.file_b64) > (max_bytes // 3) * 4 + 16:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="file too large",
        )

    try:
        result = await read_shop_document(
            file_b64=body.file_b64,
            mime=body.mime,
            user_id=user.id,
            message_id=body.message_id,
            doc_hint=body.doc_hint,
            caption=body.caption,
            store=_get_store(),
            judge=_judge_override,
            provider_factory=_provider_factory_override,
        )
    except ReadFailed as e:
        return ShopReadResponse(
            reply_text=str(e),
            doc_id="",
            doc_type="other",
            flags=["failed"],
            confidence=0.0,
            model_path=[],
        )

    return ShopReadResponse(
        reply_text=result.reply_text,
        doc_id=result.doc_id,
        doc_type=result.doc_type,
        flags=result.flags,
        confidence=result.confidence,
        model_path=result.model_path,
    )


__all__: list[Any] = ["router", "set_reader_overrides", "set_store"]

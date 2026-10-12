"""Persistence for shop reads.

Idempotent on ``(user_id, sha256)``: the same file resent returns the original
reply and writes nothing new. A unique violation on that pair is the dedupe
signal, not an error. The three child tables are written in the same
transaction as the document row so a reader never sees a half-saved read.

A small in-memory store backs tests and the live eval when Postgres is absent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass
class StoredDocument:
    id: str
    doc_type: str
    trust_label: str
    reply_text: str
    confidence: float
    model_path: list[Any] = field(default_factory=list)
    flags: list[Any] = field(default_factory=list)


class ShopDocumentStore(Protocol):
    async def find_by_sha(self, user_id: str, sha256: str) -> StoredDocument | None: ...

    async def save(
        self,
        *,
        user_id: str,
        message_id: str,
        doc_type: str,
        trust_label: str,
        sha256: str,
        mime: str,
        model_path: list[Any],
        models_used: dict[str, Any],
        jev_decision: dict[str, Any],
        flags: list[Any],
        prompt_version: str,
        confidence: float,
        reply_text: str,
        raw_extraction: dict[str, Any],
        rows: list[dict[str, Any]],
        lines: list[dict[str, Any]],
        notes: list[dict[str, Any]],
    ) -> StoredDocument: ...


class InMemoryShopDocumentStore:
    """Process-local store. Tests and the live eval only."""

    def __init__(self) -> None:
        self._by_sha: dict[tuple[str, str], StoredDocument] = {}

    async def find_by_sha(self, user_id: str, sha256: str) -> StoredDocument | None:
        return self._by_sha.get((user_id, sha256))

    async def save(self, **kwargs: Any) -> StoredDocument:
        key = (kwargs["user_id"], kwargs["sha256"])
        existing = self._by_sha.get(key)
        if existing is not None:
            return existing
        import uuid

        stored = StoredDocument(
            id=str(uuid.uuid4()),
            doc_type=kwargs["doc_type"],
            trust_label=kwargs["trust_label"],
            reply_text=kwargs["reply_text"],
            confidence=kwargs["confidence"],
            model_path=list(kwargs.get("model_path") or []),
            flags=list(kwargs.get("flags") or []),
        )
        self._by_sha[key] = stored
        return stored


class PostgresShopDocumentStore:
    """Durable store over the shop_* tables (alembic revision 003)."""

    async def find_by_sha(self, user_id: str, sha256: str) -> StoredDocument | None:
        from sqlalchemy import select

        from miriam_agent.database.connection import get_session_factory
        from miriam_agent.database.models import ShopDocument

        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ShopDocument).where(
                    ShopDocument.user_id == user_id,
                    ShopDocument.sha256 == sha256,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _to_stored(row)

    async def save(self, **kwargs: Any) -> StoredDocument:
        from sqlalchemy.exc import IntegrityError

        from miriam_agent.database.connection import get_session_factory
        from miriam_agent.database.models import (
            ShopDocument,
            ShopInvoiceLine,
            ShopNote,
            ShopStockBookRow,
        )

        document_id = _new_id()
        document = ShopDocument(
            id=document_id,
            user_id=kwargs["user_id"],
            message_id=kwargs.get("message_id", ""),
            doc_type=kwargs["doc_type"],
            trust_label=kwargs["trust_label"],
            sha256=kwargs["sha256"],
            mime=kwargs.get("mime", ""),
            model_path=kwargs.get("model_path") or [],
            models_used=kwargs.get("models_used") or {},
            jev_decision=kwargs.get("jev_decision") or {},
            flags=kwargs.get("flags") or [],
            prompt_version=kwargs.get("prompt_version", ""),
            confidence=kwargs.get("confidence", 0.0),
            reply_text=kwargs.get("reply_text", ""),
            raw_extraction=kwargs.get("raw_extraction") or {},
        )
        children: list[Any] = []
        for position, row in enumerate(kwargs.get("rows") or []):
            children.append(
                ShopStockBookRow(
                    id=_new_id(),
                    document_id=document_id,
                    position=position,
                    item=row.get("item", ""),
                    opening=row.get("opening"),
                    supplied=row.get("supplied"),
                    sold=row.get("sold"),
                    left=row.get("left"),
                    price_naira=row.get("price_naira"),
                    crossed_out_old=row.get("crossed_out_old"),
                    crossed_out_new=row.get("crossed_out_new"),
                    unclear=row.get("unclear") or [],
                    confidence=row.get("confidence", 0.0),
                )
            )
        for position, line in enumerate(kwargs.get("lines") or []):
            children.append(
                ShopInvoiceLine(
                    id=_new_id(),
                    document_id=document_id,
                    position=position,
                    item=line.get("item", ""),
                    qty=line.get("qty"),
                    unit_price_naira=line.get("unit_price_naira"),
                    line_total_naira=line.get("line_total_naira"),
                )
            )
        for position, note in enumerate(kwargs.get("notes") or []):
            children.append(
                ShopNote(
                    id=_new_id(),
                    document_id=document_id,
                    position=position,
                    text=note.get("text", ""),
                    kind=note.get("kind", "other"),
                    party=note.get("party", ""),
                    amount_naira=note.get("amount_naira"),
                    due=note.get("due"),
                )
            )

        factory = get_session_factory()
        async with factory() as session:
            session.add(document)
            session.add_all(children)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                # A concurrent read of the same file won: return the original.
                existing = await self.find_by_sha(kwargs["user_id"], kwargs["sha256"])
                if existing is not None:
                    return existing
                raise
        return _to_stored(document)


def _new_id() -> str:
    import uuid

    return str(uuid.uuid4())


def _to_stored(row: Any) -> StoredDocument:
    return StoredDocument(
        id=row.id,
        doc_type=row.doc_type,
        trust_label=row.trust_label,
        reply_text=row.reply_text,
        confidence=float(row.confidence or 0.0),
        model_path=list(row.model_path or []),
        flags=list(row.flags or []),
    )


def default_store() -> ShopDocumentStore:
    """The Postgres store, unless the process asked for the in-memory one."""
    import os

    if os.getenv("SHOP_DOC_STORE", "").lower() == "memory":
        return InMemoryShopDocumentStore()
    return PostgresShopDocumentStore()


__all__ = [
    "InMemoryShopDocumentStore",
    "PostgresShopDocumentStore",
    "ShopDocumentStore",
    "StoredDocument",
    "default_store",
]

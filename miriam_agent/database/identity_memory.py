"""Channel-identity memory operations, mixed into ``MemoryStore``.

Moved verbatim out of :mod:`miriam_agent.database.memory` (blob-ratchet split):
handle normalization, identity linking/resolution/listing, and
``merge_user_data``. Mixed into ``MemoryStore``; the runtime surface is
unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from miriam_agent.core.exceptions import AuthorizationError
from miriam_agent.core.timeutil import utcnow_naive
from miriam_agent.database.models import (
    ChannelIdentity,
    Conversation,
    FinancialProfile,
    MemoryEntry,
)


class IdentityMemoryMixin:
    """Channel identities: a stable user across changing numbers/handles.

    Mixed into :class:`miriam_agent.database.memory.MemoryStore`, which
    provides the runtime surface declared below.
    """

    _session: Callable[[], AbstractAsyncContextManager[AsyncSession]]

    @staticmethod
    def normalize_identity(channel: str, handle: str) -> tuple[str, str]:
        """Normalize a channel handle for stable lookup.

        Channel is lowercased; the handle is lowercased, stripped, and for
        phone-like handles whitespace/dashes/parens are removed so
        ``+1 (555) 123-4567`` and ``+15551234567`` bind the same row.
        """
        import re

        ch = (channel or "").strip().lower()
        h = (handle or "").strip().lower()
        if ch in ("imessage", "whatsapp", "sms", "terminal"):
            h = re.sub(r"[\s\-().]", "", h)
        return ch, h

    async def link_identity(
        self, user_id: str, channel: str, handle: str, verified: bool = False
    ) -> ChannelIdentity:
        """Bind a channel handle to a user (first-seen auto-link).

        Idempotent for the owner; raises AuthorizationError when the handle
        already points at a *different* user — that move needs the
        rail-authenticated merge path, not a bare user call.
        """
        from sqlalchemy.exc import IntegrityError

        ch, h = self.normalize_identity(channel, handle)
        if not ch or not h:
            raise ValueError("link_identity requires a channel and handle")
        async with self._session() as session:
            existing = (
                (
                    await session.execute(
                        select(ChannelIdentity).where(
                            ChannelIdentity.channel == ch, ChannelIdentity.handle == h
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                if existing.user_id != user_id:
                    raise AuthorizationError(
                        "Handle is linked to a different user; "
                        "recover it through the verified merge endpoint"
                    )
                existing.last_seen_at = utcnow_naive()
                if verified and not existing.verified:
                    existing.verified = True
                await session.commit()
                return existing
            row = ChannelIdentity(
                user_id=user_id, channel=ch, handle=h, verified=verified
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = (
                    (
                        await session.execute(
                            select(ChannelIdentity).where(
                                ChannelIdentity.channel == ch,
                                ChannelIdentity.handle == h,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing is None:
                    raise
                if existing.user_id != user_id:
                    raise AuthorizationError(
                        "Handle is linked to a different user; "
                        "recover it through the verified merge endpoint"
                    )
                return existing
            return row

    async def resolve_user_for_identity(self, channel: str, handle: str) -> str | None:
        """Return the stable user_id a handle points at, if any."""
        ch, h = self.normalize_identity(channel, handle)
        if not ch or not h:
            return None
        async with self._session() as session:
            row = (
                (
                    await session.execute(
                        select(ChannelIdentity).where(
                            ChannelIdentity.channel == ch, ChannelIdentity.handle == h
                        )
                    )
                )
                .scalars()
                .first()
            )
            return row.user_id if row is not None else None

    async def list_identities(self, user_id: str) -> list[ChannelIdentity]:
        """All handles bound to a user."""
        async with self._session() as session:
            rows = await session.execute(
                select(ChannelIdentity)
                .where(ChannelIdentity.user_id == user_id)
                .order_by(ChannelIdentity.created_at)
            )
            return list(rows.scalars())

    async def merge_user_data(
        self, from_user_id: str, to_user_id: str
    ) -> dict[str, int]:
        """Move portable history from one stable id to another.

        Called only behind the rail service key after Go verified ownership
        (OTP to the old number / email match / wallet signature). Moves
        conversations, memory entries and channel handles. Audit rows stay
        with the original id for compliance; the financial profile moves
        only when the target has none.
        """
        if not from_user_id or not to_user_id or from_user_id == to_user_id:
            raise ValueError("merge_user_data requires two distinct user ids")
        counts = {"conversations": 0, "memories": 0, "identities": 0}
        async with self._session() as session:
            convs = (
                (
                    await session.execute(
                        select(Conversation).where(Conversation.user_id == from_user_id)
                    )
                )
                .scalars()
                .all()
            )
            for conv in convs:
                conv.user_id = to_user_id
                counts["conversations"] += 1
            mems = (
                (
                    await session.execute(
                        select(MemoryEntry).where(MemoryEntry.user_id == from_user_id)
                    )
                )
                .scalars()
                .all()
            )
            for mem in mems:
                mem.user_id = to_user_id
                counts["memories"] += 1
            handles = (
                (
                    await session.execute(
                        select(ChannelIdentity).where(
                            ChannelIdentity.user_id == from_user_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            for ident in handles:
                ident.user_id = to_user_id
                counts["identities"] += 1
            profile_from = (
                (
                    await session.execute(
                        select(FinancialProfile).where(
                            FinancialProfile.user_id == from_user_id
                        )
                    )
                )
                .scalars()
                .first()
            )
            profile_to = (
                (
                    await session.execute(
                        select(FinancialProfile).where(
                            FinancialProfile.user_id == to_user_id
                        )
                    )
                )
                .scalars()
                .first()
            )
            if profile_from is not None and profile_to is None:
                profile_from.user_id = to_user_id
            await session.commit()
            return counts

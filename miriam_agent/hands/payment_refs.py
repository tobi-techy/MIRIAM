"""Durable dedupe for inbound payments (G23 / C052).

The old guard was a hash of the raw alert text (``inflow_id_for_alert``): the
same transfer worded differently, or sent as a different screenshot, hashed
differently and counted twice. This module owns the real rule.

What counts as the same payment -- the structured key, never the raw text:

    (channel, sender_handle, amount_minor, reference)

* channel        -- where the money was observed ("gtbank", "opay", "rail").
* sender_handle  -- who sent it (the account name/alias on the alert).
* amount_minor   -- the amount in the currency's minor unit (kobo for NGN).
* reference      -- the rail/bank reference for the transfer.

The raw payload is retained on the row for dispute replay, but it is *not* part
of the key. Two wordings of one transfer collide; two genuinely different
transfers that merely share a sender and amount do not (their references
differ).

The claim is one INSERT ... ON CONFLICT DO NOTHING on the four key columns:
exactly one racer's row lands and the row count tells the caller won/lost
atomically. A unique violation means "already recorded": the caller moves no
money and returns the original receipt.

The ledger id for the split is derived from the same four columns
(:func:`inflow_id_for_payment`), so a duplicate can find the original receipt in
the ledger with the ordinary ``receipt_for`` replay and change nothing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

__all__ = [
    "InMemoryPaymentRefStore",
    "PaymentRefClaim",
    "PaymentRefKey",
    "PaymentRefStore",
    "PostgresPaymentRefStore",
    "amount_to_minor",
    "inflow_id_for_payment",
    "payload_hash",
]


def payload_hash(raw: str) -> str:
    """Stable hash of the raw payload. Retained for dispute, never keyed."""
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()[:32]


def amount_to_minor(amount: Decimal | int | str) -> int:
    """Amount in minor units (kobo for NGN). Two-decimal, half-up."""
    value = Decimal(str(amount))
    return int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _normalise(value: str) -> str:
    return (value or "").strip().casefold()


@dataclass(frozen=True)
class PaymentRefKey:
    """The four columns that decide "same payment". Never the raw text."""

    channel: str
    sender_handle: str
    amount_minor: int
    reference: str

    @classmethod
    def of(
        cls,
        *,
        channel: str,
        sender_handle: str,
        amount_minor: int,
        reference: str,
    ) -> PaymentRefKey:
        return cls(
            channel=_normalise(channel),
            sender_handle=_normalise(sender_handle),
            amount_minor=int(amount_minor),
            reference=_normalise(reference),
        )

    @property
    def complete(self) -> bool:
        """A key with no reference cannot distinguish two real payments.

        Without a reference the only safe dedupe is the rail's own payment id,
        which the caller passes as ``inflow_id``; the table is then written but
        the caller keeps its own idempotency semantics.
        """
        return bool(self.reference)

    def as_tuple(self) -> tuple[str, str, int, str]:
        return (self.channel, self.sender_handle, self.amount_minor, self.reference)


def inflow_id_for_payment(key: PaymentRefKey) -> str:
    """Deterministic ledger id for a payment, from the structured key.

    Deterministic so a duplicate -- which arrives with a different rail
    ``payment_id`` but the same structured key -- resolves to the *same* ledger
    inflow id and the ordinary ``receipt_for`` replay returns the original
    receipt.
    """
    digest = hashlib.sha256(
        "|".join(
            [
                key.channel,
                key.sender_handle,
                str(key.amount_minor),
                key.reference,
            ]
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"inflow_{digest}"


@dataclass(frozen=True)
class PaymentRefClaim:
    """The outcome of claiming a payment key."""

    # True when some earlier claim already recorded this payment.
    duplicate: bool


class PaymentRefStore(Protocol):
    """Where payment-reference claims live between calls."""

    async def claim(
        self,
        *,
        key: PaymentRefKey,
        payload_hash: str,
        raw_payload: str,
    ) -> PaymentRefClaim: ...


class InMemoryPaymentRefStore:
    """Process-local store. The default, and what the tests use.

    Mirrors the Postgres insert-or-conflict: the first ``claim()`` for a key
    wins; later ones observe. No ``await`` sits between the check and the
    write, so concurrent claims on one event loop are atomic.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, int, str], str] = {}

    async def claim(
        self,
        *,
        key: PaymentRefKey,
        payload_hash: str,  # noqa: ARG002 - retained for parity with Postgres
        raw_payload: str,  # noqa: ARG002 - retained for parity with Postgres
    ) -> PaymentRefClaim:
        if key.as_tuple() in self._rows:
            return PaymentRefClaim(duplicate=True)
        self._rows[key.as_tuple()] = payload_hash
        return PaymentRefClaim(duplicate=False)


class PostgresPaymentRefStore:
    """Shared store for multi-worker deployments, backed by payment_references.

    ``claim()`` is a single INSERT ... ON CONFLICT DO NOTHING on the four key
    columns: exactly one racer's row lands, and the row count tells the caller
    won/lost atomically. The engine is created lazily so importing this module
    never opens a connection.
    """

    def __init__(self, database_url: str | None = None, *, _insert=None):
        self.database_url = database_url
        # Dialect-specific INSERT with ON CONFLICT DO NOTHING. Production is
        # Postgres (asyncpg); tests inject the SQLite dialect against a real
        # SQLite engine to prove the same claim semantics end to end.
        if _insert is None:
            from sqlalchemy.dialects.postgresql import insert as _insert
        self._insert = _insert
        self._engine = None
        self._sessions = None

    def _require_url(self) -> str:
        url = self.database_url
        if not url:
            from miriam_agent.config.settings import get_settings

            url = get_settings().DATABASE_URL
        return url

    async def _session_factory(self):
        if self._sessions is None:
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

            from miriam_agent.database.models import Base

            self._engine = create_async_engine(self._require_url())
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        return self._sessions

    async def claim(
        self,
        *,
        key: PaymentRefKey,
        payload_hash: str,
        raw_payload: str,
    ) -> PaymentRefClaim:
        import uuid

        from miriam_agent.database.models import PaymentReference

        sessions = await self._session_factory()
        stmt = (
            self._insert(PaymentReference)
            .values(
                id=str(uuid.uuid4()),
                channel=key.channel,
                sender_handle=key.sender_handle,
                amount_minor=key.amount_minor,
                reference=key.reference,
                payload_hash=payload_hash,
                raw_payload=raw_payload,
                status="reported",
            )
            .on_conflict_do_nothing(
                index_elements=[
                    "channel",
                    "sender_handle",
                    "amount_minor",
                    "reference",
                ]
            )
        )
        async with sessions() as session:
            result = await session.execute(stmt)
            await session.commit()
            return PaymentRefClaim(duplicate=result.rowcount == 0)

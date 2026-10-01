"""Layer 1 - HANDS. Durable exactly-once journal for money movements.

The arbiter that closes the check-then-act race in ``execute_transfer``. The
ledger's ``processed`` map is checked-then-acted on: two workers can both read
"key absent" before either saves, and the CAS version check catches the ledger
overwrite but NOT the double rail call -- both rails can fire first.

The journal fixes this by making the claim atomic: ``reserve()`` inserts the
idempotency key row, and exactly one racer wins. The winner calls the rail;
the loser replays. The claim is written BEFORE the rail call, in a store
independent of the ledger body, so a crash between rail and ledger-save still
leaves proof the key was claimed.

Lifecycle: ``reserved`` -> ``dispatched`` -> ``confirmed`` | ``failed``.
``reserve()`` is the only creator; ``mark_*`` only move forward. The journal
never stores balances and never becomes the money record -- the ledger stays
the single source of truth for money. This module stores facts; it makes no
decisions and calls no models.

Fail-closed by contract: if the journal is unavailable, ``reserve()`` raises
and the caller must refuse the movement, never execute it. An unknown
journal state must refuse sooner, not move more.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

logger = logging.getLogger(__name__)

# Lifecycle states. Forward-only: reserved -> dispatched -> confirmed|failed.
RESERVED = "reserved"
DISPATCHED = "dispatched"
CONFIRMED = "confirmed"
FAILED = "failed"

_TERMINAL = frozenset({CONFIRMED, FAILED})


@dataclass(frozen=True)
class JournalClaim:
    """The outcome of attempting to claim an idempotency key."""

    # True when this caller won the claim and may proceed to the rail.
    won: bool
    # The lifecycle status observed (reserved/dispatched/confirmed/failed).
    status: str
    # Rail reference, when a previous attempt recorded one.
    rail_reference: str = ""


class ExecutionJournal(Protocol):
    """Where idempotency claims live between calls."""

    async def reserve(
        self,
        *,
        key: str,
        user_id: str,
        action: str,
        amount: Decimal,
        currency: str,
        counterparty: str,
        sleeve: str,
        decision_id: str,
    ) -> JournalClaim: ...

    async def mark_dispatched(self, *, key: str, rail_reference: str = "") -> None: ...

    async def mark_confirmed(self, *, key: str, rail_reference: str = "") -> None: ...

    async def mark_failed(self, *, key: str, detail: str = "") -> None: ...

    async def status_of(self, *, key: str) -> JournalClaim | None: ...


class InMemoryExecutionJournal:
    """Process-local journal. The default, and what the tests use.

    Single-threaded claim semantics mirror the Postgres insert-or-conflict:
    the first ``reserve()`` for a key wins; later ones observe. Like
    ``InMemoryLedgerStore`` this is process-local, so multi-worker
    deployments must use ``PostgresExecutionJournal``.
    """

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, str]] = {}

    async def reserve(self, **fields) -> JournalClaim:  # noqa: ANN003
        key = str(fields["key"])
        existing = self._rows.get(key)
        if existing is not None:
            return JournalClaim(
                won=False,
                status=existing["status"],
                rail_reference=existing.get("rail_reference", ""),
            )
        self._rows[key] = {
            "status": RESERVED,
            "rail_reference": "",
            "user_id": str(fields.get("user_id", "")),
        }
        return JournalClaim(won=True, status=RESERVED)

    async def mark_dispatched(self, *, key: str, rail_reference: str = "") -> None:
        row = self._rows.get(key)
        if row is None or row["status"] in _TERMINAL:
            return
        row["status"] = DISPATCHED
        if rail_reference:
            row["rail_reference"] = rail_reference

    async def mark_confirmed(self, *, key: str, rail_reference: str = "") -> None:
        row = self._rows.get(key)
        if row is None or row["status"] in _TERMINAL:
            return
        row["status"] = CONFIRMED
        if rail_reference:
            row["rail_reference"] = rail_reference

    async def mark_failed(self, *, key: str, detail: str = "") -> None:
        row = self._rows.get(key)
        if row is None or row["status"] in _TERMINAL:
            return
        row["status"] = FAILED

    async def status_of(self, *, key: str) -> JournalClaim | None:
        row = self._rows.get(key)
        if row is None:
            return None
        return JournalClaim(
            won=False,
            status=row["status"],
            rail_reference=row.get("rail_reference", ""),
        )


class PostgresExecutionJournal:
    """Shared journal for multi-worker deployments. Backed by money_executions.

    ``reserve()`` is a single INSERT ... ON CONFLICT DO NOTHING: exactly one
    racer's row lands, and the row count tells the caller won/lost atomically.
    ``mark_*`` are conditional forward-only UPDATEs. The engine is created
    lazily so importing this module never opens a connection.
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

    async def reserve(self, **fields) -> JournalClaim:  # noqa: ANN003
        from miriam_agent.database.models import MoneyExecution

        sessions = await self._session_factory()
        stmt = (
            self._insert(MoneyExecution)
            .values(
                idempotency_key=str(fields["key"]),
                user_id=str(fields.get("user_id", "")),
                action=str(fields.get("action", "")),
                amount=str(fields.get("amount", "")),
                currency=str(fields.get("currency", "")),
                counterparty=str(fields.get("counterparty", "")),
                sleeve=str(fields.get("sleeve", "")),
                decision_id=str(fields.get("decision_id", "")),
                status=RESERVED,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
        )
        async with sessions() as session:
            result = await session.execute(stmt)
            await session.commit()
            if result.rowcount == 1:
                return JournalClaim(won=True, status=RESERVED)
            observed = await self.status_of(key=str(fields["key"]))
            if observed is None:  # pragma: no cover - cannot happen post-commit
                raise RuntimeError("journal lost a committed claim row")
            return observed

    async def _transition(
        self, *, key: str, to: str, rail_reference: str = "", detail: str = ""
    ) -> None:
        from sqlalchemy import update

        from miriam_agent.database.models import MoneyExecution

        sessions = await self._session_factory()
        values: dict[str, object] = {
            "status": to,
            "updated_at": datetime.now(UTC).replace(tzinfo=None),
        }
        if rail_reference:
            values["rail_reference"] = rail_reference
        if detail:
            values["detail"] = detail[:500]
        async with sessions() as session:
            await session.execute(
                update(MoneyExecution)
                .where(
                    MoneyExecution.idempotency_key == key,
                    MoneyExecution.status.not_in(_TERMINAL),
                )
                .values(**values)
            )
            await session.commit()

    async def mark_dispatched(self, *, key: str, rail_reference: str = "") -> None:
        await self._transition(key=key, to=DISPATCHED, rail_reference=rail_reference)

    async def mark_confirmed(self, *, key: str, rail_reference: str = "") -> None:
        await self._transition(key=key, to=CONFIRMED, rail_reference=rail_reference)

    async def mark_failed(self, *, key: str, detail: str = "") -> None:
        await self._transition(key=key, to=FAILED, detail=detail)

    async def status_of(self, *, key: str) -> JournalClaim | None:
        from sqlalchemy import select

        from miriam_agent.database.models import MoneyExecution

        sessions = await self._session_factory()
        async with sessions() as session:
            row = (
                await session.execute(
                    select(MoneyExecution).where(MoneyExecution.idempotency_key == key)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return JournalClaim(
                won=False,
                status=row.status,
                rail_reference=row.rail_reference or "",
            )

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessions = None


__all__ = [
    "CONFIRMED",
    "DISPATCHED",
    "FAILED",
    "RESERVED",
    "ExecutionJournal",
    "InMemoryExecutionJournal",
    "JournalClaim",
    "PostgresExecutionJournal",
]

"""The Postgres dedupe path (G23/C052), proved end to end on a real database.

``PostgresPaymentRefStore.claim`` is one ``INSERT ... ON CONFLICT DO NOTHING``
on the four key columns ``(channel, sender_handle, amount_minor, reference)``;
``rowcount == 0`` is the duplicate signal. That insert *is* the dedupe
guarantee for real money, and until now it ran only through
``InMemoryPaymentRefStore`` in CI. These tests run it against Postgres (the
deployment dialect), where ``ON CONFLICT`` and ``rowcount`` semantics are the
thing under test -- SQLite would not prove the Postgres conflict/rowcount
behaviour, only a lookalike.

Each test uses a fresh key (a random reference) so no test can be answered by
another test's row, and counts rows for that exact key on a separate
connection. Postgres is expected at ``localhost:5432`` with the credentials in
``tests`` -- the same service CI runs; the module skips (never silently
passes) if it cannot connect, and a skipped node id does not count as proven
evidence in the capability catalog.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from layer_fakes import FakeProvider, jev, judge_of

from miriam_agent.hands.ledger import (
    InMemoryLedgerStore,
    Ledger,
    RentFirst,
    Track,
    money,
)
from miriam_agent.hands.limits import Policy
from miriam_agent.hands.payment_refs import (
    PaymentRefKey,
    PostgresPaymentRefStore,
)
from miriam_agent.hands.transfer import InMemoryRail

# The SQLAlchemy URL the store opens (asyncpg dialect); the raw URL the test
# uses to count rows out of band.
_SQLALCHEMY_URL = os.environ.get(
    "PAYMENT_REFS_TEST_DATABASE_URL",
    "postgresql+asyncpg://miriam:miriam_password@localhost:5432/miriam_test",
)
_RAW_URL = _SQLALCHEMY_URL.replace("+asyncpg", "")


def _key(*, reference: str, amount_minor: int = 42_000_000) -> PaymentRefKey:
    return PaymentRefKey.of(
        channel="gtbank",
        sender_handle="acme payroll",
        amount_minor=amount_minor,
        reference=reference,
    )


async def _count(key: PaymentRefKey) -> int:
    """Count rows for one exact key, on the test's own connection."""
    import asyncpg

    conn = await asyncpg.connect(_RAW_URL)
    try:
        return await conn.fetchval(
            "SELECT count(*) FROM payment_references "
            "WHERE channel=$1 AND sender_handle=$2 AND amount_minor=$3 "
            "AND reference=$4",
            key.channel,
            key.sender_handle,
            key.amount_minor,
            key.reference,
        )
    finally:
        await conn.close()


@pytest.fixture
async def store():
    """A Postgres-backed store with the schema ensured, disposed afterwards."""
    st = PostgresPaymentRefStore(_SQLALCHEMY_URL)
    try:
        # Opens the engine and runs create_all; the same call the first claim
        # makes, so a failure here is the real "Postgres unreachable" signal.
        await st._session_factory()
    except Exception as exc:  # noqa: BLE001 - any connect failure means skip
        pytest.skip(f"Postgres is not reachable at {_RAW_URL}: {exc!r}")
    try:
        yield st
    finally:
        if st._engine is not None:
            await st._engine.dispose()


async def test_first_claim_writes_a_row_and_reports_claimed(store):
    key = _key(reference=f"ref-{uuid.uuid4().hex}")

    claim = await store.claim(key=key, payload_hash="h", raw_payload="raw")

    assert claim.duplicate is False
    assert await _count(key) == 1


async def test_an_identical_replay_is_a_no_op_duplicate(store):
    key = _key(reference=f"ref-{uuid.uuid4().hex}")

    first = await store.claim(key=key, payload_hash="h1", raw_payload="one")
    second = await store.claim(key=key, payload_hash="h2", raw_payload="two")

    assert first.duplicate is False
    assert second.duplicate is True
    assert await _count(key) == 1


async def test_the_same_reference_with_a_different_amount_is_a_distinct_claim(store):
    reference = f"ref-{uuid.uuid4().hex}"
    small = _key(reference=reference, amount_minor=10_000)
    large = _key(reference=reference, amount_minor=20_000)

    first = await store.claim(key=small, payload_hash="h", raw_payload="raw")
    second = await store.claim(key=large, payload_hash="h", raw_payload="raw")

    assert first.duplicate is False
    assert second.duplicate is False
    assert await _count(small) == 1
    assert await _count(large) == 1


async def test_a_different_reference_writes_a_second_row(store):
    first_key = _key(reference=f"ref-{uuid.uuid4().hex}")
    second_key = _key(reference=f"ref-{uuid.uuid4().hex}")

    first = await store.claim(key=first_key, payload_hash="h", raw_payload="raw")
    second = await store.claim(key=second_key, payload_hash="h", raw_payload="raw")

    assert first.duplicate is False
    assert second.duplicate is False
    assert await _count(first_key) == 1
    assert await _count(second_key) == 1


async def test_two_concurrent_claims_leave_exactly_one_row(store):
    """Two racers on separate connections: one wins, one observes, one row.

    Each racer gets its own store, and so its own engine and connection -- a
    distinct database backend, not merely a distinct session on a shared one.
    The unique index plus ``ON CONFLICT DO NOTHING`` is what makes exactly one
    row land; ``rowcount`` tells each racer which side it was on.
    """
    other = PostgresPaymentRefStore(_SQLALCHEMY_URL)
    await other._session_factory()
    key = _key(reference=f"ref-{uuid.uuid4().hex}")
    try:
        first, second = await asyncio.gather(
            store.claim(key=key, payload_hash="h", raw_payload="a"),
            other.claim(key=key, payload_hash="h", raw_payload="b"),
        )

        outcomes = sorted([first.duplicate, second.duplicate])
        assert outcomes == [False, True]
        assert await _count(key) == 1
    finally:
        if other._engine is not None:
            await other._engine.dispose()


async def test_the_claim_row_is_written_before_the_split_runs(monkeypatch):
    """On the inflow path the claim lands before ``split_inflow`` touches money."""
    import miriam_agent.orchestrator as orch_mod
    from miriam_agent.orchestrator import Orchestrator

    store = PostgresPaymentRefStore(_SQLALCHEMY_URL)
    try:
        await store._session_factory()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres is not reachable at {_RAW_URL}: {exc!r}")

    key = _key(reference=f"ref-{uuid.uuid4().hex}")
    observed: dict[str, int] = {}

    real_split = orch_mod.split_inflow

    async def _spying_split(*args, **kwargs):
        observed["rows_before_split"] = await _count(key)
        return await real_split(*args, **kwargs)

    monkeypatch.setattr(orch_mod, "split_inflow", _spying_split)

    ledger_store = InMemoryLedgerStore()
    ledger = Ledger(
        user_id="u-pg",
        track=Track(name="70/30", spend=Decimal("70"), save=Decimal("30")),
    )
    ledger.sleeves = {
        "spendable": money(0),
        "savings": money(0),
        "yield": money(0),
        "locked": money(0),
    }
    ledger.rent_first = RentFirst(required=money(150000), reserved=money(0))
    ledger_store.seed(ledger)

    orchestrator = Orchestrator(
        store=ledger_store,
        policy=Policy(
            max_auto=money(2000),
            max_with_confirm=money(100000),
            reversible_under=money(2000),
        ),
        rail=InMemoryRail(),
        provider=FakeProvider("Noted."),
        judge=judge_of(jev(intent="order", afford=0.9)),
        payment_refs=store,
    )

    try:
        result = await orchestrator.handle_inflow(
            "u-pg",
            payment_id="pay_before_split",
            amount=Decimal("420000"),
            source_raw="Credit alert: NGN420,000.00 from ACME PAYROLL",
            channel=key.channel,
            sender_handle=key.sender_handle,
            reference=key.reference,
        )

        # The spy ran, and at that moment the claim row already existed.
        assert observed.get("rows_before_split") == 1
        assert result.receipt is not None
        assert result.receipt.action == "inflow_split"
        assert await _count(key) == 1
    finally:
        if store._engine is not None:
            await store._engine.dispose()

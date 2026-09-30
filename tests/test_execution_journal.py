"""Phase B regression tests: exactly-once journal + reconciliation.

Proves the three Phase B guarantees without network, Postgres, or JEV:

* a key is claimed once: the rail fires exactly once no matter how many
  workers race it,
* a retry replays the original receipt instead of moving money again,
* a journal outage refuses the movement (fail-closed), never executes it,
* reconciliation detects every drift direction and changes nothing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from layer_fakes import POLICY, ledger_with

from miriam_agent.hands.audit import Receipt
from miriam_agent.hands.execution_journal import (
    CONFIRMED,
    DISPATCHED,
    FAILED,
    RESERVED,
    InMemoryExecutionJournal,
)
from miriam_agent.hands.ledger import InMemoryLedgerStore, money
from miriam_agent.hands.reconcile import reconcile
from miriam_agent.hands.state import ProposedAction, build_state
from miriam_agent.hands.transfer import InMemoryRail, RailOutcome, execute_transfer

NOW = datetime.now(UTC)


async def _seeded(ledger):
    store = InMemoryLedgerStore()
    store.seed(ledger)
    return store


def _authorised(ledger, amount, counterparty="Femi"):
    action = ProposedAction(
        type="transfer", amount=money(amount), counterparty=counterparty
    )
    state = build_state(
        ledger=ledger,
        policy=POLICY,
        proposed_action=action,
        decision={"id": "dec_test", "next_mode": "act", "action_choice": "allow"},
    )
    return state, action


def _receipt(key, *, status="executed", ref="ref_1"):
    return Receipt(
        id="rcpt_test",
        at=NOW,
        status=status,
        action="transfer",
        currency="NGN",
        amount=money(1500),
        counterparty="Femi",
        sleeve="spendable",
        decision_id="dec_test",
        idempotency_key=key,
        rail_reference=ref,
    )


class TestJournalLifecycle:
    async def test_first_reserve_wins_second_observes(self):
        journal = InMemoryExecutionJournal()
        kw = dict(
            key="k",
            user_id="u",
            action="transfer",
            amount=Decimal("5"),
            currency="NGN",
            counterparty="Femi",
            sleeve="spendable",
            decision_id="d",
        )
        first = await journal.reserve(**kw)
        second = await journal.reserve(**kw)
        assert (first.won, first.status) == (True, RESERVED)
        assert (second.won, second.status) == (False, RESERVED)

    async def test_transitions_move_forward_only(self):
        journal = InMemoryExecutionJournal()
        kw = dict(
            key="k",
            user_id="u",
            action="transfer",
            amount=Decimal("5"),
            currency="NGN",
            counterparty="Femi",
            sleeve="spendable",
            decision_id="d",
        )
        await journal.reserve(**kw)
        await journal.mark_dispatched(key="k", rail_reference="r")
        assert (await journal.status_of(key="k")).status == DISPATCHED
        await journal.mark_confirmed(key="k")
        assert (await journal.status_of(key="k")).status == CONFIRMED
        await journal.mark_failed(key="k")  # terminal: no-op
        assert (await journal.status_of(key="k")).status == CONFIRMED

    async def test_unknown_key_is_none(self):
        assert await InMemoryExecutionJournal().status_of(key="nope") is None


class TestExactlyOnce:
    async def test_happy_path_confirms_and_replays(self):
        ledger = ledger_with(spendable=50000)
        store = await _seeded(ledger)
        state, _ = _authorised(ledger, "1500")
        journal, rail = InMemoryExecutionJournal(), InMemoryRail()
        out = await execute_transfer(
            store=store,
            ledger=ledger,
            state=state,
            policy=POLICY,
            rail=rail,
            journal=journal,
        )
        assert out.receipt.status == "executed"
        key = out.receipt.idempotency_key
        assert (await journal.status_of(key=key)).status == CONFIRMED
        again = await execute_transfer(
            store=store,
            ledger=ledger,
            state=state,
            policy=POLICY,
            rail=rail,
            journal=journal,
        )
        assert again.receipt.idempotent_replay is True
        assert len(rail.calls) == 1

    async def test_race_fires_rail_once(self):
        ledger = ledger_with(spendable=50000)
        store = await _seeded(ledger)
        state, _ = _authorised(ledger, "1500")
        journal, rail = InMemoryExecutionJournal(), InMemoryRail()
        r1, r2 = await asyncio.gather(
            execute_transfer(
                store=store,
                ledger=ledger,
                state=state,
                policy=POLICY,
                rail=rail,
                journal=journal,
            ),
            execute_transfer(
                store=store,
                ledger=ledger,
                state=state,
                policy=POLICY,
                rail=rail,
                journal=journal,
            ),
        )
        assert len(rail.calls) == 1
        assert sum(r.receipt.idempotent_replay for r in (r1, r2)) == 1

    async def test_in_flight_loser_is_refused_not_refired(self):
        class SlowRail:
            def __init__(self):
                self.calls = []

            async def execute(self, instruction):
                self.calls.append(instruction)
                await asyncio.sleep(0.2)
                return RailOutcome(ok=True, reference="slow_1")

            async def reverse(self, instruction, reference):
                return RailOutcome(ok=True, reference="rev")

        ledger = ledger_with(spendable=50000)
        store = await _seeded(ledger)
        state, _ = _authorised(ledger, "1500")
        journal, slow = InMemoryExecutionJournal(), SlowRail()
        w, loser = await asyncio.gather(
            execute_transfer(
                store=store,
                ledger=ledger,
                state=state,
                policy=POLICY,
                rail=slow,
                journal=journal,
            ),
            execute_transfer(
                store=store,
                ledger=ledger,
                state=state,
                policy=POLICY,
                rail=slow,
                journal=journal,
            ),
        )
        assert len(slow.calls) == 1
        refused = loser if loser.receipt.status == "rejected" else w
        assert "ALREADY_IN_FLIGHT" in refused.receipt.reasons

    async def test_journal_outage_fails_closed(self):
        class DeadJournal:
            async def reserve(self, **kw):
                raise ConnectionError("db down")

            async def mark_dispatched(self, **kw):
                pass

            async def mark_confirmed(self, **kw):
                pass

            async def mark_failed(self, **kw):
                pass

            async def status_of(self, **kw):
                return None

        ledger = ledger_with(spendable=50000)
        store = await _seeded(ledger)
        state, _ = _authorised(ledger, "1500")
        rail = InMemoryRail()
        out = await execute_transfer(
            store=store,
            ledger=ledger,
            state=state,
            policy=POLICY,
            rail=rail,
            journal=DeadJournal(),
        )
        assert out.receipt.status == "rejected"
        assert "JOURNAL_UNAVAILABLE" in out.receipt.reasons
        assert rail.calls == []

    async def test_rail_failure_marks_failed(self):
        ledger = ledger_with(spendable=50000)
        store = await _seeded(ledger)
        state, _ = _authorised(ledger, "1500")
        journal = InMemoryExecutionJournal()
        out = await execute_transfer(
            store=store,
            ledger=ledger,
            state=state,
            policy=POLICY,
            rail=InMemoryRail(fail_with="broke"),
            journal=journal,
        )
        assert out.receipt.status == "rejected"
        assert "RAIL_FAILED" in out.receipt.reasons
        key = out.receipt.idempotency_key
        assert (await journal.status_of(key=key)).status == FAILED


class _RailRefs:
    def __init__(self, refs):
        self._refs = set(refs)

    async def references(self):
        return set(self._refs)


class TestReconciliation:
    async def test_clean_run_has_no_findings(self):
        report = await reconcile(
            journal_rows=[
                {"idempotency_key": "k1", "status": CONFIRMED, "rail_reference": "r1"}
            ],
            ledger_receipts=[_receipt("k1", ref="r1")],
            ledger_processed={"k1": "rcpt_test"},
            rail=_RailRefs({"r1"}),
        )
        assert report.clean and report.checked == 1

    async def test_dispatched_without_receipt_is_flagged(self):
        report = await reconcile(
            journal_rows=[
                {"idempotency_key": "k1", "status": DISPATCHED, "rail_reference": "r1"}
            ],
            ledger_receipts=[],
            ledger_processed={},
            rail=_RailRefs({"r1"}),
        )
        kinds = [f.kind for f in report.findings]
        assert "JOURNAL_DISPATCHED_NO_RECEIPT" in kinds

    async def test_confirmed_without_processed_key_is_flagged(self):
        report = await reconcile(
            journal_rows=[
                {"idempotency_key": "k1", "status": CONFIRMED, "rail_reference": "r1"}
            ],
            ledger_receipts=[_receipt("k1", ref="r1")],
            ledger_processed={},
            rail=_RailRefs({"r1"}),
        )
        assert "JOURNAL_CONFIRMED_NO_PROCESSED" in [f.kind for f in report.findings]

    async def test_unknown_rail_reference_is_flagged(self):
        report = await reconcile(
            journal_rows=[
                {
                    "idempotency_key": "k1",
                    "status": CONFIRMED,
                    "rail_reference": "ghost",
                }
            ],
            ledger_receipts=[_receipt("k1", ref="ghost")],
            ledger_processed={"k1": "rcpt_test"},
            rail=_RailRefs({"r1"}),
        )
        assert "RAIL_UNKNOWN_REFERENCE" in [f.kind for f in report.findings]

    async def test_receipt_without_journal_is_flagged(self):
        report = await reconcile(
            journal_rows=[],
            ledger_receipts=[_receipt("legacy", ref="r9")],
            ledger_processed={"legacy": "rcpt_test"},
            rail=_RailRefs({"r9"}),
        )
        assert "RECEIPT_NO_JOURNAL" in [f.kind for f in report.findings]

    async def test_reconcile_changes_nothing(self):
        rows = [{"idempotency_key": "k1", "status": DISPATCHED, "rail_reference": "r1"}]
        receipts = [_receipt("k1", ref="r1")]
        processed = {"k1": "rcpt_test"}
        before = (repr(rows), repr(receipts), repr(processed))
        await reconcile(
            journal_rows=rows,
            ledger_receipts=receipts,
            ledger_processed=processed,
            rail=_RailRefs({"r1"}),
        )
        assert (repr(rows), repr(receipts), repr(processed)) == before


async def test_postgres_journal_roundtrip_sqlite(tmp_path):
    """The Postgres journal logic runs against a real database engine.

    SQLite stands in for Postgres (same SQLAlchemy code path, same
    insert-or-conflict semantics); asyncpg/Postgres is deployment config.
    """
    pytest.importorskip("aiosqlite")
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from miriam_agent.hands.execution_journal import PostgresExecutionJournal

    journal = PostgresExecutionJournal(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/j.db",
        _insert=sqlite_insert,
    )
    kw = dict(
        key="k",
        user_id="u",
        action="transfer",
        amount=Decimal("5"),
        currency="NGN",
        counterparty="Femi",
        sleeve="spendable",
        decision_id="d",
    )
    try:
        first = await journal.reserve(**kw)
        second = await journal.reserve(**kw)
        assert (first.won, first.status) == (True, RESERVED)
        assert (second.won, second.status) == (False, RESERVED)
        await journal.mark_dispatched(key="k", rail_reference="r1")
        assert (await journal.status_of(key="k")).rail_reference == "r1"
        await journal.mark_confirmed(key="k")
        assert (await journal.status_of(key="k")).status == CONFIRMED
    finally:
        await journal.close()

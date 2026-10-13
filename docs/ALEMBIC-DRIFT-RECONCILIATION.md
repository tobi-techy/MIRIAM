# Alembic drift reconciliation and production adoption

Owner decision recorded 13 Oct 2026: `memory_entries.embedding` is **dropped**
(see [SEMANTIC-MEMORY-PATH-BACK.md](SEMANTIC-MEMORY-PATH-BACK.md)).

`alembic check` found 51 items of drift between `miriam_agent/database/models.py`
and the migrations (MIRI-9): 10 JSON/JSONB types, 34 nullability, 7 indexes.
This document classifies every item, explains what the deployed database
actually is, and gives the ordered commands to adopt it — with the rollback for
each step.

**Do not merge the reconciliation PR until the pre-flights below have been run
against the deployed database.** Merging a migration is not applying it.

## 1. Provenance: which file built the deployed database

The deployed database was **not built by Alembic**. It holds the model tables,
but:

- it has **no `alembic_version` table at all** (so the migrations never ran);
- it is **missing `payment_references`** — the one model table `create_all`
  never reached;
- it has **no `vector` extension** and `memory_entries.embedding` is plain
  `json`, **all 299 rows NULL**;
- it carries **only primary and unique indexes** — none of the seven `ix_*`
  indexes migration 001 declares.

That is the signature of `Base.metadata.create_all` running on a request path
(`miriam_agent/database/memory.py`, `hands/execution_journal.py`,
`hands/payment_refs.py`, `safety/audit.py`), which builds exactly what the
models declare and silently diverges from the migrations meant to define the
schema.

The consequence that matters: **`alembic upgrade head` against the deployed
database would have failed at migration 001 on "table already exists."**
Deploys must not be wired to migrate until the adoption path below is used.

## 2. The provenance diagnostic

Run `scripts/schema_provenance.sql` (read-only):

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/schema_provenance.sql
```

What each result means:

| Result | Meaning |
|---|---|
| `alembic_version WHERE version_num = '004'`, `embedding` absent, 7 `ix_*` indexes | Alembic built it (case A). The migrations are authoritative; only the pre-flights are needed. |
| `alembic_version = '00x'` but `embedding` is `vector` or an HNSW index exists | Alembic was **stamped** onto a hand-built schema (case B). The version rows prove nothing; diff the live schema with `alembic check` before applying anything. |
| **No `alembic_version`**, ~12 tables, `embedding` is `json` or `vector`, no `ix_*` indexes | Built by `create_all` / the retired `init-db.sql` (case C) — **this is the deployed database**. Use the adoption runbook in §5. Never `alembic stamp` to skip it. |
| No `alembic_version`, 0 tables | Empty database (case D). `alembic upgrade head` (`$DATABASE_URL` pointing at it) builds it; nothing to protect. |
| Anything mixed | Treat as case B: verify with a schema dump plus `alembic check` before applying. |

## 3. The 51 items, classified

Classes: **safe** (cannot fail on existing rows), **backfill-required**
(models demand something rows may not have — pre-flight first),
**owner-decision** (the sources disagreed about intent).

Direction of reconciliation is the point of MIRI-17: **the models are the truth
about what exists; the migrations are brought up to the intended design.** Not
the reverse.

### 3a. Type changes — 10 items

The models declare SQLAlchemy's generic `JSON`; migration 001 wrote `JSONB`.
The deployed database is `json` (create_all from the models). Models and the
deployed shape agree; **001 was wrong**.

| # | column | models.py | 001 produced | deployed has | class | action |
|---|---|---|---|---|---|---|
| 1 | `users.preferences` | `JSON` | `JSONB` | `json` | safe | 001 → `JSON` |
| 2 | `financial_profiles.investment_goals` | `JSON` | `JSONB` | `json` | safe | 001 → `JSON` |
| 3 | `financial_profiles.financial_goals` | `JSON` | `JSONB` | `json` | safe | 001 → `JSON` |
| 4 | `transactions.metadata` | `JSON` | `JSONB` | `json` | safe | 001 → `JSON` |
| 5 | `messages.metadata` | `JSON` | `JSONB` | `json` | safe | 001 → `JSON` |
| 6 | `memory_entries.metadata` | `JSON` | `JSONB` | `json` | safe | 001 → `JSON` |
| 7 | `memory_entries.embedding` | `JSON` (drop) | `JSONB` | `json`, all NULL | owner-decision → **drop** | 004 drops it; 001 no longer creates it |
| 8 | `audit_logs.details` | `JSON` | `JSONB` | `json` | safe | 001 → `JSON` |
| 9 | `tool_usage.parameters` | `JSON` | `JSONB` | `json` | safe | 001 → `JSON` |
| 10 | `tool_usage.result` | `JSON` | `JSONB` | `json` | safe | 001 → `JSON` |

No live DDL: the deployed columns are already `json`. The fix is entirely in the
migration text.

### 3b. Nullability — 34 items

The models declare `Mapped[datetime]` / `Mapped[dict]` / `Mapped[bool]` (non-
`Optional`) with a Python-side default, so metadata is `NOT NULL` **and so is
every column the deployed `create_all` built**. Migration 001 created them
nullable, so the 34 are drift against 001 — not against the deployed shape.

| # | table.column | models.py | 001 produced | deployed has | class |
|---|---|---|---|---|---|
| 1 | `users.created_at` | NOT NULL | nullable | NOT NULL | backfill-required (pre-flight) |
| 2 | `users.is_active` | NOT NULL | nullable | NOT NULL | backfill-required |
| 3 | `users.preferences` | NOT NULL | nullable | NOT NULL | backfill-required |
| 4 | `channel_identities.verified` | NOT NULL | nullable | NOT NULL | backfill-required |
| 5 | `channel_identities.created_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 6 | `channel_identities.last_seen_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 7 | `conversations.created_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 8 | `conversations.updated_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 9 | `messages.metadata` | NOT NULL | nullable | NOT NULL | backfill-required |
| 10 | `messages.created_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 11 | `memory_entries.metadata` | NOT NULL | nullable | NOT NULL | backfill-required |
| 12 | `memory_entries.created_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 13 | `memory_entries.updated_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 14 | `financial_profiles.current_savings` | NOT NULL | nullable | NOT NULL | backfill-required |
| 15 | `financial_profiles.risk_tolerance` | NOT NULL | nullable | NOT NULL | backfill-required |
| 16 | `financial_profiles.investment_goals` | NOT NULL | nullable | NOT NULL | backfill-required |
| 17 | `financial_profiles.financial_goals` | NOT NULL | nullable | NOT NULL | backfill-required |
| 18 | `financial_profiles.created_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 19 | `financial_profiles.updated_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 20 | `transactions.transaction_date` | NOT NULL | nullable | NOT NULL | backfill-required |
| 21 | `transactions.created_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 22 | `transactions.metadata` | NOT NULL | nullable | NOT NULL | backfill-required |
| 23 | `investments.purchase_date` | NOT NULL | nullable | NOT NULL | backfill-required |
| 24 | `investments.acquired_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 25 | `budgets.current_spend` | NOT NULL | nullable | NOT NULL | backfill-required |
| 26 | `budgets.created_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 27 | `audit_logs.details` | NOT NULL | nullable | NOT NULL | backfill-required |
| 28 | `audit_logs.created_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 29 | `tool_usage.parameters` | NOT NULL | nullable | NOT NULL | backfill-required |
| 30 | `tool_usage.result` | NOT NULL | nullable | NOT NULL | backfill-required |
| 31 | `tool_usage.created_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 32 | `payment_references.created_at` | NOT NULL | nullable | **table absent** | backfill-required (created empty by 001) |
| 33 | `money_executions.created_at` | NOT NULL | nullable | NOT NULL | backfill-required |
| 34 | `money_executions.updated_at` | NOT NULL | nullable | NOT NULL | backfill-required |

**None of the 34 is violated in the deployed database.** `create_all` built every
one `NOT NULL`; the pre-flight (§5 step 2) proves it, and migration 003 no-ops
there. They stay `backfill-required` because a database built by the *old* 001
(or a future raw insert) can hold NULLs, and 003 must refuse rather than invent a
value. Where a backfill were genuinely needed, the proposed value is the column's
model default (`now()` for timestamps, `{}` for JSON, `false` for booleans,
`0.0`/`"medium"` for numerics/strings) — but that is an owner call, never
applied silently.

### 3c. Indexes — 7 items

The models did not declare them and the deployed database does not have them;
migration 001 created them. They index the per-user / per-conversation access
pattern the agent actually uses. **Intended design keeps them**, so they are
declared in the models and 001 creates them where absent.

| # | index | models.py | 001 produced | deployed has | class |
|---|---|---|---|---|---|
| 1 | `ix_conversations_user_id` | now `index=True` | created | absent | safe |
| 2 | `ix_messages_conversation_id` | now `index=True` | created | absent | safe |
| 3 | `ix_memory_entries_user_id` | now `index=True` | created | absent | safe |
| 4 | `ix_memory_entries_type` | now `index=True` | created | absent | safe |
| 5 | `ix_audit_logs_user_id` | now `index=True` | created | absent | safe |
| 6 | `ix_audit_logs_created_at` | now `index=True` | created | absent | safe |
| 7 | `ix_tool_usage_user_id` | now `index=True` | created | absent | safe |

`CREATE INDEX` cannot fail on existing rows and does not rewrite the table.

## 4. What each migration now does

- **`001_baseline.py`** — `create_table`/`create_index` are existence-guarded
  (`_table`, `_index`), so this revision **adopts or creates**. It writes `JSON`
  (not `JSONB`), does not create `memory_entries.embedding`, and does **not**
  `CREATE EXTENSION vector`. On the deployed database it creates only what is
  missing (`payment_references`, the 7 indexes) and touches nothing else; on an
  empty database it builds everything as before.
- **`002_payment_ref_structured_key.py`** — unchanged.
- **`003_reconcile_models_not_null.py`** — sets the 34 columns `NOT NULL`, each
  guarded: it skips a column already `NOT NULL` (idempotent) and, before any DDL,
  counts `WHERE col IS NULL` and **raises, naming the column**, if any row is
  NULL. No-op on the deployed database.
- **`004_drop_memory_embedding.py`** — drops `memory_entries.embedding` if it
  exists (the deployed database has it; a fresh one does not). Downgrade re-adds
  it as nullable `json`.

## 5. Adoption runbook — ordered commands, expected output, rollback

Run against the deployed database. `$DATABASE_URL` is your connection string; do
not commit it.

**Step 0 — snapshot.** Take a restorable backup/snapshot before anything else.
*Rollback:* restore the snapshot; nothing else below is needed.

**Step 1 — provenance.** `psql "$DATABASE_URL" -f scripts/schema_provenance.sql`
*Expect (case C):* `NO alembic_version table.`, `embedding_type = json`,
0 HNSW rows, ~12 base tables.
*Rollback:* read-only; none.

**Step 2 — pre-flight.** `psql "$DATABASE_URL" -f scripts/not_null_preflight.sql`
*Expect:* 33 rows (payment_references is skipped — it does not exist yet), every
`null_count = 0`.
*If any non-zero:* stop. Backfill that column (owner decides the value) or leave
003 unapplied; 003 will refuse it.
*Rollback:* read-only; none.

**Step 3 — adopt.** `DATABASE_URL=... alembic upgrade head`
(env `DATABASE_URL`; the revision chain resolves it.)
*Expect:*
```
Running upgrade  -> 001, Baseline: all tables from models.py — created only where absent.
Running upgrade 001 -> 002, G23/C052: dedupe inbound payments on the structured key, not the payload hash.
Running upgrade 002 -> 003, 003: align the 34 columns the models declare NOT NULL.
Running upgrade 003 -> 004, 004: drop the unused memory_entries.embedding column.
```
No "table already exists". Each migration runs in one transaction, so a failure
rolls back its own step — there is no half-applied state to repair.
*Rollback:* `alembic downgrade 002` (re-adds `embedding json`, drops the NOT
NULLs). To return fully to the pre-adoption shape, additionally
`DROP TABLE alembic_version;` (metadata only), and optionally drop
`payment_references` and the 7 `ix_*` indexes that 001 added — none of that
touches rows.
**Never run `alembic downgrade base` on the deployed database:** 001's downgrade
would drop the model tables and every row in them.

**Step 4 — verify.** `psql "$DATABASE_URL" -f scripts/schema_provenance.sql` and
row counts.
*Expect:* `alembic_version = 004`; 14 base tables; `embedding` gone;
7 `ix_*` indexes; `payment_references` present (empty). Row counts
(users, conversations, messages, memory_entries, audit_logs, …) unchanged.

**Step 5 — confirm the models match.** `DATABASE_URL=... alembic check`
*Expect:* `No new upgrade operations detected.`

**Step 6 — deploy the constrained code.** Only after step 3, deploy the build
that gates the request-path `create_all` (otherwise a missing
`payment_references` would error the payment path).
*Rollback:* redeploy the previous image; it reverts only the gate, not the schema.

## 6. Why the deployed database drifted, and the gate on it

Four request paths called `Base.metadata.create_all` unconditionally. In
production that creates a missing table silently — which is how the deployed
schema came to hold the model tables with no migration history and a missing
`payment_references`. `miriam_agent/database/bootstrap.py::create_schema_if_allowed`
is now the single call site: dev and test are unchanged; in production it
**refuses the DDL** and logs at ERROR, naming the missing tables, pointing at
`alembic upgrade head`. A missing table becomes a visible deploy fault, not a
silent repair.

What it would have caught here: the deployed database is missing
`payment_references` while `models.py` declares it, and has no `alembic_version`.
The gate logs both loud instead of quietly papering over the gap at the next
request.

## 7. CI gate

`.github/workflows/ci.yml` job **`alembic-check`**: on plain `postgres:15` it
runs `alembic upgrade head` then `alembic check`, failing on **any** difference
between `models.py` and the migrations. There is **no baseline** — every one of
the 51 items is reconciled at head, so nothing is excused and the gate cannot
silently grow. If an intentional difference ever must remain, the exact command
to widen it goes in this table first.

## 8. Not verified here

- The adoption runbook is proved on a database **reconstructed** from the
  described deployed shape (create_all, missing `payment_references`, vestigial
  `json` embedding, no `ix_*` indexes) with realistic rows — not on the deployed
  database itself, which this thread cannot reach. Row counts and schema are
  checked before and after; see the PR.
- `scripts/init-db.sql` still runs `CREATE EXTENSION IF NOT EXISTS vector` for
  the container-booted database. That file is out of this task's scope (another
  thread owns the Docker bootstrap) but it now contradicts the owner decision
  that the Python agent's database carries no `vector` extension — worth
  removing there.
- Whether the deployed database's `alembic_version` should be created by running
  003/004 at all is the owner's call; the pre-flights exist so that call is made
  on numbers, not hope.

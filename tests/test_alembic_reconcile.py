"""The schema reconciliation is proved on real, non-empty databases.

The deployed database was built by ``Base.metadata.create_all`` on a request
path, not by Alembic: it holds the model tables with no ``alembic_version`` and
is missing ``payment_references``. ``alembic check`` compared that reality to the
migrations and found 51 items of drift (10 JSON/JSONB types, 34 nullability, 7
indexes; MIRI-9), and ``alembic upgrade head`` against it would have failed at
migration 001 on "table already exists".

These tests prove the three things that matter before any of this touches a
database with money in it:

* migration 003's guard refuses, naming the column, when a column it wants to
  tighten holds NULL — it never invents a value;
* ``alembic upgrade head`` ADOPTS a create_all-built database (no duplicate-table
  error) and preserves every row, on Postgres, the deployment dialect;
* the four request-path ``create_all`` calls are refused in production and kept
  in dev/test.

They skip (never silently pass) when Postgres is unreachable, the convention
``tests/test_payment_refs_postgres.py`` uses.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = _ROOT / "alembic" / "versions" / "003_reconcile_models_not_null.py"


def _load_003():
    spec = importlib.util.spec_from_file_location("migration_003", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


# --- the guard, without a database -----------------------------------------


class _Result:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value

    def scalar_one(self):
        return self._value


class _FakeBind:
    """Answers the two queries the guard makes: column existence and null count."""

    def __init__(self, *, nulls=None, not_null=(), missing=()):
        self.nulls = nulls or {}
        self.not_null = set(not_null)
        self.missing = set(missing)

    def execute(self, statement, params=None):
        sql = str(statement)
        if "information_schema.columns" in sql:
            key = f"{params['t']}.{params['c']}"
            if key in self.missing:
                return _Result(None)
            return _Result(("NO",) if key in self.not_null else ("YES",))
        match = re.search(r'FROM "([^"]+)" WHERE "([^"]+)" IS NULL', sql)
        assert match, sql
        return _Result(self.nulls.get(f"{match.group(1)}.{match.group(2)}", 0))


def test_003_covers_exactly_the_columns_models_declare_not_null():
    from miriam_agent.database.models import Base

    pairs = _load_003()._NOT_NULL_COLUMNS
    assert len(pairs) == 34, "the drift set is 34 nullability items (MIRI-9)"
    assert len(set(pairs)) == 34, "no duplicate (table, column) entries"
    for table, column in pairs:
        meta = Base.metadata.tables[table]
        assert column in meta.columns, f"{table}.{column} is not a models column"
        assert meta.columns[column].nullable is False, (
            f"{table}.{column} is in migration 003 but the model no longer "
            "declares it NOT NULL — 003 and models.py have drifted apart"
        )


def test_guard_refuses_and_names_the_column_when_nulls_exist():
    with pytest.raises(RuntimeError) as excinfo:
        _load_003()._assert_no_nulls(_FakeBind(nulls={"users.created_at": 3}))
    message = str(excinfo.value)
    assert "users.created_at" in message
    assert "3" in message
    assert "refusing" in message


def test_guard_is_silent_when_every_column_is_clean():
    _load_003()._assert_no_nulls(_FakeBind())


def test_guard_skips_a_column_that_is_already_not_null():
    _load_003()._assert_no_nulls(
        _FakeBind(nulls={"users.created_at": 99}, not_null=["users.created_at"])
    )


def test_missing_column_aborts_with_provenance_guidance():
    with pytest.raises(RuntimeError, match="schema_provenance"):
        _load_003()._assert_no_nulls(_FakeBind(missing=["users.created_at"]))


# --- the create_all gate, without a database -------------------------------


def test_auto_create_is_allowed_outside_production_and_refused_in_production(
    monkeypatch,
):
    from miriam_agent.config.settings import Settings

    dev = Settings(ENVIRONMENT="development", DB_AUTO_CREATE_TABLES=None)
    assert dev.auto_create_tables is True

    def _prod(**overrides):
        return Settings(
            ENVIRONMENT="production",
            JWT_SECRET="j" * 40,
            SECRET_KEY="s" * 40,
            ENCRYPTION_KEY="e" * 40,
            DATABASE_URL="postgresql+asyncpg://miriam:realpw@db.internal:5432/miriam",
            GO_BACKEND_URL="https://api.userail.money",
            RAIL_SERVICE_KEY="r" * 40,
            OPENAI_API_KEY="sk-test-not-a-real-key",
            **overrides,
        )

    assert _prod(DB_AUTO_CREATE_TABLES=None).auto_create_tables is False

    # An explicit setting overrides the environment either way.
    assert _prod(DB_AUTO_CREATE_TABLES=True).auto_create_tables is True
    assert (
        Settings(ENVIRONMENT="dev", DB_AUTO_CREATE_TABLES=False).auto_create_tables
        is False
    )


# --- end to end, on Postgres (the deployment dialect) -----------------------

_DEFAULT_URL = "postgresql://miriam:miriam_password@localhost:5432/miriam_test"
_BASE_URL = os.environ.get("ALEMBIC_TEST_DATABASE_URL", _DEFAULT_URL)


def _alembic(database: str, *args: str) -> subprocess.CompletedProcess:
    url = _BASE_URL.rsplit("/", 1)[0] + "/" + database
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )


def _db_url(database: str) -> str:
    return _BASE_URL.rsplit("/", 1)[0] + "/" + database


def _connect(database: str = "postgres"):
    psycopg2 = pytest.importorskip("psycopg2")

    parts = urlparse(_BASE_URL)
    return psycopg2.connect(
        host=parts.hostname,
        port=parts.port or 5432,
        user=parts.username,
        password=parts.password,
        dbname=database,
    )


def _require_postgres():
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        return _connect("postgres")
    except psycopg2.OperationalError as exc:  # pragma: no cover - environment
        pytest.skip(f"Postgres is not reachable at {_BASE_URL}: {exc!r}")


class _TempDB:
    """A throwaway database that is dropped even when the test fails."""

    def __init__(self):
        admin = _require_postgres()
        self.name = "miriam_alembic_check_" + uuid.uuid4().hex[:12]
        admin.autocommit = True
        try:
            with admin.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{self.name}"')
        finally:
            admin.close()

    def __enter__(self):
        return self.name

    def __exit__(self, *_exc):
        admin = _connect("postgres")
        admin.autocommit = True
        try:
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{self.name}"')
        finally:
            admin.close()
        return False


def _count(database: str, table: str) -> int:
    conn = _connect(database)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM "{table}"')
            return cur.fetchone()[0]
    finally:
        conn.close()


def test_upgrade_head_builds_an_empty_database_and_matches_the_models():
    with _TempDB() as db:
        upgrade = _alembic(db, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr
        assert "004" in (upgrade.stdout + upgrade.stderr)

        check = _alembic(db, "check")
        assert check.returncode == 0, check.stdout + check.stderr
        assert "No new upgrade operations detected" in (check.stdout + check.stderr)

        # 13 application tables + alembic_version.
        conn = _connect(db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE'"
                )
                assert cur.fetchone()[0] == 14
                cur.execute(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='memory_entries' AND column_name='embedding'"
                )
                assert cur.fetchone()[0] == 0, "embedding must not be recreated"
        finally:
            conn.close()


def test_upgrade_head_adopts_a_create_all_database_and_preserves_rows():
    """The deployed shape: create_all built it, no alembic_version, missing a table.

    Reproduced faithfully — ``Base.metadata.create_all`` builds the tables (as
    the production request path did), then ``payment_references`` is dropped to
    match the observed deployed gap. ``upgrade head`` must adopt it without a
    duplicate-table error and keep every row.
    """
    import sqlalchemy as sa

    from miriam_agent.database.models import Base

    with _TempDB() as db:
        engine = sa.create_engine(_db_url(db))
        try:
            Base.metadata.create_all(engine)  # the create_all path
            with engine.begin() as conn:
                # Reproduce the deployed shape exactly: the vestigial embedding
                # column, no ix_ indexes, and the one table create_all never
                # reached.
                conn.execute(
                    sa.text("ALTER TABLE memory_entries ADD COLUMN embedding json")
                )
                for idx in (
                    "ix_conversations_user_id",
                    "ix_messages_conversation_id",
                    "ix_memory_entries_user_id",
                    "ix_memory_entries_type",
                    "ix_audit_logs_user_id",
                    "ix_audit_logs_created_at",
                    "ix_tool_usage_user_id",
                ):
                    conn.execute(sa.text(f"DROP INDEX {idx}"))
                conn.execute(sa.text("DROP TABLE payment_references"))
                # Realistic rows in the tables the reconciliation touches.
                conn.execute(
                    sa.text(
                        "INSERT INTO users (id, username, email, full_name, "
                        "created_at, is_active, preferences) VALUES "
                        "('u1','shop1','shop1@example.com','Adaeze Provisions',"
                        "now(), true, '{}')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO conversations (id, user_id, title, created_at, "
                        "updated_at) VALUES ('conv1','u1','Stock', now(), now())"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO messages (id, conversation_id, role, content, "
                        "metadata, created_at) VALUES ('m1','conv1','user','how many?',"
                        "'{}',now())"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO memory_entries (id, user_id, type, content, "
                        "metadata, embedding, created_at, updated_at) VALUES "
                        "('mem1','u1','financial','sells rice','{}',NULL,now(),now())"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO audit_logs (id, user_id, action, resource, "
                        "details, created_at) VALUES "
                        "('a1','u1','view','ledger','{}',now())"
                    )
                )
        finally:
            engine.dispose()

        # Which of the 34 nullability items are actually violated in the
        # deployed (create_all) shape? create_all builds Mapped[...]-non-Optional
        # columns NOT NULL, so every count is zero. The shipped pre-flight
        # (scripts/not_null_preflight.sql) is psql-native; this is its Python
        # equivalent, skipping the table create_all never reached.
        migration = _load_003()
        conn = _connect(db)
        try:
            offenders = []
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema='public'"
                )
                present = {(t, c) for t, c in cur.fetchall()}
                for table, column in migration._NOT_NULL_COLUMNS:
                    if (table, column) not in present:
                        continue
                    cur.execute(
                        f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NULL'
                    )
                    count = cur.fetchone()[0]
                    if count:
                        offenders.append((f"{table}.{column}", count))
            assert offenders == [], f"NULLs in the create_all shape: {offenders}"
        finally:
            conn.close()

        before = {
            t: _count(db, t)
            for t in (
                "users",
                "conversations",
                "messages",
                "memory_entries",
                "audit_logs",
            )
        }
        assert before == {
            "users": 1,
            "conversations": 1,
            "messages": 1,
            "memory_entries": 1,
            "audit_logs": 1,
        }

        upgrade = _alembic(db, "upgrade", "head")
        assert upgrade.returncode == 0, (
            "upgrade head failed to adopt the create_all database:\n"
            + upgrade.stdout
            + upgrade.stderr
        )

        # Every row survived, and the table that was missing now exists.
        after = {t: _count(db, t) for t in before}
        assert after == before
        assert _count(db, "payment_references") == 0  # created empty

        # The reconciled shape matches the models on every axis.
        check = _alembic(db, "check")
        assert check.returncode == 0, (
            "schema still drifts from models.py after adopting:\n"
            + check.stdout
            + check.stderr
        )

        conn = _connect(db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='memory_entries' AND column_name='embedding'"
                )
                assert cur.fetchone()[0] == 0, "004 must drop embedding"
                cur.execute(
                    "SELECT count(*) FROM pg_indexes WHERE tablename='memory_entries' "
                    "AND indexname = 'ix_memory_entries_user_id'"
                )
                assert cur.fetchone()[0] == 1, "001 must create the declared index"
                cur.execute("SELECT version_num FROM alembic_version")
                assert cur.fetchone()[0] == "004"
        finally:
            conn.close()

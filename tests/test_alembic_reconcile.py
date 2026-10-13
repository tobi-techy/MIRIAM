"""The schema reconciliation is proved on real, non-empty databases.

The deployed database was built by ``Base.metadata.create_all`` on a request
path, not by Alembic: it holds the model tables with no ``alembic_version`` and
is missing ``payment_references``. ``alembic check`` compared that reality to the
migrations and found 51 items of drift (10 JSON/JSONB types, 34 nullability, 7
indexes; MIRI-9), and ``alembic upgrade head`` against it would have failed at
migration 001 on "table already exists".

These tests prove the three things that matter before any of this touches a
database with money in it:

* migration 003's guard aggregates and names every offending column, and never
  counts a column it has already decided to skip;
* ``alembic upgrade head`` ADOPTS a create_all-built database (no duplicate-table
  error) and preserves every row, on Postgres, the deployment dialect;
* a column that holds NULL where the model demands NOT NULL fails the migration
  **loudly**, leaving the version and the row untouched — never inventing a value.

The four request-path ``create_all`` calls are also covered (refused in
production, kept in dev/test).

Postgres tests skip (never silently pass) when it is unreachable, the convention
``tests/test_payment_refs_postgres.py`` uses.
"""

from __future__ import annotations

import importlib.util
import os
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


# --- the migration's shape and guard, without a database --------------------


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


def test_guard_refuses_and_names_every_offending_column(monkeypatch):
    migration = _load_003()
    monkeypatch.setattr(migration, "_is_nullable", lambda bind, t, c: True)
    monkeypatch.setattr(
        migration,
        "_count_nulls",
        lambda bind, t, c: 3 if (t, c) == ("users", "created_at") else 0,
    )
    with pytest.raises(RuntimeError) as excinfo:
        migration._assert_no_nulls(object())
    message = str(excinfo.value)
    assert "users.created_at" in message
    assert "3" in message
    assert "refusing" in message


def test_guard_is_silent_when_every_column_is_clean(monkeypatch):
    migration = _load_003()
    monkeypatch.setattr(migration, "_is_nullable", lambda bind, t, c: True)
    monkeypatch.setattr(migration, "_count_nulls", lambda bind, t, c: 0)
    migration._assert_no_nulls(object())


def test_guard_never_counts_a_column_that_is_already_not_null(monkeypatch):
    migration = _load_003()
    monkeypatch.setattr(migration, "_is_nullable", lambda bind, t, c: False)

    def _must_not_run(*_args):
        raise AssertionError("counted a column that is already NOT NULL")

    monkeypatch.setattr(migration, "_count_nulls", _must_not_run)
    migration._assert_no_nulls(object())  # a half-applied database converges


def test_a_column_the_database_does_not_have_aborts_with_provenance_guidance(
    monkeypatch,
):
    migration = _load_003()

    class _EmptyInspector:
        def get_columns(self, _table):
            return []

    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: _EmptyInspector())
    with pytest.raises(RuntimeError, match="schema_provenance"):
        migration._is_nullable(object(), "users", "created_at")


# --- the create_all gate, without a database --------------------------------


def test_auto_create_is_allowed_outside_production_and_refused_in_production():
    from miriam_agent.config.settings import Settings

    assert (
        Settings(
            ENVIRONMENT="development", DB_AUTO_CREATE_TABLES=None
        ).auto_create_tables
        is True
    )

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


def _build_deployed_shape(engine) -> None:
    """Reproduce the deployed create_all shape: vestigial embedding column, no
    ix_ indexes, and the one model table create_all never reached."""
    import sqlalchemy as sa

    from miriam_agent.database.models import Base

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE memory_entries ADD COLUMN embedding json"))
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


def test_upgrade_head_builds_an_empty_database_and_matches_the_models():
    with _TempDB() as db:
        upgrade = _alembic(db, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr
        assert "004" in (upgrade.stdout + upgrade.stderr)

        check = _alembic(db, "check")
        assert check.returncode == 0, check.stdout + check.stderr
        assert "No new upgrade operations detected" in (check.stdout + check.stderr)

        conn = _connect(db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE'"
                )
                assert cur.fetchone()[0] == 14  # 13 application + alembic_version
                cur.execute(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='memory_entries' AND column_name='embedding'"
                )
                assert cur.fetchone()[0] == 0, "embedding must not be recreated"
        finally:
            conn.close()


def test_upgrade_head_adopts_a_create_all_database_and_preserves_rows():
    """The deployed shape: create_all built it, no alembic_version, missing a table.

    ``upgrade head`` must adopt it without a duplicate-table error and keep every
    row, and the 34 nullability items must all be clean in that shape.
    """
    import sqlalchemy as sa

    with _TempDB() as db:
        engine = sa.create_engine(_db_url(db))
        try:
            _build_deployed_shape(engine)
            with engine.begin() as conn:
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

        rows_before = {
            t: _count(db, t)
            for t in (
                "users",
                "conversations",
                "messages",
                "memory_entries",
                "audit_logs",
            )
        }
        assert rows_before == {
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

        assert {t: _count(db, t) for t in rows_before} == rows_before
        assert _count(db, "payment_references") == 0  # created empty

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


def test_upgrade_head_refuses_a_column_that_holds_null_and_invents_nothing():
    """The guarded case: a NULL where the model demands NOT NULL fails loudly.

    A hand-built or older-001 database can hold a NULL in a column the models
    declare NOT NULL. 003 must refuse — naming the column — leaving the version
    and the row untouched, rather than inventing a value to satisfy the
    constraint.
    """
    import sqlalchemy as sa

    with _TempDB() as db:
        engine = sa.create_engine(_db_url(db))
        try:
            _build_deployed_shape(engine)
            with engine.begin() as conn:
                conn.execute(
                    sa.text("ALTER TABLE users ALTER COLUMN created_at DROP NOT NULL")
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO users (id, username, email, full_name, "
                        "created_at, is_active, preferences) VALUES "
                        "('u0','nullshop','n@example.com','Null Shop',NULL,true,'{}')"
                    )
                )
        finally:
            engine.dispose()

        result = _alembic(db, "upgrade", "head")
        assert result.returncode != 0, "003 must fail on a NULL it would tighten"
        output = result.stdout + result.stderr
        assert "users.created_at" in output
        assert "refusing" in output

        conn = _connect(db)
        try:
            with conn.cursor() as cur:
                # The run is atomic: a failure at 003 rolls the whole upgrade
                # back, so nothing partial is left behind — not the version, not
                # the table 001 would have created.
                cur.execute("SELECT to_regclass('public.alembic_version')")
                if cur.fetchone()[0] is not None:
                    cur.execute("SELECT version_num FROM alembic_version")
                    assert cur.fetchone()[0] not in ("003", "004")
                cur.execute("SELECT to_regclass('public.payment_references')")
                assert cur.fetchone()[0] is None, "001's work must not have persisted"
                cur.execute(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name='users' AND column_name='created_at'"
                )
                assert cur.fetchone()[0] == "YES", "no DDL applied"
                cur.execute("SELECT count(*) FROM users WHERE created_at IS NULL")
                assert cur.fetchone()[0] == 1, "the row must survive"
        finally:
            conn.close()

-- schema_provenance.sql — which path built this database?
--
-- READ-ONLY. Ordinary catalog SELECTs and one count; no writes, no DDL, no
-- table scan, no EXPLAIN ANALYZE, no lock beyond what a SELECT takes. Safe to
-- run against production:
--
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/schema_provenance.sql
--
-- It answers the question `alembic_version` alone cannot: the live database may
-- not have been built by Alembic at all. `scripts/init-db.sql` (until recently
-- mounted into the Postgres container's init directory) was a hand-written
-- mirror of the models, and it declared `memory_entries.embedding` as
-- `VECTOR(1536)` with an HNSW index that no migration ever created — while
-- migration 001 leaves the same column as `JSONB`. Two builds, two shapes, and
-- a stamped `alembic_version` proves nothing about which one you have.

\echo '== 1. Did Alembic build this schema? =='
SELECT to_regclass('public.alembic_version') IS NOT NULL AS alembic_version_exists \gset
\if :alembic_version_exists
  \echo 'alembic_version table present; its contents:'
  SELECT version_num AS alembic_version FROM alembic_version;
\else
  \echo 'NO alembic_version table.'
\endif

\echo ''
\echo '== 2. What type is memory_entries.embedding? =='
SELECT format_type(a.atttypid, a.atttypmod) AS embedding_type
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname = 'memory_entries'
  AND a.attname = 'embedding'
  AND a.attnum > 0
  AND NOT a.attisdropped;

\echo ''
\echo '== 3. Is there an HNSW index (pgvector) anywhere on memory_entries? =='
SELECT ci.relname AS index_name,
       am.amname  AS method,
       pg_get_indexdef(ci.oid) AS definition
FROM pg_index x
JOIN pg_class ct ON ct.oid = x.indrelid
JOIN pg_class ci ON ci.oid = x.indexrelid
JOIN pg_am am ON am.oid = ci.relam
WHERE ct.relname = 'memory_entries'
  AND am.amname = 'hnsw';
-- Zero rows = no HNSW index (the migrated shape). One row = the init-db.sql
-- shape, which the migrations never produced.

\echo ''
\echo '== 3b. Every index on memory_entries (context for the above) =='
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public' AND tablename = 'memory_entries'
ORDER BY indexname;

\echo ''
\echo '== 4. Is pgvector installed, and what version? =='
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
-- Zero rows = the extension is absent, so a `vector` column cannot exist and
-- the schema is necessarily the migrated JSONB shape.

\echo ''
\echo '== 5. How many base tables, and which? =='
SELECT count(*) AS base_table_count
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE';

SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name;
-- Migrations 001+002 produce 14 public tables (13 application + alembic_version).

-- ---------------------------------------------------------------------------
-- What each combination means
-- ---------------------------------------------------------------------------
--
-- A. alembic_version EXISTS and holds '002' (or '001'), AND
--    memory_entries.embedding IS jsonb, AND
--    no HNSW index on memory_entries.
--    -> The database was built by Alembic. Alembic is authoritative. Migration
--       003 applies as written; the nullability pre-flights are all you need.
--
-- B. alembic_version EXISTS and holds '002' (or '001'), BUT
--    memory_entries.embedding IS vector, OR an HNSW index exists.
--    -> Alembic was STAMPED onto a hand-built (init-db.sql) schema, or the two
--       were mixed. The version rows are not proof the DDL matches. Do NOT
--       apply 003 blind: diff the live schema against models.py first
--       (`alembic check`), because the base shape differs from what the
--       migrations believe they created.
--
-- C. alembic_version does NOT exist, and the table count is 13 application
--    tables (or ~13), embedding vector/jsonb per init-db.sql.
--    -> The database was built by init-db.sql (or `create_all`). Alembic has
--       never run. `alembic upgrade head` would try to CREATE the tables again
--       and fail. The base schema must be reconciled to migration 001 by hand
--       (or on a copy) before any migration is applied. Never `alembic stamp`
--       to skip this: it asserts a shape nobody verified.
--
-- D. alembic_version does NOT exist and the table count is 0 (only the
--    `vector` extension).
--    -> An empty, un-migrated database (the retired extension-only
--       init-db.sql). Run `alembic upgrade head`; there is no data to protect.
--
-- E. table count of 14 but embedding type / indexes ambiguous, or unexpected
--    tables present.
--    -> Mixed or unknown provenance. Treat as case B: verify with
--       `alembic check` and a schema dump before applying anything.

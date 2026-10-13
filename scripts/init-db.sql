-- Bootstrap that runs once when the container first initialises a cluster
-- (mounted at /docker-entrypoint-initdb.d/). It is deliberately minimal: the
-- extension must exist before Alembic's first migration, which itself runs
-- `CREATE EXTENSION IF NOT EXISTS vector`, so this file is a belt for the
-- create_all path and nothing more.
--
-- Alembic is the ONLY path that creates application tables. The former
-- hand-written mirror of miriam_agent/database/models.py lived here and had
-- already drifted (generic JSONB vs the model's JSON, a missing
-- channel_identities table), which made it a second, disagreeing source of
-- truth. Do not reintroduce table DDL here; add a migration instead
-- (alembic revision --autogenerate -m "...").

CREATE EXTENSION IF NOT EXISTS vector;

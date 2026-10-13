# Semantic memory — the path back

The Python agent has no semantic memory in the pilot. `memory_entries` has no
`embedding` column (dropped in migration `004_drop_memory_embedding.py`) and the
`vector` extension is not enabled in the Python agent's database.

## Why

The column existed but nothing wrote it: in the deployed database every one of
the 299 memory rows held NULL. The real decision was never the column type — it
is whether the Python agent computes embeddings at all, because that carries a
per-entry API cost forever. The pilot's success measures (shops active five days
a week, sell-through within 15% of the distributor's records, fake alerts
caught, no double-counted payments) are deterministic-data measures that
semantic recall does nothing for. Recall today is token-overlap + recency
(`miriam_agent/database/memory.py::search_memories`), which needs no vectors.

## If you turn it on later

This is the whole reversal. Do it as a migration, not a request-path change.

1. Enable the extension in the Python agent's database:
   `CREATE EXTENSION IF NOT EXISTS vector;`
2. Add the column back at the model's dimension, e.g. in a new Alembic revision:
   `op.add_column("memory_entries", sa.Column("embedding", Vector(1536), nullable=True))`
   plus an HNSW index:
   `CREATE INDEX ix_memory_entries_embedding ON memory_entries USING hnsw (embedding vector_cosine_ops);`
3. Wire an embedding provider and write the vector on memory create/update.
4. Backfill: embed existing `memory_entries.content` rows and write the vectors.
5. Flip `search_memories` to prefer the vector path (it is already written
   "vector-ready").

## The alternative already on the server

The Go platform's database, on the same server, has a per-user facts table with
a `vector` column (present, zero rows). Per-user semantic memory could live
there instead, alongside the knowledge-corpus embeddings the Go side already
computes — keeping Python free of embeddings entirely. This is recorded, not
decided; the pilot does not need either.

## Scope note

Migration `001_baseline.py` deliberately does not `CREATE EXTENSION vector`, and
`miriam_agent/database/models.py` deliberately has no `embedding` column. Both
say so in a comment pointing here.

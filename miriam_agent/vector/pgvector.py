"""pgvector implementation for Miriam Financial Agent.

Stores vector embeddings in PostgreSQL with the pgvector extension and
performs cosine-similarity search. Uses SQLAlchemy ``text()`` with named
bound parameters (asyncpg-compatible) rather than positional ``$N`` marks
splatted into ``execute()``.

Embeddings are serialized to pgvector's bracket form ``[0.1,0.2,...]``.
"""

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from miriam_agent.vector.base import VectorStore

logger = logging.getLogger(__name__)

TABLE = "memory_entries_vector"


def _vec(embedding: list[float]) -> str:
    """Serialize a float list to pgvector's text form."""
    return "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"


class PgVectorStore(VectorStore):
    """PostgreSQL + pgvector store for embeddings and similarity search."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = None
        self.async_session = None

    async def initialize(self) -> None:
        """Create engine, ensure the pgvector extension and table exist."""
        self.engine = create_async_engine(self.database_url)
        async with self.engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {TABLE} (
                        id UUID PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        content TEXT NOT NULL,
                        metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        embedding VECTOR(1536),
                        content_type VARCHAR(50) NOT NULL DEFAULT 'text',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """))
            await conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_user ON {TABLE} (user_id)"
                )
            )
            await conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_type "
                    f"ON {TABLE} (content_type)"
                )
            )
            await conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_embedding "
                    f"ON {TABLE} USING ivfflat (embedding vector_cosine_ops) "
                    "WITH (lists = 100)"
                )
            )
        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def close(self) -> None:
        if self.engine:
            await self.engine.dispose()

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ------------------------------------------------------------------
    # VectorStore interface (+ user_id scoping)
    # ------------------------------------------------------------------

    async def store_embedding(
        self,
        id: str,
        content: str,
        embedding: list[float],
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        content_type: str = "text",
    ) -> bool:
        """Store (or upsert) a vector embedding."""

        async with self.async_session() as session:
            values = {
                "id": id,
                "user_id": user_id or "unknown",
                "content": content,
                "metadata": json_dumps(metadata or {}),
                "embedding": _vec(embedding),
                "content_type": content_type,
            }
            try:
                # Build upsert manually with text() since pgvector type is custom.
                existing = await session.execute(
                    text(f"SELECT 1 FROM {TABLE} WHERE id = :id"),
                    {"id": id},
                )
                if existing.first():
                    await session.execute(
                        text(f"""
                            UPDATE {TABLE} SET
                                content = :content,
                                metadata = :metadata::jsonb,
                                embedding = :embedding::vector,
                                content_type = :content_type,
                                user_id = :user_id,
                                updated_at = NOW()
                            WHERE id = :id
                            """),
                        values,
                    )
                else:
                    await session.execute(
                        text(f"""
                            INSERT INTO {TABLE}
                                (id, user_id, content, metadata, embedding,
                                 content_type)
                            VALUES
                                (:id, :user_id, :content, :metadata::jsonb,
                                 :embedding::vector, :content_type)
                            """),
                        values,
                    )
                await session.commit()
                return True
            except Exception as e:
                await session.rollback()
                logger.error("store_embedding failed: %s", e)
                return False

    async def search_similar(
        self,
        query_embedding: list[float],
        limit: int = 10,
        threshold: float = 0.3,
        filters: dict[str, Any] | None = None,
        user_id: str | None = None,
        content_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Cosine-similarity search. Returns rows with a ``similarity`` field."""
        filters = filters or {}
        where = []
        params: dict[str, Any] = {"emb": _vec(query_embedding), "lim": limit}

        if user_id:
            where.append("user_id = :user_id")
            params["user_id"] = user_id
        if content_type:
            where.append("content_type = :content_type")
            params["content_type"] = content_type
        for i, (key, value) in enumerate(filters.items()):
            where.append(f"metadata->>:fkey{i} = :fval{i}")
            params[f"fkey{i}"] = key
            params[f"fval{i}"] = str(value)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        sql = text(f"""
            SELECT id, user_id, content, metadata, content_type,
                   1 - (embedding <=> :emb::vector) AS similarity
            FROM {TABLE}
            {where_sql}
            AND 1 - (embedding <=> :emb::vector) >= :thresh
            ORDER BY similarity DESC
            LIMIT :lim
            """)
        # sqlalchemy text() bound params can't be re-used twice; inline threshold.
        rendered = str(sql)
        rendered = rendered.replace(":thresh", str(threshold))
        async with self.async_session() as session:
            try:
                result = await session.execute(text(rendered), params)
                rows = []
                for row in result.mappings():
                    rows.append(
                        {
                            "id": str(row["id"]),
                            "user_id": row["user_id"],
                            "content": row["content"],
                            "metadata": json_loads(row["metadata"]),
                            "content_type": row["content_type"],
                            "similarity": float(row["similarity"] or 0.0),
                        }
                    )
                return rows
            except Exception as e:
                logger.error("search_similar failed: %s", e)
                return []

    async def delete_embedding(self, id: str, user_id: str | None = None) -> bool:
        params = {"id": id}
        where = "id = :id"
        if user_id:
            where += " AND user_id = :user_id"
            params["user_id"] = user_id
        async with self.async_session() as session:
            try:
                await session.execute(
                    text(f"DELETE FROM {TABLE} WHERE {where}"), params
                )
                await session.commit()
                return True
            except Exception as e:
                await session.rollback()
                logger.error("delete_embedding failed: %s", e)
                return False

    async def update_embedding(
        self,
        id: str,
        embedding: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        async with self.async_session() as session:
            try:
                await session.execute(
                    text(f"""
                        UPDATE {TABLE} SET
                            embedding = :embedding::vector,
                            metadata = :metadata::jsonb,
                            updated_at = NOW()
                        WHERE id = :id
                        """),
                    {
                        "id": id,
                        "embedding": _vec(embedding),
                        "metadata": json_dumps(metadata or {}),
                    },
                )
                await session.commit()
                return True
            except Exception as e:
                await session.rollback()
                logger.error("update_embedding failed: %s", e)
                return False


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)


def json_loads(raw: Any) -> dict[str, Any]:
    import json

    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": str(raw)}

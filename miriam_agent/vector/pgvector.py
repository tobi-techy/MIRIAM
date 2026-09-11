import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from miriam_agent.database.models import Base, MemoryEntry
from miriam_agent.vector import VectorStore

logger = logging.getLogger(__name__)

class PgVectorStore(VectorStore):
    """pgVector implementation for storing and searching vector embeddings."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = None
        self.async_session = None
        self.table_name = "memory_entries_vector"

    async def initialize(self):
        """Initialize the vector database connection."""
        # Create async engine
        self.engine = create_async_engine(self.database_url)

        # Create tables
        async with self.engine.begin() as conn:
            await conn.run_sync(self._create_tables)

        # Create async session factory
        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    def _create_tables(self, conn):
        """Create vector store tables."""
        # Create main table for vector data
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_entries_vector (
                id UUID PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                content TEXT NOT NULL,
                metadata JSONB,
                embedding VECTOR(1536),  -- OpenAI embedding dimension
                content_type VARCHAR(50) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_memory_entries_vector_user_id (user_id),
                INDEX idx_memory_entries_vector_content_type (content_type),
                INDEX idx_memory_entries_vector_user_content_type (user_id, content_type)
            )
            """
        )

        # Create vector index for cosine similarity
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_entries_vector_embedding 
            ON memory_entries_vector 
            USING ivfflat (embedding vector_cosine_ops) 
            WITH (lists = 100)
            """
        )

    async def close(self):
        """Close vector database connections."""
        if self.engine:
            await self.engine.dispose()

    async def store_embedding(
        self,
        user_id: str,
        content: str,
        metadata: Dict[str, Any],
        embedding: List[float],
        content_type: str = "text",
        memory_id: Optional[str] = None,
    ) -> str:
        """Store an embedding in the vector database."""
        try:
            async with self.async_session() as session:
                # Generate ID if not provided
                from uuid import uuid4
                embedding_id = memory_id or str(uuid4())

                # Store embedding
                query = f"""
                INSERT INTO {self.table_name} 
                (id, user_id, content, metadata, embedding, content_type, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (id) DO UPDATE SET
                content = EXCLUDED.content,
                metadata = EXCLUDED.metadata,
                embedding = EXCLUDED.embedding,
                content_type = EXCLUDED.content_type,
                updated_at = CURRENT_TIMESTAMP
                """

                await session.execute(
                    query,
                    (
                        embedding_id,
                        user_id,
                        content,
                        json.dumps(metadata),
                        embedding,
                        content_type,
                        datetime.utcnow(),
                    ),
                )

                await session.commit()

                logger.info(
                    "Embedding stored",
                    embedding_id=embedding_id,
                    user_id=user_id,
                    content_type=content_type,
                )

                return embedding_id

        except Exception as e:
            logger.error(
                "Error storing embedding",
                user_id=user_id,
                error=str(e),
                exc_info=True,
            )
            raise

    async def search_similar(
        self,
        user_id: str,
        query_embedding: List[float],
        content_type: Optional[str] = None,
        limit: int = 10,
        similarity_threshold: float = 0.7,
    ) -> List[Dict[str, Any]]:
        """Search for similar embeddings."""
        try:
            async with self.async_session() as session:
                # Build query
                query = f"""
                SELECT id, content, metadata, embedding,
                       1 - (embedding <=> $1::vector) as similarity
                FROM {self.table_name}
                WHERE user_id = $2
                """

                params = [query_embedding, user_id]

                # Add content type filter if specified
                if content_type:
                    query += " AND content_type = $3"
                    params.append(content_type)

                # Add similarity threshold
                query += " AND (1 - (embedding <=> $1::vector)) >= $3"

                # Adjust parameters for threshold
                query = query.replace(
                    "(1 - (embedding <=> $1::vector)) >= $3",
                    f"(1 - (embedding <=> $1::vector)) >= {similarity_threshold}",
                )

                query += " ORDER BY similarity DESC LIMIT $4"
                params.append(limit)

                # Execute query
                result = await session.execute(query, *params)

                # Format results
                results = []
                for row in result:
                    results.append(
                        {
                            "id": row.id,
                            "content": row.content,
                            "metadata": json.loads(row.metadata) if row.metadata else {},
                            "similarity": float(row.similarity),
                            "content_type": row.content_type,
                        }
                    )

                return results

        except Exception as e:
            logger.error(
                "Error searching for similar embeddings",
                user_id=user_id,
                error=str(e),
                exc_info=True,
            )
            raise

    async def delete_embedding(self, embedding_id: str, user_id: str) -> bool:
        """Delete an embedding."""
        try:
            async with self.async_session() as session:
                query = f"""
                DELETE FROM {self.table_name}
                WHERE id = $1 AND user_id = $2
                """

                result = await session.execute(query, (embedding_id, user_id))
                await session.commit()

                if result.rowcount > 0:
                    logger.info(
                        "Embedding deleted",
                        embedding_id=embedding_id,
                        user_id=user_id,
                    )
                    return True

                return False

        except Exception as e:
            logger.error(
                "Error deleting embedding",
                embedding_id=embedding_id,
                user_id=user_id,
                error=str(e),
                exc_info=True,
            )
            raise

    async def get_user_embeddings(
        self, user_id: str, content_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get all embeddings for a user."""
        try:
            async with self.async_session() as session:
                # Build query
                query = f"""
                SELECT id, content, metadata, embedding, content_type, created_at
                FROM {self.table_name}
                WHERE user_id = $1
                """

                params = [user_id]

                if content_type:
                    query += " AND content_type = $2"
                    params.append(content_type)

                query += " ORDER BY created_at DESC"

                # Execute query
                result = await session.execute(query, *params)

                # Format results
                results = []
                for row in result:
                    results.append(
                        {
                            "id": row.id,
                            "content": row.content,
                            "metadata": json.loads(row.metadata) if row.metadata else {},
                            "embedding": row.embedding,
                            "content_type": row.content_type,
                            "created_at": row.created_at.isoformat(),
                        }
                    )

                return results

        except Exception as e:
            logger.error(
                "Error getting user embeddings",
                user_id=user_id,
                error=str(e),
                exc_info=True,
            )
            raise

    async def update_embedding(
        self,
        embedding_id: str,
        user_id: str,
        content: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        embedding: Optional[List[float]] = None,
    ) -> bool:
        """Update an embedding."""
        try:
            async with self.async_session() as session:
                # Build query
                set_clauses = []
                params = [embedding_id, user_id]

                if content is not None:
                    set_clauses.append("content = $3")
                    params.append(content)

                if metadata is not None:
                    set_clauses.append("metadata = $4")
                    params.append(json.dumps(metadata))

                if embedding is not None:
                    set_clauses.append("embedding = $5")
                    params.append(embedding)

                if not set_clauses:
                    return False

                query = f"""
                UPDATE {self.table_name}
                SET {', '.join(set_clauses)}, updated_at = CURRENT_TIMESTAMP
                WHERE id = $1 AND user_id = $2
                """

                result = await session.execute(query, *params)
                await session.commit()

                if result.rowcount > 0:
                    logger.info(
                        "Embedding updated",
                        embedding_id=embedding_id,
                        user_id=user_id,
                    )
                    return True

                return False

        except Exception as e:
            logger.error(
                "Error updating embedding",
                embedding_id=embedding_id,
                user_id=user_id,
                error=str(e),
                exc_info=True,
            )
            raise

    async def search_by_content(
        self,
        user_id: str,
        query_text: str,
        content_type: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search embeddings by content text."""
        try:
            # For text-based search, we'll use full-text search
            # or simple text matching since we don't have embeddings for the query
            async with self.async_session() as session:
                # Build query using ILIKE for text search
                query = f"""
                SELECT id, content, metadata, embedding, content_type,
                       1 - (content ILIKE $1) as similarity
                FROM {self.table_name}
                WHERE user_id = $2
                """

                # Convert query text to ILIKE pattern
                ilike_pattern = f"%{query_text}%"

                params = [ilike_pattern, user_id]

                if content_type:
                    query += " AND content_type = $3"
                    params.append(content_type)

                query += " ORDER BY similarity DESC LIMIT $4"
                params.append(limit)

                # Execute query
                result = await session.execute(query, *params)

                # Format results
                results = []
                for row in result:
                    # Calculate actual similarity based on content match
                    content_match = 1.0 if query_text.lower() in row.content.lower() else 0.5

                    results.append(
                        {
                            "id": row.id,
                            "content": row.content,
                            "metadata": json.loads(row.metadata) if row.metadata else {},
                            "similarity": content_match,
                            "content_type": row.content_type,
                        }
                    )

                return results

        except Exception as e:
            logger.error(
                "Error searching by content",
                user_id=user_id,
                error=str(e),
                exc_info=True,
            )
            raise

    async def batch_store_embeddings(
        self, embeddings: List[Dict[str, Any]]
    ) -> List[str]:
        """Store multiple embeddings efficiently."""
        try:
            async with self.async_session() as session:
                stored_ids = []

                for embedding_data in embeddings:
                    embedding_id = await self.store_embedding(
                        embedding_data["user_id"],
                        embedding_data["content"],
                        embedding_data["metadata"],
                        embedding_data["embedding"],
                        embedding_data.get("content_type", "text"),
                        embedding_data.get("memory_id"),
                    )

                    stored_ids.append(embedding_id)

                return stored_ids

        except Exception as e:
            logger.error(
                "Error batch storing embeddings",
                error=str(e),
                exc_info=True,
            )
            raise

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def __del__(self):
        """Destructor to clean up resources."""
        if hasattr(self, "engine") and self.engine:
            self.engine.dispose()

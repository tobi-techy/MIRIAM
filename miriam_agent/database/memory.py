import asyncio
import json
from typing import Any, Dict, List, Optional, AsyncGenerator
from datetime import datetime

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from miriam_agent.database.models import Base, MemoryEntry, Conversation, Message

class MemoryStore:
    """Memory store for handling conversations, memories, and user interactions."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = None
        self.async_session = None

    async def initialize(self):
        """Initialize the database connection."""
        # Create async engine
        self.engine = create_async_engine(self.database_url)

        # Create tables
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Create async session factory
        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def close(self):
        """Close database connections."""
        if self.engine:
            await self.engine.dispose()

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def store_interaction(
        self,
        user_id: str,
        role: str,
        content: str,
        conversation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Store an interaction (user message or assistant response)."""
        async with self.async_session() as session:
            try:
                # Create or get conversation
                if conversation_id:
                    conversation = await session.get(Conversation, conversation_id)
                else:
                    # Create new conversation
                    conversation = Conversation(
                        user_id=user_id,
                        title=f"Conversation {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
                    )
                    session.add(conversation)
                    await session.flush()

                # Store message
                message = Message(
                    conversation_id=conversation.id,
                    role=role,
                    content=content,
                    metadata=metadata or {},
                )
                session.add(message)

                # Also store in memory entries for long-term memory
                memory_entry = MemoryEntry(
                    user_id=user_id,
                    type="conversation",
                    content=content,
                    metadata={
                        "role": role,
                        "conversation_id": conversation.id,
                        "timestamp": datetime.utcnow().isoformat(),
                        **(metadata or {}),
                    },
                )
                session.add(memory_entry)

                await session.commit()

                return conversation.id

            except Exception as e:
                await session.rollback()
                raise e

    async def get_conversation_history(
        self, conversation_id: str
    ) -> List[Dict[str, Any]]:
        """Get conversation history."""
        async with self.async_session() as session:
            try:
                # Get conversation
                conversation = await session.get(Conversation, conversation_id)
                if not conversation:
                    return []

                # Get messages
                messages = await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at)
                )

                return [
                    {
                        "id": msg.id,
                        "role": msg.role,
                        "content": msg.content,
                        "metadata": msg.metadata,
                        "created_at": msg.created_at.isoformat(),
                    }
                    for msg in messages.scalars()
                ]

            except Exception as e:
                raise e

    async def get_recent_interactions(
        self, user_id: str, limit: int = 10
    ) -> List[MemoryEntry]:
        """Get recent interactions for a user."""
        async with self.async_session() as session:
            try:
                # Get recent memory entries
                memory_entries = await session.execute(
                    select(MemoryEntry)
                    .where(MemoryEntry.user_id == user_id)
                    .where(MemoryEntry.type == "conversation")
                    .order_by(MemoryEntry.created_at.desc())
                    .limit(limit)
                )

                return list(memory_entries.scalars())

            except Exception as e:
                raise e

    async def store_memory(
        self,
        user_id: str,
        memory_type: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Store a memory entry."""
        async with self.async_session() as session:
            try:
                memory_entry = MemoryEntry(
                    user_id=user_id,
                    type=memory_type,
                    content=content,
                    metadata=metadata or {},
                )
                session.add(memory_entry)
                await session.commit()

                return memory_entry.id

            except Exception as e:
                await session.rollback()
                raise e

    async def retrieve_memory(
        self,
        user_id: str,
        memory_type: Optional[str] = None,
        limit: int = 10,
        filter_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[MemoryEntry]:
        """Retrieve memories for a user."""
        async with self.async_session() as session:
            try:
                # Build query
                query = select(MemoryEntry).where(MemoryEntry.user_id == user_id)

                if memory_type:
                    query = query.where(MemoryEntry.type == memory_type)

                if filter_metadata:
                    for key, value in filter_metadata.items():
                        query = query.where(
                            MemoryEntry.metadata[key].astext == str(value)
                        )

                # Execute query
                memory_entries = await session.execute(
                    query.order_by(MemoryEntry.created_at.desc()).limit(limit)
                )

                return list(memory_entries.scalars())

            except Exception as e:
                raise e

    async def update_memory(
        self, memory_id: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Update a memory entry."""
        async with self.async_session() as session:
            try:
                memory_entry = await session.get(MemoryEntry, memory_id)
                if not memory_entry:
                    return False

                memory_entry.content = content
                if metadata:
                    memory_entry.metadata.update(metadata)

                await session.commit()
                return True

            except Exception as e:
                await session.rollback()
                raise e

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory entry."""
        async with self.async_session() as session:
            try:
                memory_entry = await session.get(MemoryEntry, memory_id)
                if not memory_entry:
                    return False

                await session.delete(memory_entry)
                await session.commit()
                return True

            except Exception as e:
                await session.rollback()
                raise e

    async def search_memories(
        self, user_id: str, query: str, limit: int = 5
    ) -> List[MemoryEntry]:
        """Search for memories based on query text."""
        async with self.async_session() as session:
            try:
                # This is a simple text-based search
                # In a production system, you would use vector search with embeddings
                search_term = f"%{query}%"

                memory_entries = await session.execute(
                    select(MemoryEntry)
                    .where(MemoryEntry.user_id == user_id)
                    .where(
                        or_(
                            MemoryEntry.content.ilike(search_term),
                            MemoryEntry.metadata["content"].astext.ilike(search_term),
                        )
                    )
                    .order_by(MemoryEntry.created_at.desc())
                    .limit(limit)
                )

                return list(memory_entries.scalars())

            except Exception as e:
                raise e

    async def get_user_financial_context(
        self, user_id: str, limit: int = 10
    ) -> List[MemoryEntry]:
        """Get user's financial context from memories."""
        return await self.retrieve_memory(
            user_id=user_id,
            memory_type="financial",
            limit=limit,
        )

    async def get_user_preferences(
        self, user_id: str, limit: int = 10
    ) -> List[MemoryEntry]:
        """Get user's preferences from memories."""
        return await self.retrieve_memory(
            user_id=user_id,
            memory_type="preference",
            limit=limit,
        )

    async def get_user_goals(
        self, user_id: str, limit: int = 10
    ) -> List[MemoryEntry]:
        """Get user's financial goals from memories."""
        return await self.retrieve_memory(
            user_id=user_id,
            memory_type="goal",
            limit=limit,
        )

    async def get_conversations(
        self, user_id: str, limit: int = 10
    ) -> List[Conversation]:
        """Get conversations for a user."""
        async with self.async_session() as session:
            try:
                conversations = await session.execute(
                    select(Conversation)
                    .where(Conversation.user_id == user_id)
                    .order_by(Conversation.updated_at.desc())
                    .limit(limit)
                )

                return list(conversations.scalars())

            except Exception as e:
                raise e

    async def get_conversation_messages(
        self, conversation_id: str
    ) -> List[Message]:
        """Get all messages for a conversation."""
        async with self.async_session() as session:
            try:
                messages = await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at)
                )

                return list(messages.scalars())

            except Exception as e:
                raise e

    async def create_conversation(
        self, user_id: str, title: str
    ) -> Conversation:
        """Create a new conversation."""
        async with self.async_session() as session:
            try:
                conversation = Conversation(
                    user_id=user_id,
                    title=title,
                )
                session.add(conversation)
                await session.commit()

                return conversation

            except Exception as e:
                await session.rollback()
                raise e

    async def update_conversation_title(
        self, conversation_id: str, title: str
    ) -> bool:
        """Update conversation title."""
        async with self.async_session() as session:
            try:
                conversation = await session.get(Conversation, conversation_id)
                if not conversation:
                    return False

                conversation.title = title
                conversation.updated_at = datetime.utcnow()

                await session.commit()
                return True

            except Exception as e:
                await session.rollback()
                raise e

    async def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation."""
        async with self.async_session() as session:
            try:
                conversation = await session.get(Conversation, conversation_id)
                if not conversation:
                    return False

                await session.delete(conversation)
                await session.commit()
                return True

            except Exception as e:
                await session.rollback()
                raise e

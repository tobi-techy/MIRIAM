from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from miriam_agent.database.models import (
    Base,
    Conversation,
    FinancialProfile,
    MemoryEntry,
    Message,
)


class MemoryStore:
    """Memory store for handling conversations, memories, and user interactions."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine: AsyncEngine | None = None
        self.async_session: async_sessionmaker[AsyncSession] | None = None

    async def initialize(self):
        """Initialize the database connection."""
        # Create async engine
        self.engine = create_async_engine(self.database_url)

        # Create tables
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Create async session factory
        self.async_session = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        """Yield an AsyncSession; callers must call ``initialize()`` first."""
        if self.async_session is None:
            raise RuntimeError("MemoryStore.initialize() must be called before use")
        async with self._session() as session:
            yield session

    async def close(self):
        """Close database connections."""
        if self.engine:
            await self.engine.dispose()

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def ensure_user(self, user: Any) -> None:
        """Upsert a user row so conversations can reference it.

        The Go backend is the identity authority; this inserts (or no-ops on
        existing) the user row mirroring the JWT claims used for agent context.
        """
        from miriam_agent.database.models import User

        async with self._session() as session:
            existing = await session.get(User, user.id)
            if existing is not None:
                return
            row = User(
                id=user.id,
                username=getattr(user, "username", "unknown") or "unknown",
                email=getattr(user, "email", "") or "",
                full_name=getattr(user, "full_name", user.id) or user.id,
            )
            session.add(row)
            await session.commit()

    async def store_interaction(
        self,
        user_id: str,
        role: str,
        content: str,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store an interaction (user message or assistant response)."""
        async with self._session() as session:
            try:
                # Create or get conversation
                if conversation_id:
                    conversation = await session.get(Conversation, conversation_id)
                    if conversation is None:
                        # conversation_id provided but doesn't exist - create it
                        title = f"Conversation {datetime.utcnow():%Y-%m-%d %H:%M}"
                        conversation = Conversation(
                            user_id=user_id,
                            title=title,
                            id=conversation_id,
                        )
                        session.add(conversation)
                        await session.flush()
                else:
                    # Create new conversation
                    title = f"Conversation {datetime.utcnow():%Y-%m-%d %H:%M}"
                    conversation = Conversation(
                        user_id=user_id,
                        title=title,
                    )
                    session.add(conversation)
                    await session.flush()

                # Store message
                message = Message(
                    conversation_id=conversation.id,
                    role=role,
                    content=content,
                    extra_data=metadata or {},
                )
                session.add(message)

                # Also store in memory entries for long-term memory
                memory_entry = MemoryEntry(
                    user_id=user_id,
                    type="conversation",
                    content=content,
                    extra_data={
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
    ) -> list[dict[str, Any]]:
        """Get conversation history."""
        async with self._session() as session:
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
                        "metadata": msg.extra_data,
                        "created_at": msg.created_at.isoformat(),
                    }
                    for msg in messages.scalars()
                ]

            except Exception as e:
                raise e

    async def get_recent_interactions(
        self, user_id: str, limit: int = 10
    ) -> list[MemoryEntry]:
        """Get recent interactions for a user."""
        async with self._session() as session:
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
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store a memory entry."""
        async with self._session() as session:
            try:
                memory_entry = MemoryEntry(
                    user_id=user_id,
                    type=memory_type,
                    content=content,
                    extra_data=metadata or {},
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
        memory_type: str | None = None,
        limit: int = 10,
        filter_metadata: dict[str, Any] | None = None,
    ) -> list[MemoryEntry]:
        """Retrieve memories for a user."""
        async with self._session() as session:
            try:
                # Build query
                query = select(MemoryEntry).where(MemoryEntry.user_id == user_id)

                if memory_type:
                    query = query.where(MemoryEntry.type == memory_type)

                if filter_metadata:
                    for key, value in filter_metadata.items():
                        query = query.where(
                            MemoryEntry.extra_data[key].astext == str(value)
                        )

                # Execute query
                memory_entries = await session.execute(
                    query.order_by(MemoryEntry.created_at.desc()).limit(limit)
                )

                return list(memory_entries.scalars())

            except Exception as e:
                raise e

    async def update_memory(
        self, memory_id: str, content: str, metadata: dict[str, Any] | None = None
    ) -> bool:
        """Update a memory entry."""
        async with self._session() as session:
            try:
                memory_entry = await session.get(MemoryEntry, memory_id)
                if not memory_entry:
                    return False

                memory_entry.content = content
                if metadata:
                    memory_entry.extra_data.update(metadata)

                await session.commit()
                return True

            except Exception as e:
                await session.rollback()
                raise e

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory entry."""
        async with self._session() as session:
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
    ) -> list[MemoryEntry]:
        """Search for memories based on query text."""
        async with self._session() as session:
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
                            MemoryEntry.extra_data["content"].astext.ilike(search_term),
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
    ) -> list[MemoryEntry]:
        """Get user's financial context from memories."""
        return await self.retrieve_memory(
            user_id=user_id,
            memory_type="financial",
            limit=limit,
        )

    async def get_user_preferences(
        self, user_id: str, limit: int = 10
    ) -> list[MemoryEntry]:
        """Get user's preferences from memories."""
        return await self.retrieve_memory(
            user_id=user_id,
            memory_type="preference",
            limit=limit,
        )

    async def get_user_goals(self, user_id: str, limit: int = 10) -> list[MemoryEntry]:
        """Get user's financial goals from memories."""
        return await self.retrieve_memory(
            user_id=user_id,
            memory_type="goal",
            limit=limit,
        )

    async def get_conversations(
        self, user_id: str, limit: int = 10
    ) -> list[Conversation]:
        """Get conversations for a user."""
        async with self._session() as session:
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

    async def get_conversation_messages(self, conversation_id: str) -> list[Message]:
        """Get all messages for a conversation."""
        async with self._session() as session:
            try:
                messages = await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at)
                )

                return list(messages.scalars())

            except Exception as e:
                raise e

    async def create_conversation(self, user_id: str, title: str) -> Conversation:
        """Create a new conversation."""
        async with self._session() as session:
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

    async def update_conversation_title(self, conversation_id: str, title: str) -> bool:
        """Update conversation title."""
        async with self._session() as session:
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
        async with self._session() as session:
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

    async def get_financial_profile(self, user_id: str) -> FinancialProfile | None:
        """Get user's financial profile."""
        async with self._session() as session:
            try:
                result = await session.execute(
                    select(FinancialProfile).where(FinancialProfile.user_id == user_id)
                )
                return result.scalars().first()
            except Exception as e:
                raise e

    async def get_config(self) -> dict[str, Any]:
        """Get agent configuration."""
        from miriam_agent.config.settings import get_settings

        settings = get_settings()
        return {
            "grpc_endpoint": settings.GRPC_ENDPOINT,
            "go_backend_url": settings.GO_BACKEND_URL,
            "openai_model": settings.OPENAI_MODEL,
            "max_daily_transfer": settings.MAX_DAILY_TRANSFER,
            "max_transaction_amount": settings.MAX_TRANSACTION_AMOUNT,
        }

    async def get_portfolio_data(self, user_id: str) -> dict[str, Any]:
        """Get user's portfolio data (stocks, investments)."""
        return {
            "investments": [],
            "total_value": 0.0,
            "daily_returns": [],
            "allocations": {},
        }

    async def get_income_data(self, user_id: str) -> dict[str, Any]:
        """Get user's income data."""
        profile = await self.get_financial_profile(user_id)
        return {
            "monthly_income": profile.monthly_income if profile else 0.0,
            "income_streams": [],
            "frequency": "monthly",
        }

    async def get_expense_data(self, user_id: str) -> dict[str, Any]:
        """Get user's expense data."""
        return {
            "monthly_expenses": 0.0,
            "categories": {},
            "transactions": [],
        }


_memory_singleton: Any = None


def get_memory_singleton(database_url: str | None = None) -> "MemoryStore":
    """Get the process-wide MemoryStore singleton.

    Must be initialized (``await initialize()``) once at application
    startup before use.
    """
    global _memory_singleton
    if _memory_singleton is None:
        from miriam_agent.config.settings import get_settings

        url = database_url or get_settings().DATABASE_URL
        _memory_singleton = MemoryStore(url)
    return _memory_singleton

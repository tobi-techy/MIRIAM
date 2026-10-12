import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from miriam_agent.core.exceptions import AuthorizationError
from miriam_agent.core.timeutil import utcnow_naive
from miriam_agent.database.identity_memory import IdentityMemoryMixin
from miriam_agent.database.models import (
    Base,
    Conversation,
    FinancialProfile,
    MemoryEntry,
    Message,
)

logger = logging.getLogger(__name__)

# Go's agent token often has no username. Every such user used to be stored as
# "unknown", so the second signup hit users_username_key and the chat 500'd.
_SHARED_USERNAMES = frozenset({"", "unknown", "user", "none", "null"})


def mirror_username(user_id: str, username: str | None) -> str:
    raw = (username or "").strip()
    if raw.casefold() in _SHARED_USERNAMES:
        return f"u-{user_id}"[:80]
    return raw[:80]


def mirror_email(user_id: str, email: str | None) -> str:
    raw = (email or "").strip()
    if raw:
        return raw[:254]
    return f"{user_id}@users.miriam.invalid"


class MemoryStore(IdentityMemoryMixin):
    """Memory store for handling conversations, memories, and user interactions."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine: AsyncEngine | None = None
        self.async_session: async_sessionmaker[AsyncSession] | None = None

    async def initialize(self):
        """Initialize the database connection."""
        # Create async engine
        self.engine = create_async_engine(self.database_url)

        # In development/tests the DB is created via ``create_all`` so the
        # suite works without a migration. In deployed environments Alembic
        # is the source of truth (see ``alembic/versions/001_baseline.py``);
        # ``create_all`` is idempotent there and will not overwrite migrated
        # tables, but no DDL differences should be introduced outside a
        # migration (G27). Deploys must run ``alembic upgrade head`` before
        # starting the app so create_all never masks a missing migration.
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Create async session factory
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        """Yield an AsyncSession; callers must call ``initialize()`` first."""
        if self.async_session is None:
            raise RuntimeError("MemoryStore.initialize() must be called before use")
        async with self.async_session() as session:
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

        Idempotent and race-safe: concurrent first-requests for the same user
        collapse to one row via an IntegrityError catch (the row won via the
        competing transaction). Database errors other than a duplicate-key
        conflict are re-raised so callers never proceed as if the account
        exists when persistence actually failed.
        """
        from sqlalchemy.exc import IntegrityError

        from miriam_agent.database.models import User

        user_id = getattr(user, "id", None)
        if not user_id:
            raise ValueError("ensure_user requires a user with an id")
        async with self._session() as session:
            existing = await session.get(User, user_id)
            if existing is not None:
                return
            username = mirror_username(user_id, getattr(user, "username", None))
            email = mirror_email(user_id, getattr(user, "email", None))
            full_name = getattr(user, "full_name", None) or user_id
            row = User(
                id=user_id,
                username=username,
                email=email,
                full_name=full_name,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                # Lost a race with another request creating the same user,
                # or a conflicting unique username/email row. Roll back and
                # re-check: if our id row now exists the account is usable.
                # A shared username like "unknown" is retried once as u-<id>.
                await session.rollback()
                existing = await session.get(User, user_id)
                if existing is not None:
                    return
                unique_name = f"u-{user_id}"[:80]
                if username == unique_name:
                    raise
                session.add(
                    User(
                        id=user_id,
                        username=unique_name,
                        email=email,
                        full_name=full_name,
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    existing = await session.get(User, user_id)
                    if existing is not None:
                        return
                    raise

    async def store_interaction(
        self,
        user_id: str,
        role: str,
        content: str,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store an interaction (user message or assistant response).

        Writing into an existing conversation is ownership-checked: without it
        a caller could append messages into another user's conversation simply
        by presenting its id.
        """
        async with self._session() as session:
            try:
                # Create or get conversation
                if conversation_id:
                    conversation = await session.get(Conversation, conversation_id)
                    if conversation is None:
                        # conversation_id provided but doesn't exist - create it
                        title = f"Conversation {utcnow_naive():%Y-%m-%d %H:%M}"
                        conversation = Conversation(
                            user_id=user_id,
                            title=title,
                            id=conversation_id,
                        )
                        session.add(conversation)
                        await session.flush()
                    elif conversation.user_id != user_id:
                        raise AuthorizationError(
                            "Conversation belongs to a different user"
                        )
                    else:
                        # Bump recency so GET /conversations ordering (and the
                        # resume endpoint) reflects the latest message, not
                        # the conversation's birth. onupdate= only fires on
                        # UPDATE of this row, not on child message inserts.
                        conversation.updated_at = utcnow_naive()
                else:
                    # Create new conversation
                    title = f"Conversation {utcnow_naive():%Y-%m-%d %H:%M}"
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
                        "timestamp": utcnow_naive().isoformat(),
                        **(metadata or {}),
                    },
                )
                session.add(memory_entry)

                await session.commit()

                return conversation.id

            except Exception as e:
                await session.rollback()
                raise e

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        """Fetch a conversation by id, regardless of owner."""
        async with self._session() as session:
            return await session.get(Conversation, conversation_id)

    async def get_owned_conversation(
        self, conversation_id: str, user_id: str
    ) -> Conversation | None:
        """Return the conversation only when it belongs to ``user_id``.

        Returns ``None`` both for "does not exist" and "belongs to someone
        else". Callers that must distinguish the two (the chat write path,
        which creates a conversation on first use) use ``get_conversation``
        and compare owners themselves.
        """
        conversation = await self.get_conversation(conversation_id)
        if conversation is None or conversation.user_id != user_id:
            return None
        return conversation

    async def get_conversation_history(
        self, conversation_id: str, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get conversation history, optionally scoped to its owner.

        ``user_id`` is optional only for internal callers that minted the id
        themselves for an already-authenticated user (onboarding); every
        request-driven caller passes it. A mismatch returns no history rather
        than another user's messages.
        """
        async with self._session() as session:
            try:
                # Get conversation
                conversation = await session.get(Conversation, conversation_id)
                if not conversation:
                    return []

                if user_id is not None and conversation.user_id != user_id:
                    logger.warning(
                        "Refusing cross-user conversation read",
                        extra={
                            "conversation_id": conversation_id,
                            "requesting_user": user_id,
                        },
                    )
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
        """Hybrid-lite search: token-overlap + recency, vector-ready.

        Why not pure ILIKE: exact-substring misses paraphrases and buries
        recent important facts. This scores candidates by token overlap
        (BM25-lite) with recency boost, so fresh relevant memories win.
        When a pgvector column exists with embeddings, prefer it; else this
        path keeps local recall sovereign without Supermemory.
        """
        async with self._session() as session:
            try:
                tokens = [t.lower() for t in query.split() if len(t) > 2][:8]
                if not tokens:
                    tokens = [query.lower()]
                clauses = []
                for tok in tokens:
                    pat = f"%{tok}%"
                    clauses.append(MemoryEntry.content.ilike(pat))
                # Fetch broader candidate set, rank in Python (small N, cheap).
                candidates = await session.execute(
                    select(MemoryEntry)
                    .where(MemoryEntry.user_id == user_id)
                    .where(or_(*clauses) if clauses else True)
                    .order_by(MemoryEntry.created_at.desc())
                    .limit(max(limit * 4, 20))
                )
                rows = list(candidates.scalars())
                if not rows:
                    return []

                def _score(m: MemoryEntry) -> float:
                    text = (m.content or "").lower()
                    hits = sum(1 for t in tokens if t in text)
                    # Recency boost: newer first within same hit count.
                    try:
                        age_h = (
                            utcnow_naive() - (m.created_at or utcnow_naive())
                        ).total_seconds() / 3600.0
                    except Exception:
                        age_h = 0.0
                    recency = 1.0 / (1.0 + age_h / 72.0)
                    return hits * 2.0 + recency

                rows.sort(key=_score, reverse=True)
                return rows[:limit]

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

    async def get_conversation_messages(
        self, conversation_id: str, user_id: str | None = None
    ) -> list[Message]:
        """Get all messages for a conversation, optionally owner-scoped."""
        async with self._session() as session:
            try:
                if user_id is not None:
                    if (
                        await self.get_owned_conversation(conversation_id, user_id)
                        is None
                    ):
                        return []
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
                conversation.updated_at = utcnow_naive()

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

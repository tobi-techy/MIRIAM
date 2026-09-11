"""FastAPI dependency injection for Miriam Financial Agent."""

import os
from typing import Any, AsyncGenerator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from miriam_agent.auth.jwt import decode_token
from miriam_agent.config.settings import get_settings, Settings
from miriam_agent.core.exceptions import AuthenticationError
from miriam_agent.database.memory import MemoryStore
from miriam_agent.database.models import User
from miriam_agent.financial.intelligence import FinancialIntelligence

security = HTTPBearer()
_settings = get_settings()

# Module-level singletons (initialized on first request)
_memory_store: MemoryStore | None = None
_financial_intelligence: FinancialIntelligence | None = None


async def get_settings_dep() -> Settings:
    """Get application settings."""
    return _settings


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> User:
    """Extract and validate JWT token issued by the Go backend."""
    try:
        payload = decode_token(credentials.credentials)
        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing subject",
            )
    except AuthenticationError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )

    # In production, the source of truth for user data is the Go backend.
    # Construct a minimal User object from token claims for agent context.
    user = User()
    user.id = user_id
    user.username = payload.get("username", "unknown")
    user.email = payload.get("email", "")
    user.full_name = payload.get("full_name", "Unknown User")
    user.is_active = True
    return user


async def get_memory_store() -> AsyncGenerator[MemoryStore, None]:
    """Get or create the memory store singleton."""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore(_settings.DATABASE_URL)
        await _memory_store.initialize()
    yield _memory_store


async def get_financial_intelligence(
    memory_store: MemoryStore = Depends(get_memory_store),
) -> AsyncGenerator[FinancialIntelligence, None]:
    """Get or create the financial intelligence singleton."""
    global _financial_intelligence
    if _financial_intelligence is None:
        _financial_intelligence = FinancialIntelligence(memory_store)
    yield _financial_intelligence


async def get_go_client_dep() -> AsyncGenerator[Any, None]:
    """Get or create the Go backend client singleton."""
    from miriam_agent.integrations.go_client import get_go_client

    client = get_go_client()
    yield client


async def get_supermemory_memory_dep() -> AsyncGenerator[Any, None]:
    """Get or create the Supermemory memory service singleton."""
    from miriam_agent.conversational.supermemory_memory import SupermemoryMemory
    from miriam_agent.integrations.supermemory_client import get_supermemory_client

    service = SupermemoryMemory(get_supermemory_client())
    yield service

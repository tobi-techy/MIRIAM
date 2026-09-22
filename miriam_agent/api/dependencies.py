"""FastAPI dependency injection for Miriam Financial Agent."""

import secrets
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from miriam_agent.auth.jwt import decode_token
from miriam_agent.config.settings import Settings, get_settings
from miriam_agent.core.exceptions import AuthenticationError
from miriam_agent.database.memory import MemoryStore
from miriam_agent.database.models import User
from miriam_agent.financial.intelligence import FinancialIntelligence

security = HTTPBearer()
_settings = get_settings()

# Module-level singletons (initialized on first request)
_memory_store: MemoryStore | None = None
_financial_intelligence: FinancialIntelligence | None = None
_audit_system: Any | None = None


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
    user.roles = _roles_from_payload(payload)
    return user


def _roles_from_payload(payload: dict[str, Any]) -> list[str]:
    """Extract role claims from a Go-issued JWT.

    Go tokens use a single ``role`` string claim. We also accept a
    ``roles`` list claim for forward compatibility, and promote the
    token's ``verified`` flag into the ``verified`` role so money
    actions are permitted for KYC-verified users without a separate
    profile fetch.
    """
    roles: list[str] = []
    raw_roles = payload.get("roles")
    if isinstance(raw_roles, list):
        roles = [r for r in raw_roles if isinstance(r, str)]
    elif isinstance(raw_roles, str):
        roles = [raw_roles]
    raw_role = payload.get("role")
    if isinstance(raw_role, str) and raw_role not in roles:
        roles.append(raw_role)
    if payload.get("verified") is True and "verified" not in roles:
        roles.append("verified")
    return roles or ["user"]


async def get_bearer_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """Return the raw bearer token for forward calls to the Go backend."""
    return credentials.credentials


async def require_rail_service_key(
    x_rail_service_key: str | None = Header(
        default=None, alias="X-Rail-Service-Key"
    ),
) -> None:
    """Rail-only credential for endpoints that write ledger facts.

    ``POST /money/inflow`` mints money in the ledger. A user JWT names an
    account but must never be authority enough to credit it, so the endpoint
    additionally requires the shared rail service key, constant-time
    compared. Refuses closed when the key is not configured: the production
    settings guard forces it to be set, and any other environment that has
    not configured it has no working inflow webhook rather than an
    unauthenticated one.
    """
    expected = get_settings().RAIL_SERVICE_KEY
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="inflow endpoint not configured: RAIL_SERVICE_KEY is unset",
        )
    if not x_rail_service_key or not secrets.compare_digest(
        x_rail_service_key, expected
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a valid X-Rail-Service-Key header is required here",
        )


async def get_or_init_memory_store() -> MemoryStore:
    """Return the shared memory store, initializing it on first use.

    Public accessor so readiness probes reuse the same store as request
    handlers instead of building a fresh one per scrape.
    """
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore(_settings.DATABASE_URL)
        await _memory_store.initialize()
    return _memory_store


async def get_memory_store() -> AsyncGenerator[MemoryStore, None]:
    """Get or create the memory store singleton."""
    yield await get_or_init_memory_store()


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


async def get_audit_system() -> AsyncGenerator[Any, None]:
    """Get or create the audit system singleton (fail-open)."""
    global _audit_system
    if _audit_system is None:
        try:
            from miriam_agent.safety.audit import AuditSystem

            _audit_system = AuditSystem(_settings.DATABASE_URL)
            await _audit_system.initialize()
        except Exception:
            # Audit must never break the chat path; keep it available but inert.
            _audit_system = None
    yield _audit_system

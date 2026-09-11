"""OAuth helpers for Miriam Financial Agent.

Real OAuth provider flows (Google, etc.) are handled by the Go backend.
This module provides lightweight state token management for linking
third-party integrations initiated from the Python side.
"""

import secrets
import time

_STATE_TOKENS: dict[str, float] = {}
_TTL_SECONDS = 10 * 60


def create_state_token() -> str:
    """Create a one-time state token for an OAuth/redirect flow."""
    token = secrets.token_urlsafe(32)
    _STATE_TOKENS[token] = time.time() + _TTL_SECONDS
    return token


def validate_state_token(token: str) -> bool:
    """Validate and consume a state token (single use, TTL checked)."""
    if token not in _STATE_TOKENS:
        return False
    expires_at = _STATE_TOKENS.pop(token)
    return time.time() < expires_at


def _cleanup() -> None:
    """Remove expired state tokens."""
    now = time.time()
    expired = [t for t, exp in _STATE_TOKENS.items() if exp < now]
    for t in expired:
        _STATE_TOKENS.pop(t, None)

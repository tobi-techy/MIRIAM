"""JWT authentication for Miriam Financial Agent.

Validates tokens issued by the Go backend (RAIL_BACKEND) and supports
issuing short-lived Python-side session tokens for internal services.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt as pyjwt

from miriam_agent.config.settings import get_settings
from miriam_agent.core.exceptions import AuthenticationError

_ALGORITHM = "HS256"


def decode_token(token: str, secret: Optional[str] = None) -> Dict[str, Any]:
    """Validate and decode a JWT signed by the Go backend.

    The Go backend signs JWTs with the same JWT_SECRET. PyJWT accepts
    the standard claims (sub, exp, iat, aud) that golang-jwt produces.
    """
    settings = get_settings()
    jwt_secret = secret or settings.JWT_SECRET
    try:
        return pyjwt.decode(token, jwt_secret, algorithms=[_ALGORITHM])
    except pyjwt.ExpiredSignatureError:
        raise AuthenticationError("Token has expired")
    except pyjwt.InvalidAudienceError:
        raise AuthenticationError("Invalid audience")
    except pyjwt.InvalidTokenError:
        raise AuthenticationError("Invalid token")


def create_token(
    user_id: str,
    claims: Optional[Dict[str, Any]] = None,
    expires_minutes: Optional[int] = None,
    secret: Optional[str] = None,
) -> str:
    """Create a signed JWT for the given user."""
    settings = get_settings()
    jwt_secret = secret or settings.JWT_SECRET
    ttl = expires_minutes or settings.JWT_EXPIRATION_MINUTES
    payload = {
        "sub": user_id,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ttl),
        **(claims or {}),
    }
    return pyjwt.encode(payload, jwt_secret, algorithm=_ALGORITHM)


def get_user_id(token: str) -> str:
    """Extract the user ID (sub claim) from a token."""
    payload = decode_token(token)
    sub = payload.get("sub")
    if not sub:
        raise AuthenticationError("Token missing subject")
    return str(sub)


def has_role(payload: Dict[str, Any], role: str) -> bool:
    """Check whether the token payload carries the given role."""
    roles = payload.get("roles")
    if isinstance(roles, list):
        return role in roles
    return payload.get("role") == role
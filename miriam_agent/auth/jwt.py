"""JWT authentication for Miriam Financial Agent.

Validates tokens issued by the Go backend (RAIL_BACKEND) and supports
issuing short-lived Python-side session tokens for internal services.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt

from miriam_agent.config.settings import get_settings
from miriam_agent.core.exceptions import AuthenticationError

_DEFAULT_ALGORITHM = "HS256"


def _algorithm() -> str:
    """Signing algorithm, from configuration.

    The ``JWT_ALGORITHM`` setting used to be defined and never read (HS256 was
    hardcoded), so changing it silently did nothing.
    """
    return get_settings().JWT_ALGORITHM or _DEFAULT_ALGORITHM


def _verify_options() -> dict[str, Any]:
    """PyJWT decode options: require the claims this service depends on.

    ``exp`` must be present. Without ``require``, a token carrying no expiry
    was accepted as forever-valid, and ``aud`` was never checked at all (the
    ``InvalidAudienceError`` handler below was unreachable).
    """
    options: dict[str, Any] = {"require": ["exp", "sub"]}
    if not get_settings().JWT_AUDIENCE:
        # The Go issuer does not set `aud` today; only enforce it once a real
        # audience is configured, rather than rejecting every live token.
        options["verify_aud"] = False
    return options


def decode_token(token: str) -> dict[str, Any]:
    """Validate and decode a JWT signed by the Go backend.

    The Go backend signs JWTs with the same JWT_SECRET. The algorithm is
    pinned to configuration (never the token's own ``alg`` header, which is
    what blocks ``alg: none`` / algorithm confusion), and ``exp``/``sub`` are
    required. There is deliberately no secret override here: the verify path
    must always use the configured secret.
    """
    settings = get_settings()
    jwt_secret = settings.JWT_SECRET
    kwargs: dict[str, Any] = {}
    if settings.JWT_AUDIENCE:
        kwargs["audience"] = settings.JWT_AUDIENCE
    if settings.JWT_ISSUER:
        kwargs["issuer"] = settings.JWT_ISSUER
    try:
        return pyjwt.decode(
            token,
            jwt_secret,
            algorithms=[_algorithm()],
            options=_verify_options(),
            **kwargs,
        )
    except pyjwt.ExpiredSignatureError:
        raise AuthenticationError("Token has expired")
    except pyjwt.InvalidAudienceError:
        raise AuthenticationError("Invalid audience")
    except pyjwt.InvalidIssuerError:
        raise AuthenticationError("Invalid issuer")
    except pyjwt.MissingRequiredClaimError:
        raise AuthenticationError("Token missing a required claim")
    except pyjwt.InvalidTokenError:
        raise AuthenticationError("Invalid token")


def create_token(
    user_id: str,
    claims: dict[str, Any] | None = None,
    expires_minutes: int | None = None,
) -> str:
    """Create a signed JWT for the given user."""
    settings = get_settings()
    jwt_secret = settings.JWT_SECRET
    if jwt_secret in ("", "change-me-in-production") and settings.is_production:
        raise AuthenticationError(
            "Refusing to mint JWTs with the default JWT_SECRET in production"
        )
    ttl = expires_minutes or settings.JWT_EXPIRATION_MINUTES
    payload = {
        "sub": user_id,
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=ttl),
        **(claims or {}),
    }
    if settings.JWT_AUDIENCE:
        payload.setdefault("aud", settings.JWT_AUDIENCE)
    if settings.JWT_ISSUER:
        payload.setdefault("iss", settings.JWT_ISSUER)
    return pyjwt.encode(payload, jwt_secret, algorithm=_algorithm())


def get_user_id(token: str) -> str:
    """Extract the user ID (sub claim) from a token."""
    payload = decode_token(token)
    sub = payload.get("sub")
    if not sub:
        raise AuthenticationError("Token missing subject")
    return str(sub)


def has_role(payload: dict[str, Any], role: str) -> bool:
    """Check whether the token payload carries the given role."""
    roles = payload.get("roles")
    if isinstance(roles, list):
        return role in roles
    return payload.get("role") == role

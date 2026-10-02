"""One place to build the async Redis client.

Managed Redis (Upstash) closes idle connections, and redis-py only pings idle
pooled connections when ``health_check_interval`` is set — its default is 0. A
socket closed while the service was quiet then surfaces as a ConnectionError on
the next command, and for the ledger that is a refused money turn rather than a
retry. Every Redis client in the service is built here so the ledger, the safety
validator and the onboarding/proactive stores share one hardened configuration.

The factory also names the misconfiguration that looks like an outage: a
``rediss://`` URL with no password. Managed Redis requires the token in the URL
(``rediss://default:<token>@<host>:<port>``); without it every command fails
with "Authentication required", which the ledger reports to the user as a plain
refusal ("I could not complete that. Try again.") with nothing in the message to
say why.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Ping a pooled connection before reuse so an idle-closed socket is discarded
# instead of raising on the next command.
HEALTH_CHECK_INTERVAL_SECONDS = 30
SOCKET_TIMEOUT_SECONDS = 5
MAX_CONNECTIONS = 20


def missing_credentials(url: str) -> bool:
    """True when a TLS Redis URL carries no password, so it cannot authenticate."""
    try:
        parsed = urlparse(url)
    except ValueError:  # pragma: no cover - malformed URL
        return False
    return parsed.scheme == "rediss" and not parsed.password


def _describe_host(url: str) -> str:
    try:
        return urlparse(url).hostname or "<host>"
    except ValueError:  # pragma: no cover - malformed URL
        return "<host>"


def build_redis_client(url: str | None = None) -> Any:
    """Build the shared, hardened async Redis client.

    ``url`` falls back to ``settings.REDIS_URL`` when omitted.
    """
    import redis.asyncio as aioredis
    from redis.backoff import ExponentialWithJitterBackoff
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError
    from redis.retry import Retry

    from miriam_agent.config.settings import get_settings

    resolved = url or get_settings().REDIS_URL
    if missing_credentials(resolved):
        logger.error(
            "REDIS_URL is TLS but carries no password (%s). Managed Redis needs "
            "the token in the URL, e.g. rediss://default:<token>@%s:6379 — until "
            "then every money turn refuses because the ledger cannot "
            "authenticate. Onboarding still works from its in-process store, "
            "which is why only money turns fail.",
            _describe_host(resolved),
            _describe_host(resolved),
        )
    return aioredis.from_url(
        resolved,
        decode_responses=True,
        health_check_interval=HEALTH_CHECK_INTERVAL_SECONDS,
        socket_keepalive=True,
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        max_connections=MAX_CONNECTIONS,
        retry=Retry(ExponentialWithJitterBackoff(base=0.1, cap=2.0), retries=3),
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
    )


async def check_redis_connectivity() -> str:
    """Ping Redis once and return ``"ok"`` or a short reason. Never raises.

    A broken Redis must not stop the API booting (onboarding keeps working), but
    the reason belongs in the logs before the first money turn fails.
    """
    client = None
    try:
        client = build_redis_client()
        await client.ping()
        return "ok"
    except Exception as exc:  # noqa: BLE001 - startup diagnostics, never fatal
        return f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - best effort
                pass

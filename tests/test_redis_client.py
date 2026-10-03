"""The shared Redis client must survive managed Redis (Upstash).

Two concrete failure modes are pinned here:

* a ``rediss://`` URL with no token cannot authenticate — every money turn then
  refuses while onboarding still works, which reads like a mystery outage;
* redis-py defaults ``health_check_interval`` to 0, so an idle-closed Upstash
  socket raises on the next command instead of being recycled.
"""
from __future__ import annotations

import pytest

from miriam_agent.core.redis_client import build_redis_client, missing_credentials
from miriam_agent.hands.ledger import RedisLedgerStore


def test_missing_credentials_flags_a_tls_url_without_a_token():
    # The exact shape observed in the runtime variable.
    assert missing_credentials("rediss://novel-ram-303881.upstash.io") is True
    assert missing_credentials("rediss://host:6379") is True


def test_missing_credentials_allows_complete_and_plain_urls():
    assert missing_credentials("rediss://default:token@host:6379") is False
    assert missing_credentials("redis://localhost:6379/0") is False


def test_client_is_hardened_for_managed_redis():
    client = build_redis_client("rediss://default:token@example.upstash.io:6379")
    kwargs = client.connection_pool.connection_kwargs

    # Default is 0 in redis-py, which is the stale-connection trap.
    assert kwargs["health_check_interval"] == 30
    assert kwargs["socket_keepalive"] is True
    assert kwargs["socket_timeout"] is not None
    assert kwargs["socket_connect_timeout"] is not None
    # Transient drops are retried rather than surfaced as a refused turn.
    assert kwargs["retry_on_error"]


@pytest.mark.asyncio
async def test_ledger_store_builds_the_hardened_client():
    """The money path must go through the shared factory, not its own from_url."""
    store = RedisLedgerStore(
        "rediss://default:token@example.upstash.io:6379", single_process=True
    )
    client = await store._client()
    assert client.connection_pool.connection_kwargs["health_check_interval"] == 30

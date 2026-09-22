"""Shared pytest fixtures / hooks for the Miriam test suite.

Legacy sync tests run their async helpers through the pattern::

    asyncio.get_event_loop().run_until_complete(coro)

Under Python 3.12 this call raises ``RuntimeError: There is no current event
loop`` whenever there is no loop set for the main thread. pytest-asyncio in
``auto`` mode owns the loop for ``async def`` tests and tears its function-scoped
loop down after each one -- so once any async test has run, every *sync* test
that arrives afterwards (and that uses the legacy helper above) breaks, even
though the exact same test file passes when run on its own.

The robust, intended-for-tests fix is the loop-isolated pattern used by
``test_account_init.py`` (create your own loop, run, close). This conftest applies
that same guarantee centrally for the sync tests that still use the legacy
helper, so they keep working alongside the async tests without weakening any
assertion. Async tests are left untouched: pytest-asyncio keeps managing their
own loops.
"""

from __future__ import annotations

import asyncio
import inspect
import os

import pytest
from fastapi.testclient import TestClient

# The TypeSafe judgment layer is on by default in production, but tests must
# never hit the live API (or depend on a key existing). Force it off before any
# miriam_agent module is imported so the cached Settings pick it up.
os.environ["TYPESAFE_ENABLED"] = "false"

# The inflow endpoint mints ledger money, so it demands the rail service key
# (a user JWT alone is refused). Tests exercise that rule: this key is what
# test inflow calls must present in the X-Rail-Service-Key header. Set before
# any miriam_agent import so cached Settings see it.
os.environ.setdefault("RAIL_SERVICE_KEY", "test-rail-service-key-0123456789abcdef")

from miriam_agent.api.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Provide a TestClient for the Miriam API.

    Required by ``test_traceability.py::test_trace_id_in_response_and_header``.
    """
    return TestClient(app)


@pytest.fixture(autouse=True)
def _ensure_current_event_loop(request):
    """Give sync tests a current event loop.

    Only touches non-async tests (``async def`` tests are handled by
    pytest-asyncio). For sync tests we (re)establish a current loop on the main
    thread when none is available, so the legacy ``get_event_loop()`` helper
    works regardless of what prior async tests tore down.
    """
    if inspect.iscoroutinefunction(request.node.function):
        yield
        return
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield

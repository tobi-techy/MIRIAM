"""Airtime network detection and Naira deposit detail tool flows.

Regression tests for the reported product failures:

* "I would love to get airtime" + phone number must resolve the network via
  the ``detect_network`` tool (success path and provider-failure path).
* "What account do I send Naira to" must invoke ``get_deposit_details`` and
  answer from live backend data, never invented details.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

import miriam_agent.tools.definitions  # noqa: E402, F401  (registration side effect)
from miriam_agent.agents.tools import get_registry  # noqa: E402
from miriam_agent.integrations import go_client  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if not asyncio.get_event_loop().is_running() \
        else asyncio.new_event_loop().run_until_complete(coro)


class _AirtimeOK:
    async def detect_network(self, token, phone):
        assert token == "tok"
        assert phone == "08012345678"
        return {"network_id": "01", "network": "MTN"}


class _DepositOK:
    async def get_ngn_virtual_account(self, token):
        assert token == "tok"
        return {
            "virtual_account": {
                "bank_name": "Graph Bank",
                "account_number": "0123456789",
                "account_name": "Ada Obi",
            }
        }

    async def create_deposit_address(self, token, chain="base", currency="USDC"):
        raise AssertionError("NGN account exists, no crypto fallback needed")


class _BillDown:
    async def detect_network(self, token, phone):
        raise Exception("bill service unreachable")


class _NoDepositAnywhere:
    async def get_ngn_virtual_account(self, token):
        return {"_tool_error": "no Naira deposit account exists for this user yet"}

    async def create_deposit_address(self, token, chain="base", currency="USDC"):
        return {"_tool_error": "deposit service down"}


def _registry():
    return get_registry()


def test_airtime_phone_resolves_network_via_tool():
    go_client._client = _AirtimeOK()
    result = _run(
        _registry().execute(
            "detect_network",
            {"phone": "08012345678"},
            {"user_id": "u1", "token": "tok"},
        )
    )
    assert result["network_id"] == "01"
    assert result["network"] == "MTN"
    assert result["phone"] == "08012345678"
    assert "_tool_error" not in result


def test_airtime_provider_failure_is_structured_not_silent():
    go_client._client = _BillDown()
    result = _run(
        _registry().execute(
            "detect_network",
            {"phone": "08012345678"},
            {"user_id": "u1", "token": "tok"},
        )
    )
    assert "_tool_error" in result
    assert "bill service" in result["_tool_error"]
    assert "hint" in result


def test_airtime_missing_auth_is_structured():
    go_client._client = _AirtimeOK()
    result = _run(
        _registry().execute(
            "detect_network", {"phone": "08012345678"}, {"user_id": "u1"}
        )
    )
    assert result["_tool_error"] == "missing auth token"


def test_naira_deposit_returns_real_bank_transfer_details():
    go_client._client = _DepositOK()
    result = _run(
        _registry().execute(
            "get_deposit_details", {}, {"user_id": "u1", "token": "tok"}
        )
    )
    assert result["rail"] == "bank_transfer"
    assert result["currency"] == "NGN"
    assert result["virtual_account"]["account_number"] == "0123456789"
    assert "_tool_error" not in result


def test_naira_deposit_failure_is_honest_not_invented():
    go_client._client = _NoDepositAnywhere()
    result = _run(
        _registry().execute(
            "get_deposit_details", {}, {"user_id": "u1", "token": "tok"}
        )
    )
    assert "_tool_error" in result
    assert "virtual_account" not in result


def test_deposit_tool_is_discoverable_for_funding_intents():
    registry = _registry()
    tool = registry.get("get_deposit_details")
    assert tool is not None
    haystack = (tool.description + " " + tool.name).casefold()
    for keyword in ("deposit", "naira", "bank", "fund"):
        assert keyword in haystack
    network_tool = registry.get("detect_network")
    assert network_tool is not None
    assert "airtime" in network_tool.description.casefold()

"""NGN <-> crypto funding read tools (RampHub / Paj).

Reads are registry tools (LOW, auto-execute). Mutations live in hands/
behind a confirm_id tap and must never appear in the registry.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _client_for(handler):
    from miriam_agent.integrations.go_client import GoBackendClient

    transport = httpx.MockTransport(handler)
    client = GoBackendClient("http://go.test")
    _run(client._client.aclose())
    client._client = httpx.AsyncClient(
        base_url="http://go.test",
        transport=transport,
        headers={"Content-Type": "application/json", "X-Requested-With": "RailApp"},
    )
    return client


class _FakeGo:
    def __init__(self):
        self.calls: list[str] = []

    async def get_crypto_quote(self, token, side, amount):
        assert token == "tok"
        self.calls.append("quote")
        return {
            "side": "onramp",
            "provider": "paj",
            "rate": 1530.5,
            "estimatedOutput": 32.68,
            "tokenAmount": 32.68,
            "fee": 250,
        }

    async def get_funding_orders(self, token, kind="paj"):
        return [{"orderId": "ord-1", "status": "pending", "fiatAmount": 50000}]

    async def get_funding_order_status(self, token, kind, order_id):
        return {"orderId": order_id, "status": "pending", "fiatAmount": 50000}

    async def get_paj_banks(self, token, saved=False):
        assert token == "tok"
        return [{"bankId": "b1"}] if not saved else []

    async def get_paj_verification_status(self, token):
        return {"verified": False}


def _patch_go(monkeypatch, fake):
    import miriam_agent.tools.funding_definitions as fd

    monkeypatch.setattr(fd, "get_go_client", lambda: fake, raising=False)
    import miriam_agent.integrations.go_client as gc

    monkeypatch.setattr(gc, "get_go_client", lambda: fake, raising=False)


def test_quote_uses_ramp_path_and_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        params = httpx.URL(str(request.url)).params
        seen["side"] = params.get("side")
        seen["amount"] = params.get("amount")
        seen["currency"] = params.get("currency")
        return httpx.Response(
            200,
            json={"side": "onramp", "provider": "paj", "rate": 1530.5},
        )

    client = _client_for(handler)
    out = _run(client.get_crypto_quote("tok", "onramp", 50000))
    assert seen["path"] == "/api/v1/funding/ramp/quote"
    assert seen["side"] == "onramp"
    assert seen["amount"] == "50000"
    assert seen["currency"] == "ngn"
    assert out["rate"] == 1530.5
    _run(client.close())


def test_funding_order_paths():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/v1/funding/paj/orders":
            return httpx.Response(200, json={"orders": [{"orderId": "o1"}]})
        if request.url.path == "/api/v1/funding/paj/orders/o1/status":
            return httpx.Response(200, json={"orderId": "o1", "status": "pending"})
        if request.url.path == "/api/v1/funding/ramp/orders":
            return httpx.Response(200, json={"orders": [{"transactionId": "t1"}]})
        if request.url.path == "/api/v1/funding/ramp/orders/t1/status":
            return httpx.Response(200, json={"transactionId": "t1", "status": "done"})
        if request.url.path == "/api/v1/funding/paj/rates":
            return httpx.Response(200, json={"onRampRate": 1530})
        if request.url.path == "/api/v1/funding/paj/banks":
            return httpx.Response(200, json={"banks": [{"bankId": "b1"}]})
        if request.url.path == "/api/v1/funding/ramp/banks":
            return httpx.Response(200, json={"banks": [{"code": "058"}]})
        raise AssertionError(request.url.path)

    client = _client_for(handler)
    assert _run(client.get_funding_orders("tok", "paj")) == [{"orderId": "o1"}]
    status = _run(client.get_funding_order_status("tok", "paj", "o1"))
    assert status["status"] == "pending"
    assert _run(client.get_funding_orders("tok", "ramp")) == [{"transactionId": "t1"}]
    assert _run(client.get_paj_rates("tok"))["onRampRate"] == 1530
    assert _run(client.get_paj_banks("tok")) == [{"bankId": "b1"}]
    assert _run(client.get_ramp_banks("tok")) == [{"code": "058"}]
    _run(client.close())


def test_client_has_no_offramp_or_withdrawal_writers():
    from miriam_agent.integrations.go_client import GoBackendClient

    for gone in (
        "create_offramp",
        "submit_offramp",
        "create_withdrawal",
        "submit_withdrawal",
        "initiate_paj",
        "verify_paj",
        "create_onramp",
    ):
        assert not hasattr(GoBackendClient, gone), gone
    for kept in (
        "get_crypto_quote",
        "get_paj_rates",
        "get_funding_orders",
        "get_funding_order_status",
        "get_paj_banks",
    ):
        assert hasattr(GoBackendClient, kept), kept


def test_funding_read_tools_are_low_and_auto(monkeypatch):
    from miriam_agent.tools import build_tool_registry
    from miriam_agent.tools import funding_definitions as fd  # noqa: F401

    _patch_go(monkeypatch, _FakeGo())
    reg = build_tool_registry()
    for name in (
        "get_crypto_quote",
        "get_funding_orders",
        "get_funding_order_status",
        "get_paj_banks",
        "get_paj_verification_status",
    ):
        tool = reg.get(name)
        assert tool is not None, name
        assert tool.is_mutation is False, name
        assert tool.requires_approval is False, name
    names = {s["function"]["name"] for s in reg.llm_schemas()}
    assert {
        "get_crypto_quote",
        "get_funding_orders",
        "get_funding_order_status",
        "get_paj_banks",
        "get_paj_verification_status",
    } <= names


def test_quote_numbers_come_from_go_result_only(monkeypatch):
    from miriam_agent.tools import build_tool_registry
    from miriam_agent.tools import funding_definitions as fd  # noqa: F401

    _patch_go(monkeypatch, _FakeGo())
    reg = build_tool_registry()
    out = _run(
        reg.execute(
            "get_crypto_quote", {"side": "onramp", "amount": 50000}, {"token": "tok"}
        )
    )
    # Provenance: the exact Go figures, passed through unchanged.
    assert out["rate"] == 1530.5
    assert out["tokenAmount"] == 32.68
    assert out["fee"] == 250


def test_funding_mutation_names_never_in_registry():
    from miriam_agent.safety.money_tools import MONEY_TOOL_NAMES
    from miriam_agent.tools import build_tool_registry
    from miriam_agent.tools import funding_definitions as fd  # noqa: F401

    for name in (
        "buy_crypto",
        "sell_crypto",
        "sell_crypto_now",
        "create_onramp",
        "verify_paj_otp",
        "initiate_paj_session",
        "create_offramp",
    ):
        assert name in MONEY_TOOL_NAMES, name
    reg = build_tool_registry()
    assert sorted(set(reg.list_names()) & MONEY_TOOL_NAMES) == []


def test_tool_error_fails_open_without_token(monkeypatch):
    from miriam_agent.tools import build_tool_registry
    from miriam_agent.tools import funding_definitions as fd  # noqa: F401

    _patch_go(monkeypatch, _FakeGo())
    reg = build_tool_registry()
    out = _run(
        reg.execute(
            "get_crypto_quote", {"side": "onramp", "amount": 1000}, {"token": ""}
        )
    )
    assert out["_tool_error"] == "no user token"

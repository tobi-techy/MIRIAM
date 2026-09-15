"""Go backend client: CSRF header, real REST paths, stash transfers."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import TYPE_CHECKING

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

if TYPE_CHECKING:
    from miriam_agent.integrations.go_client import GoBackendClient


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _client_for(handler) -> GoBackendClient:
    from miriam_agent.integrations.go_client import GoBackendClient

    transport = httpx.MockTransport(handler)
    client = GoBackendClient("http://go.test")
    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "RailApp",
    }
    _run(client._client.aclose())
    client._client = httpx.AsyncClient(
        base_url="http://go.test",
        transport=transport,
        headers=headers,
    )
    return client


def test_default_headers_include_csrf_bypass():
    from miriam_agent.integrations.go_client import GoBackendClient

    client = GoBackendClient("http://go.test")
    assert client._client.headers["X-Requested-With"] == "RailApp"
    _run(client.close())


def test_get_transactions_uses_funding_path_and_unwraps_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["csrf"] = request.headers.get("X-Requested-With")
        seen["type"] = httpx.URL(str(request.url)).params.get("type")
        return httpx.Response(
            200,
            json={
                "transactions": [{"id": "tx-1", "type": "deposit", "amount": "10.00"}],
                "total": 1,
                "limit": 20,
                "offset": 0,
                "has_more": False,
            },
        )

    client = _client_for(handler)
    rows = _run(client.get_transactions("tok", limit=20, category="deposit"))
    assert seen["path"] == "/api/v1/funding/transactions"
    assert seen["csrf"] == "RailApp"
    assert seen["type"] == "deposit"
    assert rows[0]["id"] == "tx-1"
    _run(client.close())


def test_get_upcoming_bills_unwraps_data_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/financial-obligations"
        return httpx.Response(200, json={"data": [{"name": "rent", "amount": "800"}]})

    client = _client_for(handler)
    rows = _run(client.get_upcoming_bills("tok"))
    assert rows[0]["name"] == "rent"
    _run(client.close())


def test_get_user_profile_uses_users_me():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "id": "u1",
                "email": "a@test.com",
                "firstName": "Ada",
                "kycStatus": "approved",
            },
        )

    client = _client_for(handler)
    profile = _run(client.get_user_profile("tok"))
    assert seen["path"] == "/api/v1/users/me"
    assert profile["firstName"] == "Ada"
    assert profile["kycStatus"] == "approved"
    _run(client.close())


def test_send_money_matches_go_p2p_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(201, json={"status": "pending"})

    client = _client_for(handler)
    _run(
        client.send_money(
            "tok", "e2e@rail.sim", 2.5, message="hi", idempotency_key="k-p2p"
        )
    )
    assert seen["path"] == "/api/v1/p2p/send"
    assert seen["body"]["identifier"] == "e2e@rail.sim"
    assert seen["body"]["amount"] == "2.5"
    assert seen["body"]["note"] == "hi"
    assert seen["idem"] == "k-p2p"
    _run(client.close())


def test_investment_reads_use_agent_paths():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if path == "/api/v1/investments/portfolio":
            return httpx.Response(
                200, json={"total_value_usd": 100, "source": "glider"}
            )
        if path == "/api/v1/investments/positions":
            return httpx.Response(200, json={"positions": [{"symbol": "AAPL"}]})
        if path == "/api/v1/investments/assets":
            return httpx.Response(
                200,
                json={"assets": [{"asset_id": "a1", "symbol": "AAPL", "price_usd": 1}]},
            )
        if path == "/api/v1/investments/strategies":
            return httpx.Response(200, json={"strategies": [{"id": "s1"}]})
        if path == "/api/v1/investments/limits":
            return httpx.Response(200, json={"kyc_tier": "tier1"})
        if path == "/api/v1/investments/executions/e1":
            return httpx.Response(200, json={"id": "e1", "status": "filled"})
        if path == "/api/v1/investments/investors":
            return httpx.Response(200, json={"investors": [{"investor_id": "i1"}]})
        return httpx.Response(
            404, json={"code": "INVESTMENT_NOT_FOUND", "message": "no"}
        )

    client = _client_for(handler)
    portfolio = _run(client.get_investment_portfolio("tok"))
    assert portfolio["total_value_usd"] == 100
    positions = _run(client.get_investment_positions("tok"))
    assert positions["positions"][0]["symbol"] == "AAPL"
    assets = _run(client.list_investment_assets("tok", query="AAPL", limit=5))
    assert assets["assets"][0]["symbol"] == "AAPL"
    strategies = _run(client.list_investment_strategies("tok", status="active"))
    assert strategies["strategies"][0]["id"] == "s1"
    limits = _run(client.get_investment_limits("tok"))
    assert limits["kyc_tier"] == "tier1"
    execution = _run(client.get_investment_execution("tok", "e1"))
    assert execution["status"] == "filled"
    investors = _run(client.list_investment_investors("tok", collection="curated"))
    assert investors["investors"][0]["investor_id"] == "i1"
    assert "GET /api/v1/investments/portfolio" in seen
    assert not any("/investment/glider" in line for line in seen)
    _run(client.close())


def test_investment_staged_order_sends_confirmation_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            202,
            json={
                "status": "AWAITING_CONFIRMATION",
                "confirmation": {"token": "cfm-1"},
            },
        )

    client = _client_for(handler)
    first = _run(
        client.create_investment_order(
            "tok",
            {
                "symbol": "AAPL",
                "side": "buy",
                "amount_usd": 25,
                "idempotency_key": "k-inv",
            },
        )
    )
    assert seen["path"] == "/api/v1/investments/orders"
    assert seen["body"]["symbol"] == "AAPL"
    assert seen["body"]["side"] == "buy"
    assert seen["body"]["amount_usd"] == 25
    assert seen["body"]["idempotency_key"] == "k-inv"
    assert "confirmation_token" not in seen["body"]
    assert first["status"] == "AWAITING_CONFIRMATION"

    replay = _run(
        client.create_investment_order(
            "tok",
            {
                "symbol": "AAPL",
                "side": "buy",
                "amount_usd": 25,
                "idempotency_key": "k-inv",
            },
            confirmation_token="cfm-1",
        )
    )
    assert seen["body"]["confirmation_token"] == "cfm-1"
    assert replay["confirmation"]["token"] == "cfm-1"
    _run(client.close())


def test_investment_mutations_use_agent_paths():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "path": request.url.path,
                "body": json.loads(request.content) if request.content else {},
            }
        )
        return httpx.Response(202, json={"status": "AWAITING_CONFIRMATION"})

    client = _client_for(handler)
    _run(client.create_investment_strategy("tok", {"name": "Growth"}))
    _run(
        client.publish_investment_strategy_version(
            "tok", "s1", {"rationale": "rebalance"}
        )
    )
    _run(
        client.enroll_investment(
            "tok", {"strategy_id": "s1", "idempotency_key": "k-enr"}
        )
    )
    _run(
        client.set_investment_allocation(
            "tok",
            {"targets": [{"symbol": "VOO"}], "idempotency_key": "k-all"},
        )
    )
    _run(client.pause_investment_strategy("tok", "s1"))
    _run(client.resume_investment_strategy("tok", "s1"))
    _run(client.rebalance_investment_strategy("tok", "s1", reason="drift"))
    assert seen[0]["path"] == "/api/v1/investments/strategies"
    assert seen[1]["path"] == "/api/v1/investments/strategies/s1/versions"
    assert seen[2]["path"] == "/api/v1/investments/enroll"
    assert seen[2]["body"]["idempotency_key"] == "k-enr"
    assert seen[3]["path"] == "/api/v1/investments/allocations"
    assert seen[3]["body"]["targets"][0]["symbol"] == "VOO"
    assert seen[4]["path"] == "/api/v1/investments/strategies/s1/pause"
    assert seen[5]["path"] == "/api/v1/investments/strategies/s1/resume"
    assert seen[6]["path"] == "/api/v1/investments/strategies/s1/rebalance"
    assert seen[6]["body"]["reason"] == "drift"
    assert not any("/investment/glider" in s["path"] for s in seen)
    _run(client.close())


def test_investment_cooldown_surfaces_integration_error():
    from miriam_agent.core.exceptions import IntegrationError

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/investments/strategies/s1/rebalance"
        return httpx.Response(
            429,
            json={"code": "INVESTMENT_PROVIDER_COOLDOWN", "message": "try later"},
        )

    client = _client_for(handler)
    with pytest.raises(IntegrationError) as excinfo:
        _run(client.rebalance_investment_strategy("tok", "s1"))
    assert "INVESTMENT_PROVIDER_COOLDOWN" in str(excinfo.value)
    _run(client.close())


def test_agent_client_exposes_no_withdrawal_method():
    from miriam_agent.integrations.go_client import GoBackendClient

    methods = [name for name in dir(GoBackendClient) if not name.startswith("__")]
    assert not any("withdraw" in name for name in methods)


def test_stash_transfers_use_real_paths_and_idempotency_header():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(
            {
                "path": request.url.path,
                "amount": body.get("amount"),
                "idem": request.headers.get("Idempotency-Key"),
                "csrf": request.headers.get("X-Requested-With"),
            }
        )
        return httpx.Response(
            200, json={"status": "completed", "amount": body.get("amount")}
        )

    client = _client_for(handler)
    _run(client.transfer_to_stash("tok", 12.5, idempotency_key="k-from"))
    _run(client.transfer_to_spending("tok", 3, idempotency_key="k-to"))
    assert seen[0]["path"] == "/api/v1/funding/stash/from-spending"
    assert seen[0]["amount"] == "12.5"
    assert seen[0]["idem"] == "k-from"
    assert seen[0]["csrf"] == "RailApp"
    assert seen[1]["path"] == "/api/v1/funding/stash/to-spending"
    assert seen[1]["idem"] == "k-to"
    _run(client.close())


def test_lookup_and_automations_use_protected_not_ai_paths():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/v1/p2p/lookup":
            return httpx.Response(200, json={"found": True, "canSend": True})
        if request.method == "GET" and request.url.path == "/api/v1/automations":
            return httpx.Response(
                200, json={"data": [{"id": "a1", "name": "Friday stash"}]}
            )
        if request.method == "POST" and request.url.path == "/api/v1/automations":
            body = json.loads(request.content)
            assert body["action_type"] == "transfer_to_stash"
            assert body["action_config"]["amount"] == 50
            return httpx.Response(201, json={"data": {"id": "a1"}})
        if request.method == "PATCH":
            return httpx.Response(200, json={"data": {"is_active": False}})
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "deleted"})
        return httpx.Response(404, json={"error": "not found"})

    client = _client_for(handler)
    lookup = _run(client.lookup_recipient("tok", "@ada"))
    assert lookup["found"] is True
    rows = _run(client.list_automations("tok"))
    assert rows[0]["name"] == "Friday stash"
    _run(
        client.create_automation(
            "tok",
            {
                "name": "Friday stash",
                "trigger_type": "schedule",
                "trigger_config": {"weekdays": [5], "hour": 9},
                "action_type": "transfer_to_stash",
                "action_config": {"amount": 50},
            },
        )
    )
    _run(client.update_automation("tok", "a1", {"is_active": False}))
    _run(client.delete_automation("tok", "a1"))
    assert "/api/v1/ai/automations" not in "".join(seen)
    assert "POST /api/v1/automations" in seen
    _run(client.close())


def test_billpay_uses_real_paths_and_idempotency_header():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        if request.url.path == "/api/v1/billpay/pay":
            seen["body"] = json.loads(request.content)
            seen["idem"] = request.headers.get("Idempotency-Key")
            seen["csrf"] = request.headers.get("X-Requested-With")
            return httpx.Response(
                200,
                json={
                    "order_id": "ord-1",
                    "category": "airtime",
                    "recipient": "08012345678",
                    "amount_ngn": 1500,
                    "status": "submitted",
                },
            )
        if request.url.path == "/api/v1/billpay/providers":
            return httpx.Response(200, json={"providers": [{"prod_id": "ep-1"}]})
        if request.url.path == "/api/v1/billpay/detect-network":
            return httpx.Response(200, json={"network_id": "01", "network": "MTN"})
        if request.url.path == "/api/v1/billpay/validate-meter":
            return httpx.Response(
                200, json={"meter_no": "111", "account_name": "Ada L"}
            )
        if request.url.path == "/api/v1/billpay/history":
            return httpx.Response(200, json={"history": [{"order_id": "ord-1"}]})
        return httpx.Response(404, json={"error": "not found"})

    client = _client_for(handler)
    paid = _run(
        client.pay_bill(
            "tok",
            {
                "category": "airtime",
                "recipient": "08012345678",
                "amount_ngn": 1500,
                "network_id": "01",
            },
            idempotency_key="k-bill",
        )
    )
    assert seen["path"] == "/api/v1/billpay/pay"
    assert seen["body"]["category"] == "airtime"
    assert seen["body"]["recipient"] == "08012345678"
    assert seen["body"]["amount_ngn"] == 1500
    assert seen["idem"] == "k-bill"
    assert seen["csrf"] == "RailApp"
    assert paid["order_id"] == "ord-1"

    providers = _run(client.list_bill_providers("tok", "electricity"))
    assert providers[0]["prod_id"] == "ep-1"
    net = _run(client.detect_network("tok", "08012345678"))
    assert net["network_id"] == "01"
    meter = _run(client.validate_meter("tok", "111", "kaduna"))
    assert meter["account_name"] == "Ada L"
    history = _run(client.get_bill_payment_history("tok"))
    assert history[0]["order_id"] == "ord-1"
    _run(client.close())


def test_get_financial_plan_uses_snapshot_engine_not_ai_endpoint():
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/api/v1/analytics/financial-snapshot":
            return httpx.Response(
                200,
                json={
                    "period": {"from": "2026-09-01", "to": "2026-09-13"},
                    "balances": {
                        "spending_balance": "10.00",
                        "stash_balance": "5.00",
                        "total_balance": "15.00",
                    },
                    "money_flow": {
                        "total_deposits": "100.00",
                        "total_withdrawals": "40.00",
                        "total_card_spend": "10.00",
                        "total_p2p": "0.00",
                        "total_receipts": "0.00",
                        "deposit_count": 2,
                        "card_spend_count": 3,
                    },
                    "monthly_flow": [],
                    "budget": {"set": False},
                    "profile": {"has_profile": False},
                },
            )
        if request.url.path == "/api/v1/financial-obligations":
            return httpx.Response(200, json={"obligations": [{"name": "rent"}]})
        if request.url.path in {"/api/v1/balances", "/api/v1/analytics/dashboard"}:
            return httpx.Response(
                200, json={"error": "legacy path should not be called"}
            )
        return httpx.Response(404, json={"error": "not found"})

    client = _client_for(handler)
    plan = _run(client.get_financial_plan("tok"))
    assert "/api/v1/analytics/financial-snapshot" in seen_paths
    assert "/api/v1/financial-obligations" in seen_paths
    assert "/api/v1/ai/financial-plan" not in seen_paths
    assert "/api/v1/balances" not in seen_paths
    assert "/api/v1/analytics/dashboard" not in seen_paths
    assert plan["source"] == "python"
    assert plan["engine"] == "financial-snapshot"
    assert plan["next_steps"][0]["priority"] == 1
    assert plan["health"]["score"] >= 0
    health = _run(client.get_financial_health("tok"))
    assert health["source"] == "python"
    assert health["engine"] == "financial-snapshot"
    assert "/api/v1/ai/financial-health" not in seen_paths
    forecast = _run(client.get_cash_flow_forecast("tok"))
    assert forecast["engine"] == "financial-snapshot"
    assert forecast["spend_balance"] == 10.0
    _run(client.close())


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-x"])

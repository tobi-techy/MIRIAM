"""Client for the Go backend (RAIL_BACKEND).

The Go backend owns real money infrastructure: balances, transactions,
wallets, P2P transfers, investments, bills, cards. The Python agent
delegates all financial data reads and money movements to Go via REST.

This module defines the client contract and a real HTTP implementation
using httpx. All calls carry the user's JWT so Go's auth middleware
applies as-is.
"""

import logging
from typing import Any

import httpx

from miriam_agent.config.settings import get_settings
from miriam_agent.core.exceptions import IntegrationError

logger = logging.getLogger(__name__)


class GoBackendClient:
    """HTTP client for the Go backend REST API."""

    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Content-Type": "application/json",
                # Go's CSRFProtection accepts API clients that send this
                # custom header (browsers cannot set it cross-origin without
                # a CORS preflight). Harmless on GETs.
                "X-Requested-With": "RailApp",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ---- data reads (delegate to Go, which owns the ledger) ----

    async def get_balances(self, token: str) -> dict[str, Any]:
        return await self._token_get("/api/v1/balances", token)

    async def get_transactions(
        self,
        token: str,
        limit: int = 20,
        offset: int = 0,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        params = {"limit": limit, "offset": offset}
        if category:
            # Go's handler filters on `type` (deposit, withdrawal, ...), not category.
            params["type"] = category
        data = await self._token_get(
            "/api/v1/funding/transactions", token, params=params
        )
        # Go returns {transactions, total, limit, offset, has_more}.
        if isinstance(data, list):
            return data
        return data.get("transactions") or []

    async def get_spending_summary(
        self, token: str, period: str = "month"
    ) -> dict[str, Any]:
        return await self._token_get(
            "/api/v1/analytics/dashboard", token, params={"period": period}
        )

    async def get_financial_snapshot(
        self,
        token: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """One call for everything the Python intelligence engine needs.

        Go's ``/api/v1/analytics/financial-snapshot`` endpoint returns
        ledger-accurate balances, period + per-month money flow, the spending
        budget, and the user's financial profile in a single response.
        """
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        return await self._token_get(
            "/api/v1/analytics/financial-snapshot", token, params=params
        )

    async def get_financial_plan(self, token: str) -> dict[str, Any]:
        """Real financial plan from the ledger-backed financial snapshot.

        Health, cash-flow forecast, and profile-driven next steps are computed
        by the Python intelligence engine from Go's
        ``/api/v1/analytics/financial-snapshot`` response (the old
        ``/api/v1/ai/financial-plan`` endpoint is gone).
        """
        from miriam_agent.financial.intelligence import compute_financial_plan

        snapshot = await self._engine_snapshot(token)
        return compute_financial_plan(snapshot)

    async def get_cash_flow_forecast(self, token: str) -> dict[str, Any]:
        """Forecast computed from the ledger-backed financial snapshot."""
        from miriam_agent.financial.intelligence import compute_cash_flow_forecast

        snapshot = await self._engine_snapshot(token)
        return compute_cash_flow_forecast(snapshot)

    async def get_investment_positions(self, token: str) -> list[dict[str, Any]]:
        data = await self._token_get("/api/v1/investment/positions", token)
        return data.get("positions", data if isinstance(data, list) else [])

    async def get_upcoming_bills(self, token: str) -> list[dict[str, Any]]:
        data = await self._token_get("/api/v1/financial-obligations", token)
        # Go's List handler returns {data: [...]}; null data means none yet.
        if isinstance(data, list):
            return data
        items = data.get("data")
        if items is None:
            items = data.get("obligations")
        return items if isinstance(items, list) else []

    async def get_user_profile(self, token: str) -> dict[str, Any]:
        # Go's GetProfile returns entities.UserInfo at /api/v1/users/me
        # (camelCase: id, email, firstName, lastName, kycStatus, ...).
        return await self._token_get("/api/v1/users/me", token)

    async def get_financial_health(
        self, token: str, period: str = "last_90_days"
    ) -> dict[str, Any]:
        """Python-side health score from the ledger-backed financial snapshot."""
        from miriam_agent.financial.intelligence import (
            compute_financial_health,
            period_to_window,
        )

        from_date, to_date = period_to_window(period)
        snapshot = await self._engine_snapshot(token, from_date, to_date)
        return compute_financial_health(snapshot, period=period)

    async def _engine_snapshot(
        self,
        token: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Snapshot the intelligence engine needs: Go's ledger-backed
        financial snapshot plus upcoming obligations.

        Fails open: if the financial-snapshot endpoint is unreachable (e.g.
        a backend that predates it) we fall back to the legacy
        ``_money_snapshot`` shape so the engine degrades instead of raising.
        """
        try:
            snapshot = await self.get_financial_snapshot(token, from_date, to_date)
        except IntegrationError:
            logger.warning(
                "financial-snapshot unavailable; using legacy money snapshot"
            )
            snapshot = await self._money_snapshot(token)
        try:
            obligations = await self.get_upcoming_bills(token)
        except IntegrationError:
            obligations = []
        if isinstance(obligations, list):
            snapshot["upcoming_obligations"] = obligations
        return snapshot

    async def _money_snapshot(self, token: str) -> dict[str, Any]:
        balances: dict[str, Any] = {}
        spending: dict[str, Any] = {}
        obligations: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        try:
            balances = await self.get_balances(token)
        except IntegrationError:
            logger.info("snapshot: balances unavailable")
        try:
            spending = await self.get_spending_summary(token)
        except IntegrationError:
            logger.info("snapshot: spending unavailable")
        try:
            obligations = await self.get_upcoming_bills(token)
        except IntegrationError:
            logger.info("snapshot: obligations unavailable")
        try:
            positions = await self.get_investment_positions(token)
        except IntegrationError:
            logger.info("snapshot: positions unavailable")
        return {
            "balances": balances,
            "spending_summary": spending,
            "upcoming_obligations": obligations,
            "positions": positions,
        }

    # ---- money movement (delegated; Go enforces limits + auth) ----

    async def send_money(
        self,
        token: str,
        recipient: str,
        amount: float,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "identifier": recipient,
            "amount": str(amount),
            "note": message,
            "idempotencyKey": idempotency_key,
        }
        return await self._token_post(
            "/api/v1/p2p/send", token, payload, idempotency_key=idempotency_key
        )

    async def transfer_to_stash(
        self, token: str, amount: float, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return await self._token_post(
            "/api/v1/funding/stash/from-spending",
            token,
            {"amount": str(amount)},
            idempotency_key=idempotency_key,
        )

    async def transfer_to_spending(
        self, token: str, amount: float, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return await self._token_post(
            "/api/v1/funding/stash/to-spending",
            token,
            {"amount": str(amount)},
            idempotency_key=idempotency_key,
        )

    async def execute_investment(
        self,
        token: str,
        symbol: str,
        amount: float,
        side: str = "buy",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "symbol": symbol,
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "notional": str(amount),
        }
        return await self._token_post(
            "/api/v1/investment/orders",
            token,
            payload,
            idempotency_key=idempotency_key,
        )

    # ---- lookups / automations / obligations / schedules ----

    async def lookup_recipient(self, token: str, identifier: str) -> dict[str, Any]:
        return await self._token_post(
            "/api/v1/p2p/lookup", token, {"identifier": identifier}
        )

    async def list_automations(self, token: str) -> list[dict[str, Any]]:
        data = await self._token_get("/api/v1/automations", token)
        return _as_list(data, "data")

    async def create_automation(
        self,
        token: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._token_post(
            "/api/v1/automations",
            token,
            payload,
            idempotency_key=idempotency_key,
        )

    async def update_automation(
        self, token: str, automation_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._token_patch(
            f"/api/v1/automations/{automation_id}", token, payload
        )

    async def delete_automation(self, token: str, automation_id: str) -> dict[str, Any]:
        return await self._token_delete(f"/api/v1/automations/{automation_id}", token)

    async def create_obligation(
        self, token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._token_post("/api/v1/financial-obligations", token, payload)

    async def update_obligation(
        self, token: str, obligation_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._token_patch(
            f"/api/v1/financial-obligations/{obligation_id}", token, payload
        )

    async def list_scheduled_investments(self, token: str) -> list[dict[str, Any]]:
        data = await self._token_get("/api/v1/scheduled-investments", token)
        return _as_list(data, "scheduled_investments")

    async def create_scheduled_investment(
        self, token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._token_post("/api/v1/scheduled-investments", token, payload)

    async def pause_scheduled_investment(
        self, token: str, schedule_id: str
    ) -> dict[str, Any]:
        return await self._token_post(
            f"/api/v1/scheduled-investments/{schedule_id}/pause", token, {}
        )

    async def resume_scheduled_investment(
        self, token: str, schedule_id: str
    ) -> dict[str, Any]:
        return await self._token_post(
            f"/api/v1/scheduled-investments/{schedule_id}/resume", token, {}
        )

    async def list_bill_beneficiaries(
        self, token: str, category: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"category": category} if category else None
        data = await self._token_get(
            "/api/v1/billpay/beneficiaries", token, params=params
        )
        return _as_list(data, "beneficiaries")

    async def save_bill_beneficiary(
        self, token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._token_post("/api/v1/billpay/beneficiaries", token, payload)

    async def pay_bill(
        self,
        token: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._token_post(
            "/api/v1/billpay/pay",
            token,
            payload,
            idempotency_key=idempotency_key,
        )

    async def list_bill_providers(
        self, token: str, category: str, network_id: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category}
        if network_id:
            params["network_id"] = network_id
        data = await self._token_get("/api/v1/billpay/providers", token, params=params)
        return _as_list(data, "providers")

    async def get_data_plans(
        self, token: str, network_id: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"network_id": network_id} if network_id else None
        data = await self._token_get("/api/v1/billpay/data-plans", token, params=params)
        return _as_list(data, "plans")

    async def get_cable_packages(self, token: str) -> list[dict[str, Any]]:
        data = await self._token_get("/api/v1/billpay/cable-packages", token)
        return _as_list(data, "packages")

    async def detect_network(self, token: str, phone: str) -> dict[str, Any]:
        return await self._token_post(
            "/api/v1/billpay/detect-network", token, {"phone": phone}
        )

    async def validate_meter(
        self, token: str, meter_no: str, elect_id: str
    ) -> dict[str, Any]:
        return await self._token_post(
            "/api/v1/billpay/validate-meter",
            token,
            {"meter_no": meter_no, "elect_id": elect_id},
        )

    async def get_bill_payment_history(self, token: str) -> list[dict[str, Any]]:
        data = await self._token_get("/api/v1/billpay/history", token)
        return _as_list(data, "history")

    # ---- internal helpers ----

    async def _token_get(
        self,
        path: str,
        token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            resp = await self._client.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning(
                "Go backend %s %s -> %d", "GET", path, e.response.status_code
            )
            raise IntegrationError(
                f"Go backend GET {path} failed: {e.response.text[:200]}"
            )
        except httpx.HTTPError as e:
            raise IntegrationError(f"Go backend GET {path} unreachable: {e}")

    async def _token_post(
        self,
        path: str,
        token: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            resp = await self._client.post(
                path,
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise IntegrationError(
                f"Go backend POST {path} failed: {e.response.text[:200]}"
            )
        except httpx.HTTPError as e:
            raise IntegrationError(f"Go backend POST {path} unreachable: {e}")

    async def _token_patch(
        self, path: str, token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._token_mutate("PATCH", path, token, payload)

    async def _token_delete(self, path: str, token: str) -> dict[str, Any]:
        return await self._token_mutate("DELETE", path, token, None)

    async def _token_mutate(
        self,
        method: str,
        path: str,
        token: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = await self._client.request(
                method, path, json=payload, headers=headers
            )
            resp.raise_for_status()
            if resp.content:
                return resp.json()
            return {"status": "ok"}
        except httpx.HTTPStatusError as e:
            raise IntegrationError(
                f"Go backend {method} {path} failed: {e.response.text[:200]}"
            )
        except httpx.HTTPError as e:
            raise IntegrationError(f"Go backend {method} {path} unreachable: {e}")


def _as_list(data: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    items = data.get(key)
    return items if isinstance(items, list) else []


_client: GoBackendClient | None = None


def get_go_client() -> GoBackendClient:
    """Get the process-wide Go backend client singleton."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = GoBackendClient(settings.GO_BACKEND_URL)
    return _client

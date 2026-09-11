"""Client for the Go backend (RAIL_BACKEND).

The Go backend owns real money infrastructure: balances, transactions,
wallets, P2P transfers, investments, bills, cards. The Python agent
delegates all financial data reads and money movements to Go via REST.

This module defines the client contract and a real HTTP implementation
using httpx. All calls carry the user's JWT so Go's auth middleware
applies as-is.
"""

import logging
from typing import Any, Dict, List, Optional

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
            headers={"Content-Type": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ---- data reads (delegate to Go, which owns the ledger) ----

    async def get_balances(self, token: str) -> Dict[str, Any]:
        return await self._token_get("/api/v1/balances", token)

    async def get_transactions(
        self,
        token: str,
        limit: int = 20,
        offset: int = 0,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params = {"limit": limit, "offset": offset}
        if category:
            params["category"] = category
        data = await self._token_get("/api/v1/transactions", token, params=params)
        return data.get("transactions", data if isinstance(data, list) else [])

    async def get_spending_summary(
        self, token: str, period: str = "month"
    ) -> Dict[str, Any]:
        return await self._token_get(
            "/api/v1/analytics/dashboard", token, params={"period": period}
        )

    async def get_financial_plan(self, token: str) -> Dict[str, Any]:
        return await self._token_get("/api/v1/ai/financial-plan", token)

    async def get_cash_flow_forecast(self, token: str) -> Dict[str, Any]:
        return await self._token_get("/api/v1/ai/cash-flow-forecast", token)

    async def get_investment_positions(self, token: str) -> List[Dict[str, Any]]:
        data = await self._token_get("/api/v1/investment/positions", token)
        return data.get("positions", data if isinstance(data, list) else [])

    async def get_upcoming_bills(self, token: str) -> List[Dict[str, Any]]:
        data = await self._token_get("/api/v1/financial-obligations", token)
        return data.get("obligations", data if isinstance(data, list) else [])

    async def get_user_profile(self, token: str) -> Dict[str, Any]:
        return await self._token_get("/api/v1/profile", token)

    async def get_financial_health(self, token: str) -> Dict[str, Any]:
        return await self._token_get("/api/v1/ai/financial-health", token)

    # ---- money movement (delegated; Go enforces limits + auth) ----

    async def send_money(
        self,
        token: str,
        recipient: str,
        amount: float,
        message: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "to": recipient,
            "amount": amount,
            "message": message,
            "idempotency_key": idempotency_key,
        }
        return await self._token_post("/api/v1/p2p/send", token, payload)

    async def transfer_to_stash(self, token: str, amount: float) -> Dict[str, Any]:
        return await self._token_post(
            "/api/v1/ai/execute-tool", token, {"tool": "transfer_spending_to_stash", "amount": amount}
        )

    async def transfer_to_spending(self, token: str, amount: float) -> Dict[str, Any]:
        return await self._token_post(
            "/api/v1/ai/execute-tool", token, {"tool": "transfer_stash_to_spending", "amount": amount}
        )

    async def execute_investment(
        self, token: str, symbol: str, amount: float, side: str = "buy"
    ) -> Dict[str, Any]:
        payload = {"symbol": symbol, "amount": amount, "side": side}
        return await self._token_post("/api/v1/investment/orders", token, payload)

    # ---- internal helpers ----

    async def _token_get(
        self,
        path: str,
        token: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            resp = await self._client.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning("Go backend %s %s -> %d", "GET", path, e.response.status_code)
            raise IntegrationError(f"Go backend GET {path} failed: {e.response.text[:200]}")
        except httpx.HTTPError as e:
            raise IntegrationError(f"Go backend GET {path} unreachable: {e}")

    async def _token_post(
        self,
        path: str,
        token: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            resp = await self._client.post(
                path,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise IntegrationError(f"Go backend POST {path} failed: {e.response.text[:200]}")
        except httpx.HTTPError as e:
            raise IntegrationError(f"Go backend POST {path} unreachable: {e}")


_client: Optional[GoBackendClient] = None


def get_go_client() -> GoBackendClient:
    """Get the process-wide Go backend client singleton."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = GoBackendClient(settings.GO_BACKEND_URL)
    return _client
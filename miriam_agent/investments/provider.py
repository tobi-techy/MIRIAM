"""Investment provider adapter (spec §16): Miriam stays the intelligence
layer, the provider executes.

``InvestmentProvider`` is the narrow surface the conversation needs. It has
exactly two implementations:

  - :class:`GliderInvestmentProvider` -- a thin adapter over the **existing**
    ``GoBackendClient`` investment methods. No endpoints are invented here;
    every call maps 1:1 onto a route Go already serves, and the staged
    confirmation token flows straight through.
  - :class:`MockInvestmentProvider` -- an in-memory implementation for tests,
    local development and dry runs, so the conversation can be exercised
    without a live market.

Glider has **no cancel endpoint**: an order that has reached the provider
cannot be recalled from Python. That is stated as a capability
(``supports_cancel = False``) and enforced by raising, rather than faked with
a pretend success -- a fake cancel would be the worst kind of lie, the kind a
user acts on.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from miriam_agent.core.exceptions import IntegrationError

logger = logging.getLogger(__name__)


class UnsupportedOperationError(IntegrationError):
    """The provider cannot do this, by design -- not a transient failure."""


class InvestmentProvider(ABC):
    """The execution surface Miriam is allowed to talk to (spec §16)."""

    name: str = "abstract"
    supports_cancel: bool = False

    @abstractmethod
    async def create_portfolio(
        self, token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Create (or return) the user's investment portfolio."""

    @abstractmethod
    async def get_portfolio(self, token: str) -> dict[str, Any]:
        """The user's portfolio as the provider sees it."""

    @abstractmethod
    async def get_assets(
        self,
        token: str,
        query: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """The assets available to this user."""

    @abstractmethod
    async def get_asset(
        self,
        token: str,
        asset_id: str,
        *,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """One asset's detail (used for the tradability check)."""

    @abstractmethod
    async def create_order(
        self,
        token: str,
        payload: dict[str, Any],
        *,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Place an order. Staged: returns AWAITING_CONFIRMATION until the
        provider-issued ``confirmation_token`` is replayed."""

    @abstractmethod
    async def get_order(self, token: str, order_id: str) -> dict[str, Any]:
        """One order's current status."""

    @abstractmethod
    async def get_positions(self, token: str) -> dict[str, Any]:
        """The user's open positions."""

    @abstractmethod
    async def get_balance(self, token: str) -> dict[str, Any]:
        """The balances an order would draw on."""

    async def cancel_order(self, token: str, order_id: str) -> dict[str, Any]:
        """Cancel an order. Only available when ``supports_cancel`` is True."""
        raise UnsupportedOperationError(
            f"{self.name} does not support cancelling orders"
        )


class GliderInvestmentProvider(InvestmentProvider):
    """Glider, reached through the existing Go backend client (spec §16, §31).

    Miriam never talks to Glider directly: Go owns authentication, identity and
    the user's account, so the user's own JWT is forwarded and Go's middleware
    applies unchanged.
    """

    name = "glider"
    supports_cancel = False

    def __init__(self, client: Any | None = None):
        if client is None:
            from miriam_agent.integrations.go_client import get_go_client

            client = get_go_client()
        self._client = client

    async def create_portfolio(
        self, token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._client.create_investment_strategy(token, payload)

    async def get_portfolio(self, token: str) -> dict[str, Any]:
        return await self._client.get_investment_portfolio(token)

    async def get_assets(
        self,
        token: str,
        query: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return await self._client.list_investment_assets(
            token, query=query, limit=limit
        )

    async def get_asset(
        self,
        token: str,
        asset_id: str,
        *,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.get_investment_asset(token, asset_id, symbol=symbol)

    async def create_order(
        self,
        token: str,
        payload: dict[str, Any],
        *,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.create_investment_order(
            token, payload, confirmation_token=confirmation_token
        )

    async def get_order(self, token: str, order_id: str) -> dict[str, Any]:
        return await self._client.get_investment_execution(token, order_id)

    async def get_positions(self, token: str) -> dict[str, Any]:
        return await self._client.get_investment_positions(token)

    async def get_balance(self, token: str) -> dict[str, Any]:
        return await self._client.get_balances(token)


class MockInvestmentProvider(InvestmentProvider):
    """In-memory provider for tests and dry runs (spec §16).

    It behaves like the real staged flow: an order returns AWAITING_CONFIRMATION
    with a token, and replaying that same token settles it. Every mutation is
    recorded, so a test can assert on exactly what the conversation asked the
    market to do.
    """

    name = "mock"
    supports_cancel = True

    def __init__(self, *, balances: dict[str, float] | None = None):
        self.balances = balances or {"spending": 100_000.0, "stash": 50_000.0}
        self.assets: list[dict[str, Any]] = [
            {
                "asset_id": "ast_voo",
                "symbol": "VOO",
                "name": "Vanguard S&P 500",
                "tradable": True,
            },
            {
                "asset_id": "ast_vti",
                "symbol": "VTI",
                "name": "Vanguard Total Market",
                "tradable": True,
            },
        ]
        self.portfolios: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_next: Exception | None = None

    def _guard(self) -> None:
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error

    async def create_portfolio(
        self, token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._guard()
        self.calls.append(("create_portfolio", payload))
        portfolio = {
            "id": f"pf_{len(self.portfolios) + 1}",
            "status": "ACTIVE",
            "allocations": payload.get("targets") or [],
        }
        self.portfolios[token] = portfolio
        return portfolio

    async def get_portfolio(self, token: str) -> dict[str, Any]:
        self.calls.append(("get_portfolio", {}))
        return self.portfolios.get(token, {"id": "", "status": "EMPTY"})

    async def get_assets(
        self,
        token: str,
        query: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("get_assets", {"query": query, "limit": limit}))
        items = [
            a
            for a in self.assets
            if not query or query.casefold() in a["symbol"].casefold()
        ]
        return {"assets": items[: limit or len(items)]}

    async def get_asset(
        self, token: str, asset_id: str, *, symbol: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("get_asset", {"asset_id": asset_id, "symbol": symbol}))
        for asset in self.assets:
            if asset["asset_id"] == asset_id or (symbol and asset["symbol"] == symbol):
                return asset
        raise IntegrationError(f"unknown asset {asset_id or symbol}")

    async def create_order(
        self,
        token: str,
        payload: dict[str, Any],
        *,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        self._guard()
        self.calls.append(("create_order", dict(payload)))
        order_id = str(payload.get("idempotency_key") or f"ord_{len(self.orders) + 1}")
        if confirmation_token:
            existing = self.orders.get(order_id)
            if existing and existing["confirmation_token"] == confirmation_token:
                existing["status"] = "SETTLED"
                existing["filled_amount"] = existing["amount"]
                return existing
            raise IntegrationError("confirmation token does not match the order")
        order = {
            "id": order_id,
            "status": "AWAITING_CONFIRMATION",
            "amount": payload.get("amount_usd") or payload.get("amount"),
            "symbol": payload.get("symbol"),
            "confirmation_token": f"conf_{order_id}",
        }
        self.orders[order_id] = order
        return dict(order)

    async def get_order(self, token: str, order_id: str) -> dict[str, Any]:
        self.calls.append(("get_order", {"order_id": order_id}))
        order = self.orders.get(order_id)
        if order is None:
            raise IntegrationError(f"unknown order {order_id}")
        return dict(order)

    async def get_positions(self, token: str) -> dict[str, Any]:
        self.calls.append(("get_positions", {}))
        settled = [
            {"symbol": o["symbol"], "amount": o["filled_amount"]}
            for o in self.orders.values()
            if o["status"] == "SETTLED"
        ]
        return {"positions": settled}

    async def get_balance(self, token: str) -> dict[str, Any]:
        self.calls.append(("get_balance", {}))
        return {"balances": dict(self.balances)}

    async def cancel_order(self, token: str, order_id: str) -> dict[str, Any]:
        self.calls.append(("cancel_order", {"order_id": order_id}))
        order = self.orders.get(order_id)
        if order is None:
            raise IntegrationError(f"unknown order {order_id}")
        order["status"] = "CANCELLED"
        return dict(order)
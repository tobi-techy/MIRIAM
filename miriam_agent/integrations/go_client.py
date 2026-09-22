"""Client for the Go backend (RAIL_BACKEND).

The Go backend owns real money infrastructure: balances, transactions,
wallets, P2P transfers, investments, bills, cards. The Python agent
delegates all financial data reads and money movements to Go via REST.

This module defines the client contract and a real HTTP implementation
using httpx. All calls carry the user's JWT so Go's auth middleware
applies as-is.
"""

import asyncio
import logging
from typing import Any

import httpx

from miriam_agent.config.settings import get_settings
from miriam_agent.core.exceptions import IntegrationError

logger = logging.getLogger(__name__)

# Statuses worth retrying: the request never reached the business logic, or
# the backend was shedding load.
_RETRYABLE_STATUS = {429, 502, 503, 504}


def _assert_safe_path(path: str) -> None:
    """Reject a constructed backend path that could escape its route.

    Tool arguments are interpolated into several Go paths, so a value that
    slipped past schema validation must not be able to traverse out of its
    prefix (``/assets/..``) or smuggle a new request.
    """
    if ".." in path or "//" in path or "\n" in path or "\r" in path:
        raise IntegrationError(f"Refusing to call a malformed backend path: {path!r}")


class GoBackendClient:
    """HTTP client for the Go backend REST API."""

    def __init__(
        self,
        base_url: str,
        timeout: float | None = None,
        max_retries: int | None = None,
    ):
        settings = get_settings()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout if timeout is not None else settings.GO_REQUEST_TIMEOUT
        self.max_retries = (
            max_retries if max_retries is not None else settings.GO_MAX_RETRIES
        )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
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
        params: dict[str, Any] = {"limit": limit, "offset": offset}
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

    async def get_investment_positions(self, token: str) -> dict[str, Any]:
        # Agent API positions read; returns {"positions": [...]} (raw body).
        return await self._token_get("/api/v1/investments/positions", token)

    async def get_upcoming_bills(self, token: str) -> list[dict[str, Any]]:
        data = await self._token_get("/api/v1/financial-obligations", token)
        # Go's List handler returns {data: [...]}; null data means none yet.
        if isinstance(data, list):
            return data
        items = data.get("data")
        if items is None:
            items = data.get("obligations")
        return items if isinstance(items, list) else []

    async def get_ngn_virtual_account(self, token: str) -> dict[str, Any]:
        """The user's Naira bank-transfer account (Graph NGN named account).

        Go answers 404 when no NGN account exists yet; that is an empty
        result, not a failure, so it comes back as a structured
        ``_tool_error`` payload the agent can explain honestly instead of an
        exception that looks like the capability is missing.
        """
        try:
            data = await self._token_get("/api/v1/funding/ngn/virtual-account", token)
        except IntegrationError as e:
            message = str(e)
            if "404" in message or "not_found" in message:
                return {
                    "_tool_error": (
                        "no Naira deposit account exists for this user yet"
                    ),
                    "hint": (
                        "Ask the user to complete Naira account setup in the "
                        "Rail app, or fund with crypto instead."
                    ),
                }
            raise
        if isinstance(data, dict):
            account: Any = data.get("virtual_account")
            if account is None and isinstance(data.get("data"), dict):
                account = data["data"].get("virtual_account")
            if isinstance(account, dict) and account:
                return {"virtual_account": account, "raw": data}
            if data.get("_tool_error"):
                return data
            return {
                "_tool_error": "naira deposit account response had no account",
                "raw": data,
            }
        return {"_tool_error": "unexpected naira account response shape"}

    async def create_deposit_address(
        self,
        token: str,
        chain: str = "base",
        currency: str = "USDC",
    ) -> dict[str, Any]:
        """A crypto deposit address for the user (fallback funding rail)."""
        try:
            data = await self._token_post(
                "/api/v1/funding/deposit/address",
                token,
                {"chain": chain, "currency": currency},
            )
        except IntegrationError as e:
            return {"_tool_error": str(e)}
        if isinstance(data, dict):
            address: Any = data.get("address") or data.get("deposit_address")
            if address is None and isinstance(data.get("data"), dict):
                address = data["data"].get("address")
            if address:
                out = dict(data)
                out.setdefault("address", address)
                return out
            if data.get("_tool_error"):
                return data
            return {
                "_tool_error": "deposit address response had no address",
                "raw": data,
            }
        return {"_tool_error": "unexpected deposit address response shape"}

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
        snapshot["upcoming_obligations"] = obligations
        return snapshot

    async def _money_snapshot(self, token: str) -> dict[str, Any]:
        balances: dict[str, Any] = {}
        spending: dict[str, Any] = {}
        obligations: list[dict[str, Any]] = []
        positions: dict[str, Any] = {}
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

    # ---- investments (Agent API reads: /api/v1/investments/*) ----

    async def get_investment_limits(self, token: str) -> dict[str, Any]:
        return await self._token_get("/api/v1/investments/limits", token)

    async def get_investment_portfolio(self, token: str) -> dict[str, Any]:
        return await self._token_get("/api/v1/investments/portfolio", token)

    async def list_investment_assets(
        self, token: str, query: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if query:
            params["query"] = query
        if limit is not None:
            params["limit"] = limit
        return await self._token_get(
            "/api/v1/investments/assets", token, params=params or None
        )

    async def get_investment_asset(
        self,
        token: str,
        asset_id: str,
        caip19: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if caip19:
            params["caip19"] = caip19
        if symbol:
            params["symbol"] = symbol
        return await self._token_get(
            f"/api/v1/investments/assets/{asset_id}", token, params=params or None
        )

    async def list_investment_strategies(
        self, token: str, status: str | None = None
    ) -> dict[str, Any]:
        params = {"status": status} if status else None
        return await self._token_get(
            "/api/v1/investments/strategies", token, params=params
        )

    async def get_investment_strategy(
        self, token: str, strategy_id: str
    ) -> dict[str, Any]:
        return await self._token_get(
            f"/api/v1/investments/strategies/{strategy_id}", token
        )

    async def preview_investment_strategy(
        self, token: str, strategy_id: str, amount_usd: float | None = None
    ) -> dict[str, Any]:
        params = {"amount_usd": amount_usd} if amount_usd is not None else None
        return await self._token_get(
            f"/api/v1/investments/strategies/{strategy_id}/preview",
            token,
            params=params,
        )

    async def list_investment_executions(
        self, token: str, status: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit
        return await self._token_get(
            "/api/v1/investments/executions", token, params=params or None
        )

    async def get_investment_execution(
        self, token: str, execution_id: str
    ) -> dict[str, Any]:
        return await self._token_get(
            f"/api/v1/investments/executions/{execution_id}", token
        )

    async def list_investment_audit_events(
        self, token: str, limit: int | None = None
    ) -> dict[str, Any]:
        params = {"limit": limit} if limit is not None else None
        return await self._token_get("/api/v1/investments/audit", token, params=params)

    async def list_investment_investors(
        self,
        token: str,
        collection: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if collection:
            params["collection"] = collection
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        return await self._token_get(
            "/api/v1/investments/investors", token, params=params or None
        )

    async def get_investment_investor(
        self, token: str, investor_id: str
    ) -> dict[str, Any]:
        return await self._token_get(
            f"/api/v1/investments/investors/{investor_id}", token
        )

    async def get_investment_investor_activity(
        self, token: str, investor_id: str
    ) -> dict[str, Any]:
        return await self._token_get(
            f"/api/v1/investments/investors/{investor_id}/activity", token
        )

    async def get_investment_owner(self, token: str) -> dict[str, Any]:
        """The caller's Solana owner account id (CAIP-10) for user-signed enroll.

        Read-only. An account without a Solana wallet gets an explicit error,
        never an invented address.
        """
        return await self._token_get("/api/v1/investments/owner", token)

    # ---- investments (user-signed Glider enroll, via the Go host) ----
    #
    # The only investment writes Miriam makes. Stage 1 returns the base64
    # Solana transaction the wallet signs; stage 2 submits it (idempotent on
    # flowId). Both are driven from the hands layer after a confirm_id tap,
    # never from a chat-turn tool.

    async def prepare_user_enroll(
        self,
        token: str,
        payload: dict[str, Any],
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Glider stage 1 for a user-held Solana wallet (Model B).

        Returns the base64 Solana transaction to sign plus the confirmation
        the allocate card binds to. Fail-closed: a simulated backend answers
        with an explicit not-live error and no sign payload.
        """
        return await self._token_post(
            "/api/v1/investments/enroll/prepare",
            token,
            _with_confirmation(payload, confirmation_token),
        )

    async def complete_user_enroll(
        self,
        token: str,
        payload: dict[str, Any],
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Glider stage 2: submit the wallet-signed transaction.

        Idempotent on flowId. The confirmation token binds flowId + amount +
        strategyId; mutated payloads are rejected by the backend.
        """
        return await self._token_post(
            "/api/v1/investments/enroll/complete",
            token,
            _with_confirmation(payload, confirmation_token),
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

    async def get_p2p_transfers(
        self, token: str, *, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Recent P2P transfers, newest first, with their current status."""
        data = await self._token_get(
            "/api/v1/p2p/transfers",
            token,
            params={"limit": limit, "offset": offset},
        )
        return _as_list(data, "transfers")

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

    # ---- documents (read-only contract v1; Go owns lifecycle/storage) ----

    async def get_document_result(self, token: str, document_id: str):
        """Fetch the versioned document result for the authenticated user.

        Uses this client's shared httpx session (base URL, timeout, CSRF
        header) plus the caller's user JWT — Go enforces ownership.
        """
        from miriam_agent.documents.client import fetch_document_result

        return await fetch_document_result(self._client, token, document_id)

    # ---- internal helpers ----

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Perform an authenticated Go call, with bounded retries.

        Retries are allowed for reads always, and for writes only when they
        carry an idempotency key -- the Go ledger dedupes on that key, while
        repeating an unkeyed write could move money twice. Previously nothing
        was retried at all, so a transient blip surfaced to the user as a hard
        failure.
        """
        _assert_safe_path(path)
        retryable = method.upper() == "GET" or idempotency_key is not None
        attempts = self.max_retries if retryable else 0
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        last_error: Exception | None = None
        for attempt in range(attempts + 1):
            try:
                resp = await self._client.request(
                    method, path, params=params, json=payload, headers=headers
                )
            except httpx.HTTPError as e:
                last_error = e
                if attempt < attempts:
                    await asyncio.sleep(0.3 * (2**attempt))
                    continue
                logger.warning("Go backend %s %s unreachable: %s", method, path, e)
                raise IntegrationError(
                    f"Go backend {method} {path} unreachable: {e}"
                ) from e

            if resp.status_code in _RETRYABLE_STATUS and attempt < attempts:
                await asyncio.sleep(0.3 * (2**attempt))
                continue

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.warning("Go backend %s %s -> %d", method, path, resp.status_code)
                raise IntegrationError(
                    f"Go backend {method} {path} failed: {resp.text[:200]}"
                ) from e

            if resp.content:
                return resp.json()
            return {"status": "ok"}

        raise IntegrationError(f"Go backend {method} {path} failed: {last_error}")

    async def _token_get(
        self,
        path: str,
        token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request_json("GET", path, token=token, params=params)

    async def _token_post(
        self,
        path: str,
        token: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            path,
            token=token,
            payload=payload,
            idempotency_key=idempotency_key,
        )

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
        return await self._request_json(method, path, token=token, payload=payload)


def _with_confirmation(
    payload: dict[str, Any], confirmation_token: str | None
) -> dict[str, Any]:
    """Merge a staged-action confirmation token into a request body.

    The Go investment API accepts the token either as the ``confirmation_token``
    body field or the ``X-Investment-Confirmation`` header; the user-signed
    enroll endpoints use the body field. The token must accompany the exact
    same payload it was issued for.
    """
    if not confirmation_token:
        return payload
    return {**payload, "confirmation_token": confirmation_token}


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

"""Go backend client mixin: the investment / Glider endpoint family.

Split verbatim out of :mod:`miriam_agent.integrations.go_client` (blob-ratchet):
the ``/api/v1/investments/*`` reads and Glider writes, the path guard
``_assert_safe_path`` they use, and the two payload helpers
(``investment_strategy_id``, ``_with_confirmation``). Mixed into
``GoBackendClient``; the runtime surface is unchanged, and ``go_client``
re-imports every moved name so existing import paths keep working.
"""

from __future__ import annotations

import logging
from typing import Any

from miriam_agent.core.exceptions import IntegrationError

logger = logging.getLogger(__name__)


def _assert_safe_path(path: str) -> None:
    """Reject a constructed backend path that could escape its route.

    Tool arguments are interpolated into several Go paths, so a value that
    slipped past schema validation must not be able to traverse out of its
    prefix (``/assets/..``) or smuggle a new request.
    """
    if ".." in path or "//" in path or "\n" in path or "\r" in path:
        raise IntegrationError(f"Refusing to call a malformed backend path: {path!r}")


def investment_strategy_id(row: dict[str, Any]) -> str:
    """Rail strategy id as the Go API actually serializes it.

    The live entity tag is ``strategy_id``. Older fixtures and a few handlers
    used ``id``. Either is accepted; an empty string means the row is unbound.
    """
    if not isinstance(row, dict):
        return ""
    return str(row.get("strategy_id") or row.get("id") or "").strip()


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


class GoInvestmentsMixin:
    """Investment / Glider endpoints for :class:`GoBackendClient`.

    The host class supplies the token-aware HTTP verbs declared below; each is
    an abstract stub here so the type gate checks the mixin against the
    contract instead of ``Any``.
    """

    async def _token_get(
        self,
        path: str,
        token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def _token_post(
        self,
        path: str,
        token: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        no_retry: bool = False,
    ) -> dict[str, Any]: ...

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

    async def list_rail_investment_strategies(self, token: str) -> dict[str, Any]:
        """Rail-owned strategies, including the seeded stock sleeve.

        These have no owning user, so they never appear on
        ``GET /investments/strategies``. The invest path has to read this
        list or the sleeve looks missing even when it is seeded.
        """
        return await self._token_get("/api/v1/investments/strategies/rail", token)

    async def list_investable_strategies(
        self, token: str, status: str | None = None
    ) -> dict[str, Any]:
        """User strategies plus Rail strategies, de-duplicated by id.

        Partial-tolerant: if one source is down, the other still serves so a
        Rail outage cannot block a user-owned sleeve (and vice versa). The
        ``partial`` flag tells callers the merge is incomplete. Both failing
        still raises, so the invest path refuses loudly instead of inventing.
        """
        user: dict[str, Any] | None = None
        rail: dict[str, Any] | None = None
        user_error: Exception | None = None
        rail_error: Exception | None = None
        try:
            user = await self.list_investment_strategies(token, status=status)
        except Exception as exc:  # noqa: BLE001 - merged below
            user_error = exc
        try:
            rail = await self.list_rail_investment_strategies(token)
        except Exception as exc:  # noqa: BLE001 - merged below
            rail_error = exc
        if user is None and rail is None:
            raise IntegrationError(
                f"strategy catalogue unreadable (user: {user_error}; "
                f"rail: {rail_error})"
            )
        if user_error is not None or rail_error is not None:
            logger.warning(
                "invest catalogue partial (user_error=%s rail_error=%s)",
                user_error,
                rail_error,
            )
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for bucket in (user, rail):
            if not isinstance(bucket, dict):
                continue
            rows = bucket.get("strategies")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                sid = investment_strategy_id(row)
                if sid and sid in seen:
                    continue
                if sid:
                    seen.add(sid)
                merged.append(row)
        out: dict[str, Any] = {"strategies": merged}
        if user_error is not None or rail_error is not None:
            out["partial"] = True
            if user_error is not None:
                out["user_error"] = str(user_error)
            if rail_error is not None:
                out["rail_error"] = str(rail_error)
        return out

    async def contribute_to_investment(
        self,
        token: str,
        payload: dict[str, Any],
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Add USDC to a portfolio the user is already enrolled in.

        POST /api/v1/investments/contributions. Staged like enroll: the first
        call returns a confirmation token, the replay moves the money. No
        new wallet signature.
        """
        return await self._token_post(
            "/api/v1/investments/contributions",
            token,
            _with_confirmation(payload, confirmation_token),
            idempotency_key=payload.get("idempotency_key"),
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

    # ---- Glider transactions: orders, allocations, rebalance, pause/resume ----
    # Server-signed staging: these move allocations on rebalance. No wallet
    # signature is needed because the allocation shift is Rail-signed server-side.
    # Staged mutations (orders, allocations) return AWAITING_CONFIRMATION +
    # confirmation.token, then replay identical JSON + token via _obtain_and_replay.

    async def create_investment_order(
        self,
        token: str,
        payload: dict[str, Any],
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Place a buy/sell order into the Rail Stock Sleeve.

        POST /api/v1/investments/orders. Without a confirmation_token the Go
        backend stages the order and returns AWAITING_CONFIRMATION + token; with
        the token it replays the byte-identical payload and completes, returning
        the execution id and operation id. An "order" is an allocation shift
        converged on Glider rebalance (no price guarantee).
        """
        return await self._token_post(
            "/api/v1/investments/orders",
            token,
            _with_confirmation(payload, confirmation_token),
            idempotency_key=payload.get("idempotency_key"),
        )

    async def set_investment_allocation(
        self,
        token: str,
        payload: dict[str, Any],
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Set target allocation legs for the Rail Stock Sleeve.

        POST /api/v1/investments/allocations. Staged (no token) then completed
        (with token) via _obtain_and_replay, same pattern as /orders.
        """
        return await self._token_post(
            "/api/v1/investments/allocations",
            token,
            _with_confirmation(payload, confirmation_token),
            idempotency_key=payload.get("idempotency_key"),
        )

    async def rebalance_investment_strategy(
        self,
        token: str,
        strategy_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Rebalance a strategy immediately.

        POST /api/v1/investments/strategies/{id}/rebalance. No staging: the
        rebalance executes now and returns {execution, operation_id}. A 429
        means the strategy is cooling down — surfaced as CoolingDownError so
        the caller can say "try again in X". no_retry disables the retry loop.
        """
        _assert_safe_path(strategy_id)
        body: dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        return await self._token_post(
            f"/api/v1/investments/strategies/{strategy_id}/rebalance",
            token,
            body,
            no_retry=True,
        )

    async def pause_investment_strategy(
        self, token: str, strategy_id: str
    ) -> dict[str, Any]:
        """Pause a strategy immediately (no staging)."""
        _assert_safe_path(strategy_id)
        return await self._token_post(
            f"/api/v1/investments/strategies/{strategy_id}/pause",
            token,
            {},
            no_retry=True,
        )

    async def resume_investment_strategy(
        self, token: str, strategy_id: str
    ) -> dict[str, Any]:
        """Resume a paused strategy immediately (no staging)."""
        _assert_safe_path(strategy_id)
        return await self._token_post(
            f"/api/v1/investments/strategies/{strategy_id}/resume",
            token,
            {},
            no_retry=True,
        )

    # ---- reads: strategy sub-resources ----

    async def get_investment_strategy_provider_versions(
        self,
        token: str,
        strategy_id: str,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/investments/strategies/{id}/provider-versions.

        Live Glider history. Each version has isHead=true when active.
        """
        _assert_safe_path(strategy_id)
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        return await self._token_get(
            f"/api/v1/investments/strategies/{strategy_id}/provider-versions",
            token,
            params=params or None,
        )

    async def get_investment_strategy_performance(
        self, token: str, strategy_id: str
    ) -> dict[str, Any]:
        """GET /api/v1/investments/strategies/{id}/performance.

        Template TWR curve. This is the strategy's own curve, not the user's
        money-in curve.
        """
        _assert_safe_path(strategy_id)
        return await self._token_get(
            f"/api/v1/investments/strategies/{strategy_id}/performance",
            token,
        )

    async def get_investment_strategy_schedule(
        self, token: str, strategy_id: str
    ) -> dict[str, Any]:
        """GET /api/v1/investments/strategies/{id}/schedule.

        Configured cadence. Runtime nextDueAt lives on the portfolio.
        """
        _assert_safe_path(strategy_id)
        return await self._token_get(
            f"/api/v1/investments/strategies/{strategy_id}/schedule",
            token,
        )

    async def get_investment_strategy_preferences(
        self, token: str, strategy_id: str
    ) -> dict[str, Any]:
        """GET /api/v1/investments/strategies/{id}/preferences.

        Swap settings: {swap:{slippageBps, priceImpactBps, thresholdUsd}}.
        null means the provider default applies.
        """
        _assert_safe_path(strategy_id)
        return await self._token_get(
            f"/api/v1/investments/strategies/{strategy_id}/preferences",
            token,
        )

    async def get_investment_strategy_fees(
        self, token: str, strategy_id: str
    ) -> dict[str, Any]:
        """GET /api/v1/investments/strategies/{id}/fees.

        Provider fees: {swapBps}. Not an estimate.
        """
        _assert_safe_path(strategy_id)
        return await self._token_get(
            f"/api/v1/investments/strategies/{strategy_id}/fees",
            token,
        )

    # ---- reads: enrollment sub-resources ----

    async def get_investment_enrollment_performance(
        self,
        token: str,
        enrollment_id: str,
        return_method: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/investments/enrollments/{id}/performance.

        The user's money curve. returnMethod=MWR (default) is money-weighted
        return; TWR is time-weighted return.
        """
        _assert_safe_path(enrollment_id)
        params: dict[str, Any] = {}
        if return_method:
            params["returnMethod"] = return_method
        return await self._token_get(
            f"/api/v1/investments/enrollments/{enrollment_id}/performance",
            token,
            params=params or None,
        )

    async def get_investment_enrollment_sector_exposure(
        self, token: str, enrollment_id: str
    ) -> dict[str, Any]:
        """GET /api/v1/investments/enrollments/{id}/sector-exposure.

        Sector exposure rows + taxonomy for the user's sleeve holdings.
        """
        _assert_safe_path(enrollment_id)
        return await self._token_get(
            f"/api/v1/investments/enrollments/{enrollment_id}/sector-exposure",
            token,
        )

    async def breakdown_investment_holdings(
        self, token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """POST /api/v1/investments/breakdown.

        Break holdings into Glider buckets across dimensions. multiCategoryMode
        controls how assets in multiple categories are counted.
        """
        return await self._token_post(
            "/api/v1/investments/breakdown",
            token,
            payload,
        )

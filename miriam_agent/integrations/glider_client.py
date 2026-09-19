"""Glider B2B API client (v2), for reads and strategy validation.

Reference: https://docs.glider.fi/api-reference/v2-overview

This is the wire. The *decisions* (which book, allowed or not, drift) live in
``money/glider.py``; this module only speaks HTTP, which is why it sits in
``integrations/`` with the other outbound adapters rather than in the domain
layer (``docs/ARCHITECTURE-CONTRACT.md``).

Three properties are enforced here rather than trusted to callers:

  - **An asset id is never guessed.** ``require_asset_id`` rejects anything that
    is not CAIP-19 shaped, and ``discover_assets`` resolves ids from a real
    source (the user's own live positions). There is no code path that
    constructs an id from a symbol.
  - **Enrollment is never committed.** ``prepare_enrollment`` runs stage 1, which
    only *prepares* a wallet authorization. Stage 2 (``POST /v2/enroll``) is
    deliberately not implemented: it requires the user's own signature, and
    auto-enrolling someone is forbidden. ``enrollment_intent`` returns what the
    user must sign instead.
  - **Async work is polled, not assumed.** A dispatched rebalance or withdrawal
    returns an ``operationId``; ``await_operation`` polls to a terminal state.
    Nothing here reports a settled result it did not observe.

Authentication is an ``x-api-key`` header. Responses use the
``{success, data}`` envelope, with ``nextCursor`` beside ``data`` on paginated
routes and ``{success: false, error: {code, message, details}}`` on failure.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any

import httpx

from miriam_agent.config.settings import get_settings
from miriam_agent.core.exceptions import ConfigurationError, IntegrationError
from miriam_agent.money.schema import GliderDraft
from miriam_agent.money.templates import validate_weights

logger = logging.getLogger(__name__)

# Statuses worth retrying: the request never reached the business logic, or the
# API was shedding load.
_RETRYABLE_STATUS = {429, 502, 503, 504}

# Operations that have stopped moving. Polling past one of these is waste.
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})

# Glider caps an allocation at 50 assets and requires weights to sum to 100.
_MAX_ALLOCATION_ASSETS = 50


def _assert_safe_id(value: str, label: str) -> str:
    """Reject an identifier that could escape its route.

    Ids are interpolated into paths, so a value carrying ``..``, a slash, or a
    newline could traverse out of its prefix or smuggle a second request.
    """
    text = str(value or "")
    if not text or any(bad in text for bad in ("..", "/", "\\", "\n", "\r")):
        raise IntegrationError(f"Refusing to use a malformed {label}: {text!r}")
    return text


def is_caip19(value: str) -> bool:
    """Whether a string looks like a CAIP-19 asset id.

    Shape check only -- ``eip155:8453/erc20:0x...`` or ``solana:<ref>/spl:<mint>``.
    Being *recognized* by Glider is a separate question that only
    ``POST /v2/strategies/validate`` can answer.
    """
    text = str(value or "")
    return ":" in text and "/" in text and " " not in text


def require_asset_id(value: str) -> str:
    """Return ``value`` if it is a plausible CAIP-19 id, else raise.

    The point is that there is no fallback. A wrong-but-plausible id is worse
    than an error, because it is a real token someone else controls.
    """
    if not is_caip19(value):
        raise IntegrationError(
            f"{value!r} is not a CAIP-19 asset id. Asset ids are resolved from a "
            "real source (live positions or a validated map), never constructed "
            "from a symbol."
        )
    return str(value)


class GliderClient:
    """HTTP client for the Glider B2B API v2."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        offline: bool | None = None,
    ):
        settings = get_settings()
        self.base_url = (base_url or settings.GLIDER_API_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.GLIDER_API_KEY
        self.timeout = (
            timeout if timeout is not None else settings.GLIDER_REQUEST_TIMEOUT
        )
        self.max_retries = (
            max_retries if max_retries is not None else settings.GLIDER_MAX_RETRIES
        )
        self.poll_seconds = settings.GLIDER_OPERATION_POLL_SECONDS
        self.max_polls = settings.GLIDER_OPERATION_MAX_POLLS
        # Offline is the default whenever there is no key: a draft still gets
        # built and inspected, it just is never submitted. That keeps tests and a
        # key-less dev environment on the same code path as production.
        self._offline = (not self.api_key) if offline is None else offline
        # ``transport`` is the testability seam required by the architecture
        # contract: every external provider must be constructible with a fake
        # transport so tests never reach a live API.
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={"Content-Type": "application/json", "x-api-key": self.api_key},
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def online(self) -> bool:
        """Whether a real Glider call is possible right now."""
        return bool(self.api_key) and not self._offline

    # ---- identity ------------------------------------------------------

    async def whoami(self) -> dict[str, Any]:
        """The tenant identity and assigned scopes. The first call to make."""
        return await self._get("/whoami")

    async def list_scopes(self) -> dict[str, Any]:
        """The scope catalogue. The one route that needs no API key."""
        return await self._request("GET", "/scopes", authenticated=False)

    # ---- strategies ----------------------------------------------------

    async def list_strategies(
        self,
        status: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        data, next_cursor = await self._get_page("/strategies", params)
        return self._collection(data, "strategies"), next_cursor

    async def get_strategy(self, strategy_id: str) -> dict[str, Any]:
        return await self._get(
            f"/strategies/{_assert_safe_id(strategy_id, 'strategy id')}"
        )

    async def list_strategy_versions(self, strategy_id: str) -> dict[str, Any]:
        return await self._get(
            f"/strategies/{_assert_safe_id(strategy_id, 'strategy id')}/versions"
        )

    async def get_strategy_schedule(self, strategy_id: str) -> dict[str, Any]:
        return await self._get(
            f"/strategies/{_assert_safe_id(strategy_id, 'strategy id')}/schedule"
        )

    async def validate_strategy(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Dry-run a strategy against Glider's own gates. Persists nothing.

        The API runs asset validation and structural validation (CAIP-19
        parsing, weight sum, duplicates). A local check runs first so an obvious
        mistake does not spend a request, but the API stays the authority.
        """
        self._check_payload(payload)
        return await self._post("/strategies/validate", payload)

    async def create_strategy(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a strategy (a reusable template). Does not enroll anyone."""
        self._check_payload(payload)
        return await self._post("/strategies", payload)

    async def publish_strategy_version(
        self, strategy_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Publish a new allocation version. Enrolled portfolios follow it."""
        if "allocation" in payload:
            rows = self._rows(payload)
            problems = validate_weights(rows)
            if problems:
                raise IntegrationError(
                    "refusing to publish an invalid allocation: " + "; ".join(problems)
                )
        return await self._post(
            f"/strategies/{_assert_safe_id(strategy_id, 'strategy id')}/versions",
            payload,
        )

    # ---- drafts (validated or local, never submitted) -------------------

    async def validate_draft(self, draft: GliderDraft) -> GliderDraft:
        """Check a draft against Glider's own gates, if we can reach them.

        Offline (no API key, or no resolved asset ids) the draft comes back as
        ``draft_local`` with a note saying it was not submitted. That is not a
        failure: a draft is meant to be inspected before anything is sent.

        A rejection comes back as ``status="rejected"`` rather than an exception,
        because "Glider refused this allocation" is an answer the user needs, not
        an error the caller should swallow.
        """
        if not self.online:
            return draft.model_copy(
                update={
                    "status": "draft_local",
                    "submitted": False,
                    "note": (
                        "Glider is not configured, so this draft was not "
                        "submitted. Nothing leaves your machine."
                    ),
                }
            )

        unresolved = [w.asset_class for w in draft.weights if not w.asset_id]
        if unresolved:
            return draft.model_copy(
                update={
                    "status": "draft_local",
                    "submitted": False,
                    "note": (
                        "no asset ids resolved for "
                        + ", ".join(unresolved)
                        + ", so this draft was not submitted. Ids come from a live "
                        "source; they are never guessed."
                    ),
                }
            )

        try:
            payload = draft.as_payload()
        except ValueError as e:
            return draft.model_copy(
                update={"status": "rejected", "submitted": False, "note": str(e)}
            )

        try:
            result = await self.validate_strategy(payload)
        except IntegrationError as e:
            return draft.model_copy(
                update={"status": "rejected", "submitted": False, "note": str(e)}
            )

        if result.get("valid") is True:
            return draft.model_copy(
                update={
                    "status": "validated",
                    "submitted": False,
                    "note": (
                        "Glider accepted this allocation on a dry run. Nothing is "
                        "submitted until you sign the enrollment."
                    ),
                }
            )
        return draft.model_copy(
            update={
                "status": "rejected",
                "submitted": False,
                "note": f"Glider did not accept this allocation: {result}",
            }
        )

    # ---- portfolios ----------------------------------------------------

    async def list_portfolios(
        self, cursor: str | None = None, limit: int | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        data, next_cursor = await self._get_page("/portfolios", params)
        return self._collection(data, "portfolios"), next_cursor

    async def get_portfolio(self, portfolio_id: str) -> dict[str, Any]:
        """Portfolio detail, including the ``schedule`` block.

        ``schedule.status``, ``nextDueAt`` and ``lastRebalanceAt`` are the
        canonical rebalance visibility fields and are passed through untouched.
        """
        return await self._get(
            f"/portfolios/{_assert_safe_id(portfolio_id, 'portfolio id')}"
        )

    async def get_positions(self, portfolio_id: str) -> dict[str, Any]:
        """Live per-asset balances and USD values.

        Glider surfaces partial failures as a ``warnings[]`` array with a 200
        rather than an error, so callers must read ``warnings`` instead of
        assuming an empty ``assets`` list means an empty portfolio.
        """
        return await self._get(
            f"/portfolios/{_assert_safe_id(portfolio_id, 'portfolio id')}/positions"
        )

    async def discover_assets(self, portfolio_id: str) -> dict[str, str]:
        """A symbol -> CAIP-19 map built from the user's own live positions.

        This is the safe way to learn an asset id: observe a real one rather than
        construct one. Returns an empty map when nothing is held, which is
        correct -- a portfolio with no positions has no assets to reuse.
        """
        data = await self.get_positions(portfolio_id)
        assets = data.get("assets")
        if not isinstance(assets, list):
            return {}
        discovered: dict[str, str] = {}
        for row in assets:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            asset_id = row.get("assetId")
            if symbol and isinstance(asset_id, str) and is_caip19(asset_id):
                discovered[symbol] = asset_id
        return discovered

    # ---- automation ----------------------------------------------------

    async def rebalance(self, portfolio_id: str) -> dict[str, Any]:
        """Dispatch a one-off rebalance. Returns the operation to poll.

        Subject to a per-portfolio cooldown; a call that arrives too soon
        surfaces as a 429 with ``Retry-After``. If a run is already in flight,
        the same ``operationId`` comes back.
        """
        return await self._post(
            f"/portfolios/{_assert_safe_id(portfolio_id, 'portfolio id')}/rebalance",
            {},
        )

    async def get_operation(
        self, portfolio_id: str, operation_id: str
    ) -> dict[str, Any]:
        return await self._get(
            f"/portfolios/{_assert_safe_id(portfolio_id, 'portfolio id')}"
            f"/operations/{_assert_safe_id(operation_id, 'operation id')}"
        )

    async def await_operation(
        self, portfolio_id: str, operation_id: str
    ) -> dict[str, Any]:
        """Poll an operation to a terminal state.

        Glider asks for a 2-5 second cadence and gives no SLA, so this never
        reports a result it has not observed. If the poll budget runs out it
        returns the last seen state with ``settled: False`` -- "still running" is
        an honest answer and a wrong one is not.
        """
        last: dict[str, Any] = {}
        for attempt in range(self.max_polls):
            last = await self.get_operation(portfolio_id, operation_id)
            state = str(last.get("state") or "").casefold()
            if state in _TERMINAL_STATES:
                return {**last, "settled": True}
            if attempt < self.max_polls - 1:
                await asyncio.sleep(self.poll_seconds)
        return {
            **last,
            "settled": False,
            "note": (
                "the operation has not reached a terminal state within the poll "
                "budget; it may still complete"
            ),
        }

    # ---- enrollment (user-signed, never auto-committed) -----------------

    async def prepare_enrollment(
        self,
        strategy_id: str,
        owner_account_id: str,
        chain_ids: list[int],
        account_type: str | None = None,
    ) -> dict[str, Any]:
        """Stage 1 of two-stage enrollment: prepare the wallet authorization.

        Returns the payload the *user's own wallet* signs, plus the round-trip
        fields (``flowId``, ``accountIndex``, ``agentAccountId``) that stage 2
        needs. ``flowId`` is the idempotency anchor and is valid for 24 hours.

        This commits nothing. Stage 2 is intentionally absent: it requires the
        user's signature, and Miriam does not enroll anyone on their behalf.
        """
        payload: dict[str, Any] = {
            "ownerAccountId": owner_account_id,
            "strategyId": _assert_safe_id(strategy_id, "strategy id"),
            "chainIds": list(chain_ids),
        }
        if account_type:
            payload["accountType"] = account_type
        return await self._post("/enroll/signature", payload)

    @staticmethod
    def enrollment_intent(
        prepared: dict[str, Any],
        *,
        strategy_name: str = "",
        amount_usd: Decimal | None = None,
    ) -> dict[str, Any]:
        """Describe the enrollment handoff without performing it.

        Everything the user needs to complete it themselves: what they are
        enrolling in, what they will sign, what it costs them, and the risks they
        are accepting. The signature step is theirs.
        """
        message = prepared.get("message") or {}
        kind = str(message.get("kind") or "unknown")
        return {
            "action": "user_signed_enrollment",
            "strategy_name": strategy_name,
            "flow_id": prepared.get("flowId"),
            "account_index": prepared.get("accountIndex"),
            "agent_account_id": prepared.get("agentAccountId"),
            "signature_kind": kind,
            "deposit_account_id": prepared.get("depositAccountId"),
            "amount_usd": str(amount_usd) if amount_usd is not None else None,
            "requires_user_signature": True,
            "note": (
                "Miriam prepares this; you sign it in your own wallet. Nothing is "
                "enrolled until you do. Non-custodial means the keys stay yours."
            ),
        }

    # ---- internal helpers ----------------------------------------------

    def _check_payload(self, payload: dict[str, Any]) -> None:
        """Local pre-flight before spending a request."""
        name = str(payload.get("name") or "").strip()
        if not name:
            raise IntegrationError("a Glider strategy needs a name")
        rows = self._rows(payload)
        problems = validate_weights(rows)
        if len(rows) > _MAX_ALLOCATION_ASSETS:
            problems.append(
                f"at most {_MAX_ALLOCATION_ASSETS} assets are allowed "
                f"(got {len(rows)})"
            )
        if problems:
            raise IntegrationError(
                "refusing to send an invalid strategy payload: " + "; ".join(problems)
            )

    @staticmethod
    def _rows(payload: dict[str, Any]) -> list[dict[str, str]]:
        allocation = payload.get("allocation")
        if not isinstance(allocation, dict):
            raise IntegrationError("a strategy payload needs an 'allocation' object")
        rows = allocation.get("assets")
        if not isinstance(rows, list):
            raise IntegrationError("a strategy allocation needs an 'assets' list")
        return rows

    @staticmethod
    def _collection(data: Any, key: str) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = data.get(key)
            if isinstance(items, list):
                return items
        return []

    async def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def _get_page(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[Any, str | None]:
        """Like ``_get``, but keeps the pagination cursor beside ``data``."""
        body = await self._request_raw("GET", path, params=params)
        return body.get("data"), body.get("nextCursor")

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, payload=payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        body = await self._request_raw(
            method, path, params=params, payload=payload, authenticated=authenticated
        )
        data: Any = body.get("data", body)
        return data if isinstance(data, dict) else {"data": data}

    async def _request_raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        """One authenticated call, with bounded retries and envelope handling.

        Reads are retried; writes are not, because Glider's write endpoints use
        an idempotency anchor we do not control from here, and repeating an
        unanchored write is how a double-rebalance happens.
        """
        if authenticated and not self.api_key:
            raise ConfigurationError(
                "GLIDER_API_KEY is not set, so Glider cannot be reached"
            )
        _assert_safe_id(path.strip("/").split("/")[0], "path root")

        retryable = method.upper() == "GET"
        attempts = self.max_retries if retryable else 0
        headers: dict[str, str] = {}
        if not authenticated:
            headers["x-api-key"] = ""

        last_error: Exception | None = None
        for attempt in range(attempts + 1):
            try:
                resp = await self._client.request(
                    method, path, params=params, json=payload, headers=headers or None
                )
            except httpx.HTTPError as e:
                last_error = e
                if attempt < attempts:
                    await asyncio.sleep(0.3 * (2**attempt))
                    continue
                logger.warning("Glider %s %s unreachable: %s", method, path, e)
                raise IntegrationError(
                    f"Glider {method} {path} unreachable: {e}"
                ) from e

            if resp.status_code in _RETRYABLE_STATUS and attempt < attempts:
                await asyncio.sleep(self._retry_delay(resp, attempt))
                continue

            if resp.status_code >= 400:
                raise self._error(resp, method, path)

            if not resp.content:
                return {"success": True, "data": {}}
            try:
                parsed: Any = resp.json()
            except ValueError as e:
                raise IntegrationError(
                    f"Glider {method} {path} returned a non-JSON body"
                ) from e
            if not isinstance(parsed, dict):
                raise IntegrationError(
                    f"Glider {method} {path} returned an unexpected body shape"
                )
            return parsed

        raise IntegrationError(f"Glider {method} {path} failed: {last_error}")

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        """Honor ``Retry-After`` when the API sends one, else back off."""
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return float(min(30.0, max(0.0, float(header))))
            except ValueError:
                pass
        return float(0.3 * (2**attempt))

    @staticmethod
    def _error(resp: httpx.Response, method: str, path: str) -> IntegrationError:
        """Turn the API's error envelope into a readable ``IntegrationError``.

        Glider reports ``{success: false, error: {code, message, details[]}}``.
        The code and details are preserved because they are what makes a failure
        actionable (``API_104`` is a missing scope; ``API_400`` with details is a
        rejected allocation).
        """
        code = ""
        message = ""
        details: list[str] = []
        try:
            body = resp.json()
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                code = str(error.get("code") or "")
                message = str(error.get("message") or "")
                raw_details = error.get("details")
                if isinstance(raw_details, list):
                    details = [str(d) for d in raw_details]
        except ValueError:
            pass

        if not message:
            message = (resp.text or "")[:200]

        parts = [f"Glider {method} {path} failed ({resp.status_code}"]
        if code:
            parts.append(f" {code}")
        parts.append(f"): {message}")
        if details:
            parts.append(" | " + "; ".join(details))

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                parts.append(f" (retry after {retry_after}s)")
        return IntegrationError("".join(parts))


_client: GliderClient | None = None


def get_glider_client() -> GliderClient:
    """The process-wide Glider client singleton."""
    global _client
    if _client is None:
        _client = GliderClient()
    return _client

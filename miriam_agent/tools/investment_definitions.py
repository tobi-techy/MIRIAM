"""Investment tool definitions (Rail's Glider-backed Agent API).

These tools expose ``/api/v1/investments/*`` to Miriam: portfolio and position
reads, asset lookup, strategy reads + version history, rebalance previews,
execution/audit reads, publicly listed investor data, and the staged mutations
that create strategies, enroll funds, set allocations, and place single-asset
buys/sells.

Importing this module registers every tool on the shared singleton
registry. ``miriam_agent.tools.__init__`` imports it right after
``definitions`` so the production registry always includes them.

Honesty constraints baked into the descriptions:
  * Glider executes toward a target allocation. ``buy_asset`` / ``sell_asset``
    adjust that allocation; they are NOT limit orders and no price is
    guaranteed.
  * Investor data is only what Glider publicly exposes. Fields Glider does not
    provide (e.g. verifiable track record, audited returns) are labelled
    UNAVAILABLE rather than invented.
  * Withdrawals are app-only (interactive session + passcode step-up), so no
    agent tool exposes them.

Staged mutations: the Go API answers a first call with HTTP 202 and a
``confirmation`` object (``status: "AWAITING_CONFIRMATION"``). The confirmed
call is replayed with the issued token; the agent passes it through the tool
context key ``confirmation_token`` and the Go client forwards it as the
``confirmation_token`` request-body field (the API also accepts the
``X-Investment-Confirmation`` header). The agent replays the token itself right
after the user approves the action (see ``STAGED_CONFIRMATION_TOOLS``), because
Miriam only runs these handlers once the user has approved them.
"""

from typing import Any

from miriam_agent.agents.tools import RiskLevel, get_registry
from miriam_agent.core.exceptions import ValidationError
from miriam_agent.integrations.go_client import get_go_client

registry = get_registry()

_SCHEMA_STRING = {"type": "string"}
_SCHEMA_NUMBER = {"type": "number"}
_SCHEMA_INT = {"type": "integer", "minimum": 0} # type: ignore[reportGeneralTypeIssues]
_SCHEMA_ARRAY = {"type": "array"}
_SCHEMA_OBJECT = {"type": "object"}

_ALLOCATION_ITEM = {  # type: ignore[reportGeneralTypeIssues]
    "type": "object",
    "properties": {
        "asset_id": {**_SCHEMA_STRING, "description": "Glider asset id"},
        "caip19": {**_SCHEMA_STRING, "description": "CAIP-19 asset identifier"},
        "symbol": {**_SCHEMA_STRING, "description": "Ticker symbol"},
        "weight": {**_SCHEMA_NUMBER, "description": "Target weight (0-1 or percent)"},
    },
}

_INVESTOR_NOTE = (
    "Investor data is only what Glider publicly exposes. Fields Glider does not "
    "provide (verified track record, audited returns, AUM under your control) are "
    "not available and are never invented."
)

_STAGED_NOTE = (
    "Staged for confirmation: the API returns an AWAITING_CONFIRMATION preview and "
    "nothing happens until the user approves. The confirmed call is then replayed "
    "with the confirmation token the API issued, which the handler reads from the "
    "tool context ('confirmation_token')."
)

# Endpoints that stage behind a payload-bound confirmation token. The agent
# replays the token immediately once the user has approved the action, so a
# single user approval executes it instead of asking twice.
STAGED_CONFIRMATION_TOOLS = frozenset(
    {
        "create_strategy",
        "update_strategy",
        "enroll_strategy",
        "buy_asset",
        "sell_asset",
        "set_allocation",
    }
)

# ---------------------------------------------------------------------------
# Reads: portfolio & positions
# ---------------------------------------------------------------------------


async def _get_portfolio(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_portfolio(ctx["token"])


registry.register(
    name="get_portfolio",
    description=(
        "Get the user's investment portfolio as Glider reports it: total, "
        "invested and cash value in USD, unrealized P&L, the enrollment list, and "
        "positions (asset, symbol, balance, value, weight, price). Returned as-of "
        "the timestamp Glider reports, plus a stale flag when the provider data is "
        "old. Real data only; no projections."
    ),
    args_schema={"type": "object", "properties": {}},
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_portfolio,
)


async def _get_positions(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_positions(ctx["token"])


registry.register(
    name="get_positions",
    description=(
        "List the user's investment positions (asset, CAIP-19 id, symbol, "
        "balance, USD value, weight, price). Use when the user asks what they hold "
        "rather than the full portfolio summary."
    ),
    args_schema={"type": "object", "properties": {}},
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_positions,
)


# ---------------------------------------------------------------------------
# Reads: assets
# ---------------------------------------------------------------------------


async def _search_assets(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.list_investment_assets(
        ctx["token"], query=args.get("query"), limit=args.get("limit")
    )


registry.register(
    name="search_assets",
    description=(
        "Search the tradable asset universe by name or symbol. Returns whether "
        "each asset is allowlisted/prohibited for the user plus the latest price and "
        "as-of time. Use before buying so the symbol maps to a real asset id."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "query": {**_SCHEMA_STRING, "description": "Name or symbol to search"},
            "limit": {**_SCHEMA_INT, "description": "Max assets to return"},
        },
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_search_assets,
)


async def _get_asset(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    asset_id = args.get("asset_id")
    symbol = args.get("symbol")
    if asset_id:
        return await client.get_investment_asset(
            ctx["token"], asset_id, caip19=args.get("caip19"), symbol=symbol
        )
    if not symbol:
        raise ValidationError("get_asset requires either 'asset_id' or 'symbol'")
    data = await client.list_investment_assets(ctx["token"], query=symbol, limit=10)
    assets = data.get("assets")
    assets = assets if isinstance(assets, list) else []
    target = str(symbol).upper()
    match = next(
        (a for a in assets if str(a.get("symbol", "")).upper() == target), None
    )
    return {
        "asset": match,
        "found": match is not None,
        "query": symbol,
        "candidates": assets,
        "note": (
            "No exact symbol match; inspect 'candidates'."
            if match is None
            else "Matched Glider asset."
        ),
    }


registry.register(
    name="get_asset",
    description=(
        "Get one asset by asset_id (or CAIP-19), or by symbol (symbol is resolved "
        "through asset search and only an exact symbol match is returned). Includes "
        "asset class, chain, allowlist/prohibited status, price, and as-of time."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "asset_id": {**_SCHEMA_STRING, "description": "Glider asset id"},
            "caip19": {**_SCHEMA_STRING, "description": "CAIP-19 asset identifier"},
            "symbol": {**_SCHEMA_STRING, "description": "Ticker, e.g. AAPL"},
        },
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_asset,
)


# ---------------------------------------------------------------------------
# Reads: strategies & previews
# ---------------------------------------------------------------------------


async def _list_strategies(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.list_investment_strategies(
        ctx["token"], status=args.get("status")
    )


registry.register(
    name="list_strategies",
    description=(
        "List the user's investment strategies (id, name, status, current version, "
        "risk, horizon, objective, target allocation). Optionally filter by status."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "status": {
                **_SCHEMA_STRING,
                "description": "Optional status filter, e.g. active or draft",
            }
        },
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_list_strategies,
)


async def _get_strategy(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_strategy(ctx["token"], args["strategy_id"])


registry.register(
    name="get_strategy",
    description=(
        "Get one strategy with its version history: each version's target "
        "allocation, rebalance rules, contribution rules, rationale, and created-at."
    ),
    args_schema={
        "type": "object",
        "properties": {"strategy_id": {**_SCHEMA_STRING, "description": "Strategy id"}},
        "required": ["strategy_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_strategy,
)


async def _get_rebalance_preview(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.preview_investment_strategy(
        ctx["token"], args["strategy_id"], amount_usd=args.get("amount_usd")
    )


registry.register(
    name="get_rebalance_preview",
    description=(
        "Preview rebalancing a strategy toward its target allocation: current vs "
        "target allocation, drift, proposed trades with estimated amounts/units, "
        "estimated fees and slippage, expected post-trade allocation, the policy "
        "verdict and reasons, and the market-data as-of time. This has NO side "
        "effects and does not place trades."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "strategy_id": {**_SCHEMA_STRING, "description": "Strategy id"},
            "amount_usd": {
                **_SCHEMA_NUMBER,
                "description": "Optional cash amount to include in the rebalance",
            },
        },
        "required": ["strategy_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_rebalance_preview,
)


# ---------------------------------------------------------------------------
# Reads: limits, executions & audit
# ---------------------------------------------------------------------------


async def _get_investment_limits(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_limits(ctx["token"])


registry.register(
    name="get_investment_limits",
    description=(
        "Get the user's investment limits and policy verdicts: KYC tier, whether "
        "they can create a strategy / enroll / withdraw, max position %, max "
        "strategy %, max transaction and daily volume in USD, minimum cash reserve, "
        "max enrollments, and how many assets are allowed."
    ),
    args_schema={"type": "object", "properties": {}},
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_investment_limits,
)


async def _list_executions(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.list_investment_executions(
        ctx["token"], status=args.get("status"), limit=args.get("limit")
    )


registry.register(
    name="list_executions",
    description=(
        "List the user's investment executions (the auditable record of every "
        "buy, sell, rebalance, or allocation change Glider performed), newest "
        "first. Optionally filter by status. An 'order' is an execution."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "status": {**_SCHEMA_STRING, "description": "Optional status filter"},
            "limit": {**_SCHEMA_INT, "description": "Max executions to return"},
        },
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_list_executions,
)


async def _get_execution(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_execution(ctx["token"], args["execution_id"])


registry.register(
    name="get_execution",
    description=(
        "Get one investment execution by id: kind, side, status, asset, "
        "requested vs validated amount, strategy and version, policy verdict and "
        "reasons, failure code/reason, provider, and timestamps."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "execution_id": {**_SCHEMA_STRING, "description": "Execution (order) id"}
        },
        "required": ["execution_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_execution,
)


async def _get_execution_status(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    execution = await client.get_investment_execution(
        ctx["token"], args["execution_id"]
    )
    result: dict[str, Any] = {
        "execution_id": execution.get("id", args["execution_id"]),
        "status": execution.get("status"),
    }
    if execution.get("failure_reason"):
        result["failure_reason"] = execution["failure_reason"]
    return result


registry.register(
    name="get_execution_status",
    description=(
        "Get just the status (and failure reason, when there is one) of an "
        "investment execution by id. Use for a quick 'did it go through?' check."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "execution_id": {**_SCHEMA_STRING, "description": "Execution (order) id"}
        },
        "required": ["execution_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_execution_status,
)


async def _list_audit_events(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.list_investment_audit_events(
        ctx["token"], limit=args.get("limit")
    )


registry.register(
    name="list_audit_events",
    description=(
        "List the investment audit trail (confirmation requested, policy blocked, "
        "execution recorded, funding, and similar events), newest first, for "
        "explaining what happened and when."
    ),
    args_schema={
        "type": "object",
        "properties": {"limit": {**_SCHEMA_INT, "description": "Max events to return"}},
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_list_audit_events,
)


# ---------------------------------------------------------------------------
# Reads: investors (public data only)
# ---------------------------------------------------------------------------


async def _list_investors(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.list_investment_investors(
        ctx["token"],
        collection=args.get("collection"),
        cursor=args.get("cursor"),
        limit=args.get("limit"),
    )


registry.register(
    name="list_investors",
    description=(
        "List investors Glider publicly exposes for copy/mirror discovery, with "
        "their allocation and the metrics Glider publishes (TVL, portfolio count, "
        "performance windows, max APY) plus provenance. " + _INVESTOR_NOTE
    ),
    args_schema={
        "type": "object",
        "properties": {
            "collection": {
                "type": "string",
                "enum": ["curated", "top_performing"],
                "description": "Optional Glider collection to list",
            },
            "cursor": {**_SCHEMA_STRING, "description": "Pagination cursor"},
            "limit": {**_SCHEMA_INT, "description": "Max investors to return"},
        },
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_list_investors,
)


async def _get_investor(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_investor(ctx["token"], args["investor_id"])


registry.register(
    name="get_investor",
    description=(
        "Get one investor Glider publicly exposes: description, allocation, "
        "published metrics, and provenance fields. " + _INVESTOR_NOTE
    ),
    args_schema={
        "type": "object",
        "properties": {
            "investor_id": {**_SCHEMA_STRING, "description": "Glider investor id"}
        },
        "required": ["investor_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_investor,
)


async def _get_investor_activity(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_investor_activity(
        ctx["token"], args["investor_id"]
    )


registry.register(
    name="get_investor_activity",
    description=(
        "Get the activity Glider publicly exposes for one investor, plus "
        "provenance and an 'unavailable_reason' when Glider has no activity to "
        "show. " + _INVESTOR_NOTE
    ),
    args_schema={
        "type": "object",
        "properties": {
            "investor_id": {**_SCHEMA_STRING, "description": "Glider investor id"}
        },
        "required": ["investor_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_investor_activity,
)


# ---------------------------------------------------------------------------
# Staged mutations (stage & confirm; the Go API returns a preview + verdict)
# ---------------------------------------------------------------------------


async def _create_strategy(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    payload: dict[str, Any] = {
        "name": args["name"],
        "objective": args["objective"],
        "risk": args["risk"],
        "horizon": args["horizon"],
        "target_allocation": args["target_allocation"],
    }
    for key in (
        "rebalance_rules",
        "contribution_rules",
        "execution_rules",
        "rationale",
    ):
        if args.get(key) is not None:
            payload[key] = args[key]
    return await client.create_investment_strategy(
        ctx["token"],
        payload,
        confirmation_token=ctx.get("confirmation_token"),
    )


registry.register(
    name="create_strategy",
    description=(
        "Create a new investment strategy with a name, objective, risk, horizon, "
        "and target allocation (asset_id/caip19/symbol + weight per asset). "
        "Optionally include rebalance, contribution, and execution rules and a "
        "rationale. " + _STAGED_NOTE
    ),
    args_schema={
        "type": "object",
        "properties": {
            "name": {**_SCHEMA_STRING, "description": "Strategy name"},
            "objective": {**_SCHEMA_STRING, "description": "What the strategy is for"},
            "risk": {**_SCHEMA_STRING, "description": "Risk level/label"},
            "horizon": {**_SCHEMA_STRING, "description": "Time horizon"},
            "target_allocation": {
                **_SCHEMA_ARRAY,
                "items": _ALLOCATION_ITEM,
                "description": "Target weights, e.g. [{asset_id, weight}]",
            },
            "rebalance_rules": {**_SCHEMA_OBJECT, "description": "Optional rules"},
            "contribution_rules": {
                **_SCHEMA_OBJECT,
                "description": "Optional contribution rules",
            },
            "execution_rules": {**_SCHEMA_OBJECT, "description": "Optional rules"},
            "rationale": {**_SCHEMA_STRING, "description": "Optional rationale"},
        },
        "required": ["name", "objective", "risk", "horizon", "target_allocation"],
    },
    category="investment_action",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_create_strategy,
)


async def _update_strategy(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    payload: dict[str, Any] = {
        "target_allocation": args["target_allocation"],
        "rationale": args["rationale"],
    }
    for key in ("rebalance_rules", "contribution_rules"):
        if args.get(key) is not None:
            payload[key] = args[key]
    return await client.publish_investment_strategy_version(
        ctx["token"],
        args["strategy_id"],
        payload,
        confirmation_token=ctx.get("confirmation_token"),
    )


registry.register(
    name="update_strategy",
    description=(
        "Update a strategy by publishing a new version: a new target allocation "
        "plus a rationale (and optional rebalance/contribution rules). Prior "
        "versions are kept. " + _STAGED_NOTE
    ),
    args_schema={
        "type": "object",
        "properties": {
            "strategy_id": {**_SCHEMA_STRING, "description": "Strategy id"},
            "target_allocation": {
                **_SCHEMA_ARRAY,
                "items": _ALLOCATION_ITEM,
                "description": "New target weights",
            },
            "rationale": {**_SCHEMA_STRING, "description": "Why the change"},
            "rebalance_rules": {**_SCHEMA_OBJECT, "description": "Optional rules"},
            "contribution_rules": {
                **_SCHEMA_OBJECT,
                "description": "Optional contribution rules",
            },
        },
        "required": ["strategy_id", "target_allocation", "rationale"],
    },
    category="investment_action",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_update_strategy,
)


async def _enroll_strategy(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    payload: dict[str, Any] = {"strategy_id": args["strategy_id"]}
    if args.get("amount_usd") is not None:
        payload["amount_usd"] = args["amount_usd"]
    if args.get("source"):
        payload["source"] = args["source"]
    if ctx.get("idempotency_key"):
        payload["idempotency_key"] = ctx["idempotency_key"]
    return await client.enroll_investment(
        ctx["token"],
        payload,
        confirmation_token=ctx.get("confirmation_token"),
    )


registry.register(
    name="enroll_strategy",
    description=(
        "Enroll the user in an investment strategy, optionally funding it with "
        "amount_usd from a named source ('spending' by default, or 'stash'). "
        + _STAGED_NOTE
    ),
    args_schema={
        "type": "object",
        "properties": {
            "strategy_id": {**_SCHEMA_STRING, "description": "Strategy id"},
            "amount_usd": {
                **_SCHEMA_NUMBER,
                "description": "Optional amount to fund the enrollment",
            },
            "source": {
                "type": "string",
                "enum": ["spending", "stash"],
                "description": "Funding source (default spending)",
            },
        },
        "required": ["strategy_id"],
    },
    category="investment_action",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_enroll_strategy,
)


async def _pause_strategy(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.pause_investment_strategy(ctx["token"], args["strategy_id"])


registry.register(
    name="pause_strategy",
    description=(
        "Pause a strategy so Glider stops rebalancing/contributing to its "
        "portfolio. This executes immediately once approved."
    ),
    args_schema={
        "type": "object",
        "properties": {"strategy_id": {**_SCHEMA_STRING, "description": "Strategy id"}},
        "required": ["strategy_id"],
    },
    category="investment_action",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_pause_strategy,
)


async def _resume_strategy(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.resume_investment_strategy(ctx["token"], args["strategy_id"])


registry.register(
    name="resume_strategy",
    description=(
        "Resume a paused strategy so Glider rebalances/contributes again. This "
        "executes immediately once approved."
    ),
    args_schema={
        "type": "object",
        "properties": {"strategy_id": {**_SCHEMA_STRING, "description": "Strategy id"}},
        "required": ["strategy_id"],
    },
    category="investment_action",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_resume_strategy,
)


async def _rebalance_strategy(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.rebalance_investment_strategy(
        ctx["token"], args["strategy_id"], reason=args.get("reason")
    )


registry.register(
    name="rebalance_strategy",
    description=(
        "Trigger a rebalance of a strategy's portfolio back toward its target "
        "allocation. Returns the resulting execution. This executes immediately "
        "once approved and may return a provider cooldown error (429) when "
        "Glider is rate-limiting rebalances."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "strategy_id": {**_SCHEMA_STRING, "description": "Strategy id"},
            "reason": {**_SCHEMA_STRING, "description": "Optional reason"},
        },
        "required": ["strategy_id"],
    },
    category="investment_action",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_rebalance_strategy,
)


_ORDER_HONESTY = (
    "Glider executes toward a target allocation and prices are not guaranteed. "
    "This adjusts the allocation; it is not a limit order and there is no "
    "price or fill-time control. "
)


def _single_order_payload(args: dict[str, Any], side: str) -> dict[str, Any]:
    if not args.get("asset_id") and not args.get("symbol"):
        raise ValidationError("provide either 'asset_id' or 'symbol'")
    payload: dict[str, Any] = {"side": side, "amount_usd": args["amount_usd"]}
    if args.get("strategy_id"):
        payload["strategy_id"] = args["strategy_id"]
    if args.get("asset_id"):
        payload["asset_id"] = args["asset_id"]
    if args.get("symbol"):
        payload["symbol"] = args["symbol"]
    return payload


async def _buy_asset(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    payload = _single_order_payload(args, "buy")
    if ctx.get("idempotency_key"):
        payload["idempotency_key"] = ctx["idempotency_key"]
    return await client.create_investment_order(
        ctx["token"], payload, confirmation_token=ctx.get("confirmation_token")
    )


registry.register(
    name="buy_asset",
    description=(
        "Buy amount_usd of one asset in the user's investment account. "
        + _ORDER_HONESTY
        + _STAGED_NOTE
    ),
    args_schema={
        "type": "object",
        "properties": {
            "strategy_id": {
                **_SCHEMA_STRING,
                "description": "Strategy whose allocation changes (optional)",
            },
            "asset_id": {**_SCHEMA_STRING, "description": "Glider asset id"},
            "symbol": {**_SCHEMA_STRING, "description": "Ticker, e.g. AAPL"},
            "amount_usd": {**_SCHEMA_NUMBER, "description": "Dollar amount to buy"},
        },
        "required": ["amount_usd"],
    },
    category="investment_action",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_buy_asset,
)


async def _sell_asset(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    payload = _single_order_payload(args, "sell")
    if ctx.get("idempotency_key"):
        payload["idempotency_key"] = ctx["idempotency_key"]
    return await client.create_investment_order(
        ctx["token"], payload, confirmation_token=ctx.get("confirmation_token")
    )


registry.register(
    name="sell_asset",
    description=(
        "Sell amount_usd of one asset in the user's investment account. "
        + _ORDER_HONESTY
        + _STAGED_NOTE
    ),
    args_schema={
        "type": "object",
        "properties": {
            "strategy_id": {
                **_SCHEMA_STRING,
                "description": "Strategy whose allocation changes (optional)",
            },
            "asset_id": {**_SCHEMA_STRING, "description": "Glider asset id"},
            "symbol": {**_SCHEMA_STRING, "description": "Ticker, e.g. AAPL"},
            "amount_usd": {**_SCHEMA_NUMBER, "description": "Dollar amount to sell"},
        },
        "required": ["amount_usd"],
    },
    category="investment_action",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_sell_asset,
)


async def _set_allocation(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    payload: dict[str, Any] = {"targets": args["targets"]}
    if args.get("strategy_id"):
        payload["strategy_id"] = args["strategy_id"]
    if args.get("rationale"):
        payload["rationale"] = args["rationale"]
    if ctx.get("idempotency_key"):
        payload["idempotency_key"] = ctx["idempotency_key"]
    return await client.set_investment_allocation(
        ctx["token"], payload, confirmation_token=ctx.get("confirmation_token")
    )


registry.register(
    name="set_allocation",
    description=(
        "Replace a strategy's whole target allocation at once by weight, e.g. "
        "targets=[{symbol:'AAPL', weight:0.6},{symbol:'VOO', weight:0.4}]. "
        + _ORDER_HONESTY
        + _STAGED_NOTE
    ),
    args_schema={
        "type": "object",
        "properties": {
            "strategy_id": {
                **_SCHEMA_STRING,
                "description": "Strategy whose allocation is replaced",
            },
            "targets": {
                **_SCHEMA_ARRAY,
                "items": _ALLOCATION_ITEM,
                "description": "New target weights",
            },
            "rationale": {**_SCHEMA_STRING, "description": "Why the change"},
        },
        "required": ["targets"],
    },
    category="investment_action",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_set_allocation,
)

"""Investment tool definitions (Rail's Glider-backed Agent API).

These tools expose ``/api/v1/investments/*`` to Miriam: portfolio and position
reads, asset lookup, strategy reads + version history, rebalance previews,
execution/audit reads, and publicly listed investor data.

There are no mutation tools here. Every tool that created a strategy, enrolled
funds, set an allocation or placed a buy or sell called a rail, and a rail must
not be reachable from a chat turn: ``miriam_agent.hands`` is the only thing in
the process that moves money. The action tools and the confirmation-token replay
they depended on were deleted rather than left registered but unused.

Importing this module registers every tool on the shared singleton registry.
``miriam_agent.tools.__init__`` imports it right after ``definitions`` so the
production registry always includes them, and every tool here is read-only so
all of them stay reachable.

``miriam_agent.tools.definitions.build_tool_registry`` still strips the money
tools registered next door; this module registers none.

Honesty constraint baked into the descriptions: investor data is only what
Glider publicly exposes. Fields Glider does not provide (e.g. verifiable track
record, audited returns) are labelled UNAVAILABLE rather than invented.
"""

from typing import Any

from miriam_agent.agents.tools import RiskLevel, get_registry
from miriam_agent.core.exceptions import ValidationError
from miriam_agent.integrations.go_client import get_go_client

registry = get_registry()

_SCHEMA_STRING = {"type": "string"}
_SCHEMA_NUMBER = {"type": "number"}
# Money amounts must be strictly positive (see tools/definitions.py).
_SCHEMA_MONEY = {"type": "number", "exclusiveMinimum": 0}
# Opaque backend ids, constrained so they cannot traverse a URL path.
_SCHEMA_ID = {
    "type": "string",
    "pattern": "^(?=.*[A-Za-z0-9_:-])[A-Za-z0-9_.:-]{1,64}$",
}
_SCHEMA_INT = {"type": "integer", "minimum": 0}  # type: ignore[reportGeneralTypeIssues]
_INVESTOR_NOTE = (
    "Investor data is only what Glider publicly exposes. Fields Glider does not "
    "provide (verified track record, audited returns, AUM under your control) are "
    "not available and are never invented."
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
            "asset_id": {**_SCHEMA_ID, "description": "Glider asset id"},
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
    loader = getattr(client, "list_investable_strategies", None)
    if loader is None:
        return await client.list_investment_strategies(
            ctx["token"], status=args.get("status")
        )
    return await loader(ctx["token"], status=args.get("status"))


registry.register(
    name="list_strategies",
    description=(
        "List investment strategies the user can enroll in: their own plus Rail "
        "strategies such as the stock sleeve (strategy_id, name, status, version, "
        "risk, horizon). Optionally filter the user-owned rows by status."
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
        "properties": {"strategy_id": {**_SCHEMA_ID, "description": "Strategy id"}},
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
            "strategy_id": {**_SCHEMA_ID, "description": "Strategy id"},
            "amount_usd": {
                **_SCHEMA_MONEY,
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
            "execution_id": {**_SCHEMA_ID, "description": "Execution (order) id"}
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
            "execution_id": {**_SCHEMA_ID, "description": "Execution (order) id"}
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
            "investor_id": {**_SCHEMA_ID, "description": "Glider investor id"}
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
            "investor_id": {**_SCHEMA_ID, "description": "Glider investor id"}
        },
        "required": ["investor_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_investor_activity,
)

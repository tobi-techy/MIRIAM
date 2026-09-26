"""Glider strategy/enrollment detail reads.

Split out of ``investment_definitions.py`` to keep both modules under the
size ratchet (contract §4.3). Same contract as the module it came from:
every tool registered here is read-only -- a rail is never reachable from a
chat turn. The schemas and registry live next door; importing this module
registers these tools on the shared singleton registry, and
``miriam_agent.tools.__init__`` imports it alongside ``investment_definitions``.
"""

from typing import Any

from miriam_agent.agents.tools import RiskLevel, get_registry
from miriam_agent.integrations.go_client import get_go_client

registry = get_registry()

_SCHEMA_STRING = {"type": "string"}
_SCHEMA_NUMBER = {"type": "number"}
_SCHEMA_ID = {
    "type": "string",
    "pattern": "^(?=.*[A-Za-z0-9_:-])[A-Za-z0-9_.:-]{1,64}$",
}


# ---------------------------------------------------------------------------
# Reads: strategy & enrollment detail
# ---------------------------------------------------------------------------


async def _get_strategy_performance(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_strategy_performance(
        ctx["token"], args["strategy_id"]
    )


registry.register(
    name="get_strategy_performance",
    description=(
        "Get the template time-weighted return (TWR) curve for one Glider strategy: "
        "a series of {date, percentChange} points plus summary windows and metadata. "
        "This is the strategy's own published curve (template TWR) — not the user's "
        "personal money-weighted return. For the user's actual money curve use "
        "get_enrollment_performance with returnMethod=MWR."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "strategy_id": {**_SCHEMA_ID, "description": "Glider strategy id"}
        },
        "required": ["strategy_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_strategy_performance,
)


async def _get_enrollment_performance(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_enrollment_performance(
        ctx["token"],
        args["enrollment_id"],
        return_method=args.get("returnMethod"),
    )


registry.register(
    name="get_enrollment_performance",
    description=(
        "Get the user's personal money curve for one Glider enrollment. "
        "returnMethod=MWR (default) is the money-weighted return (the user's own "
        "money, in and out); returnMethod=TWR is the time-weighted return. This is "
        "the user's money, not the strategy's template TWR (see get_strategy_performance). "
        "Both are provider values, not estimates. Empty/unavailable shapes are returned "
        "as-is; never invent numbers."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "enrollment_id": {**_SCHEMA_ID, "description": "Glider enrollment id"},
            "returnMethod": {
                "type": "string",
                "enum": ["MWR", "TWR"],
                "description": "Return method; MWR (default) is money-weighted, TWR is time-weighted",
            },
        },
        "required": ["enrollment_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_enrollment_performance,
)


async def _get_sector_exposure(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_enrollment_sector_exposure(
        ctx["token"], args["enrollment_id"]
    )


registry.register(
    name="get_sector_exposure",
    description=(
        "Get sector exposure for the user's Glider sleeve holdings: rows of sector "
        "names with weights and a taxonomy label. This is the provider's actual "
        "exposure breakdown for the enrollment, not an estimate. Empty when the "
        "enrollment has no indexed holdings; say so rather than invent sectors."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "enrollment_id": {**_SCHEMA_ID, "description": "Glider enrollment id"}
        },
        "required": ["enrollment_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_sector_exposure,
)


async def _get_allocation_breakdown(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.breakdown_investment_holdings(
        ctx["token"],
        {
            "holdings": args.get("holdings", []),
            "dimensions": args.get("dimensions", []),
            "multiCategoryMode": args.get("multiCategoryMode", False),
        },
    )


registry.register(
    name="get_allocation_breakdown",
    description=(
        "Break the user's Glider holdings down into buckets across the given dimensions "
        "(e.g. sector, region, asset-class). holdings is a list of {caipAssetId, symbol, "
        "weight}; dimensions is a list of dimension names; multiCategoryMode controls how "
        "assets in multiple categories are counted. Returns raw Glider buckets — provider "
        "values, not estimates."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "holdings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "caipAssetId": {**_SCHEMA_STRING, "description": "CAIP-19 asset id"},
                        "symbol": {**_SCHEMA_STRING, "description": "Asset symbol"},
                        "weight": {**_SCHEMA_NUMBER, "description": "Weight in USD"},
                    },
                },
                "description": "Holdings to break down",
            },
            "dimensions": {
                "type": "array",
                "items": _SCHEMA_STRING,
                "description": "Dimensions to break down by (e.g. sector, region)",
            },
            "multiCategoryMode": {
                "type": "boolean",
                "description": "How to count assets in multiple categories",
            },
        },
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_allocation_breakdown,
)


async def _get_strategy_schedule(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_strategy_schedule(
        ctx["token"], args["strategy_id"]
    )


registry.register(
    name="get_strategy_schedule",
    description=(
        "Get the configured rebalance cadence for one Glider strategy: a schedule object "
        "or {schedule:null, note} when none is configured. This is the configured cadence "
        "only — the runtime nextDueAt lives on the portfolio, not here."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "strategy_id": {**_SCHEMA_ID, "description": "Glider strategy id"}
        },
        "required": ["strategy_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_strategy_schedule,
)


async def _get_strategy_preferences(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_strategy_preferences(
        ctx["token"], args["strategy_id"]
    )


registry.register(
    name="get_strategy_preferences",
    description=(
        "Get the swap trade settings for one Glider strategy: {swap:{slippageBps, "
        "priceImpactBps, thresholdUsd}}. null means the provider default applies. "
        "These are provider values, not estimates."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "strategy_id": {**_SCHEMA_ID, "description": "Glider strategy id"}
        },
        "required": ["strategy_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_strategy_preferences,
)


async def _get_strategy_fees(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_strategy_fees(
        ctx["token"], args["strategy_id"]
    )


registry.register(
    name="get_strategy_fees",
    description=(
        "Get the strategy fees: {swapBps}. This is the provider's published fee, "
        "not an estimate."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "strategy_id": {**_SCHEMA_ID, "description": "Glider strategy id"}
        },
        "required": ["strategy_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_strategy_fees,
)


async def _get_provider_versions(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_investment_strategy_provider_versions(
        ctx["token"],
        args["strategy_id"],
        cursor=args.get("cursor"),
        limit=args.get("limit"),
    )


registry.register(
    name="get_provider_versions",
    description=(
        "Get the live Glider version history for one strategy: a list of versions "
        "with isHead=true marking the currently active version. Use cursor/limit for "
        "pagination. This is the strategy's actual published history, not an estimate "
        "or a local snapshot."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "strategy_id": {**_SCHEMA_ID, "description": "Glider strategy id"},
            "cursor": {**_SCHEMA_STRING, "description": "Pagination cursor"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Max versions to return",
            },
        },
        "required": ["strategy_id"],
    },
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_get_provider_versions,
)

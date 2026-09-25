"""Glider sleeve reads: the Stocklana diversified-stock loop.

Default product is a diversified stock sleeve (Rail Stock Sleeve on Solana),
never a single-name buy. Single-name is an explicit later verb with a cap.

Reads live here so the agent loop and Voice stay grounded in provider state.
There are deliberately NO mutation tools in this module: every tool that moves
money must go through ``miriam_agent.hands`` (the only writer in the
process), and the agent loop cannot reach a rail. Enrollment and funding run
in ``miriam_agent.hands.invest`` after a Hands-issued ``confirm_id`` tap.

Tools (all via the Go money/ledger host; Miriam never talks to Glider or the
ledger directly, and the old brokerage rail is off the invest path):

- glider_get_strategy: read catalogue + our sleeve (fail closed, no fake mints)
- glider_get_portfolio / glider_get_positions: reads (charts consume these)

CAIP parsing uses strict regex, never naive string-split. The sleeve's live
asset ids are resolved from the provider catalogue, never hardcoded: the
authoritative binding is the ``Rail Stock Sleeve`` strategy row the Go backend
owns.
"""

from __future__ import annotations

import re
from typing import Any

from miriam_agent.agents.tools import RiskLevel, get_registry
from miriam_agent.core.exceptions import ValidationError
from miriam_agent.integrations.go_client import get_go_client

registry = get_registry()

_CAIP10_RE = re.compile(
    r"^(?P<namespace>[-a-z0-9]{3,8}):(?P<reference>[-_a-zA-Z0-9]{1,32}):(?P<address>[a-zA-Z0-9]{20,64})$"
)
_CAIP19_RE = re.compile(
    r"^(?P<chain_namespace>[-a-z0-9]{3,8}):(?P<chain_reference>[-_a-zA-Z0-9]{1,64})/(?P<asset_namespace>[-a-z0-9]{3,8}):(?P<asset_reference>[-_a-zA-Z0-9.:]{1,128})$"
)
# Solana-flavoured CAIP-10: solana:<genesis>:<base58>
_SOLANA_CAIP10_RE = re.compile(
    r"^solana:(?P<genesis>[1-9A-HJ-NP-Za-km-z]{32,44}):(?P<address>[1-9A-HJ-NP-Za-km-z]{32,44})$"
)

# Solana mainnet. The one chain id a sleeve enrollment may carry; EVM chain
# ids must never be mixed into the same enroll.
SOLANA_CHAIN_ID = 1399811149
# CAIP-2 genesis for Solana mainnet. Mirrors the Go backend default
# (investment_glider.owner_account_prefix); the Go side remains authoritative.
SOLANA_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOLANA_CAIP_PREFIX = f"solana:{SOLANA_GENESIS}"

SLEEVE_NAME = "Rail Stock Sleeve"


def parse_caip10(account_id: str) -> dict[str, str]:
    """Parse a CAIP-10 account id without naive string-split."""
    value = (account_id or "").strip()
    m = _SOLANA_CAIP10_RE.match(value)
    if m:
        return {
            "namespace": "solana",
            "reference": m.group("genesis"),
            "address": m.group("address"),
        }
    m = _CAIP10_RE.match(value)
    if not m:
        raise ValidationError(f"invalid CAIP-10 account id: {value!r}")
    return {
        "namespace": m.group("namespace"),
        "reference": m.group("reference"),
        "address": m.group("address"),
    }


def parse_caip19(asset_id: str) -> dict[str, str]:
    """Parse a CAIP-19 asset id without naive string-split."""
    value = (asset_id or "").strip()
    m = _CAIP19_RE.match(value)
    if not m:
        raise ValidationError(f"invalid CAIP-19 asset id: {value!r}")
    return {
        "chain_namespace": m.group("chain_namespace"),
        "chain_reference": m.group("chain_reference"),
        "asset_namespace": m.group("asset_namespace"),
        "asset_reference": m.group("asset_reference"),
    }


def deposit_address_from_caip(account_id: str) -> str:
    """Extract the on-chain address from a depositAccountId via CAIP parse."""
    return parse_caip10(account_id)["address"]


def owner_account_for(address: str) -> str:
    """Build the Solana CAIP-10 owner id for a base58 wallet address."""
    clean = (address or "").strip()
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", clean):
        raise ValidationError("owner wallet address is not valid base58")
    return f"{SOLANA_CAIP_PREFIX}:{clean}"


def _sleeve_from_strategies(strategies: list[dict[str, Any]]) -> dict[str, Any] | None:
    for s in strategies:
        if str(s.get("name") or "").strip().lower() == SLEEVE_NAME.lower():
            return s
    return None


async def _glider_get_strategy(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Read the strategy catalogue plus the Rail Stock Sleeve binding.

    Never hardcodes mint addresses. If the sleeve is missing, returns the
    exact catalogue received so the caller escalates instead of faking tickers.
    """
    token = (ctx or {}).get("token") if isinstance(ctx, dict) else None
    if not token:
        return {
            "sleeve": None,
            "live": False,
            "error": "no Go host token on this path",
            "catalogue": [],
            "hint": "Seed the Rail-owned 'Rail Stock Sleeve' strategy row "
            "bound to the live Glider tenant strategy before enrolling. "
            "Do not hardcode mint addresses.",
        }
    client = get_go_client()
    try:
        loader = getattr(client, "list_investable_strategies", None)
        if loader is None:
            data = await client.list_investment_strategies(token, status="active")
        else:
            data = await loader(token, status="active")
    except Exception as e:  # fail closed, nothing invented
        return {
            "sleeve": None,
            "live": False,
            "error": f"strategy catalogue unreadable: {e}",
            "catalogue": [],
            "hint": "Seed the Rail-owned 'Rail Stock Sleeve' strategy row "
            "bound to the live Glider tenant strategy before enrolling. "
            "Do not hardcode mint addresses.",
        }
    strategies = data.get("strategies") if isinstance(data, dict) else None
    strategies = strategies if isinstance(strategies, list) else []
    partial = bool(data.get("partial")) if isinstance(data, dict) else False
    partial_info: dict[str, Any] = {}
    if isinstance(data, dict):
        for key in ("partial", "user_error", "rail_error"):
            if data.get(key) is not None:
                partial_info[key] = data.get(key)
    sleeve = _sleeve_from_strategies(strategies)
    if sleeve is None:
        if partial:
            return {
                "sleeve": None,
                "live": False,
                "error": (
                    "strategy catalogue partial; Rail Stock Sleeve not in "
                    "the visible half, not necessarily unconfigured"
                ),
                "catalogue": strategies,
                "hint": "Retry the catalogue; one source is down. "
                "Do not seed a duplicate sleeve.",
                **partial_info,
            }
        return {
            "sleeve": None,
            "live": False,
            "error": "Rail Stock Sleeve is not configured yet",
            "catalogue": strategies,
            "hint": "Seed the Rail-owned 'Rail Stock Sleeve' strategy row "
            "bound to the live Glider tenant strategy before enrolling. "
            "Do not hardcode mint addresses.",
        }
    glider_id = sleeve.get("glider_strategy_id") or sleeve.get("gliderStrategyId")
    detail: dict[str, Any] = {}
    try:
        from miriam_agent.integrations.go_client import investment_strategy_id

        detail = await client.get_investment_strategy(
            ctx["token"], investment_strategy_id(sleeve)
        )
    except Exception as e:  # fail closed, catalogue stays visible
        detail = {"_tool_error": str(e)}
    return {
        "sleeve": sleeve,
        "glider_strategy_id": glider_id,
        "detail": detail,
        "live": True,
        **partial_info,
    }


registry.register(
    name="glider_get_strategy",
    description=(
        "Read the strategy catalogue and the Rail Stock Sleeve binding "
        "(Solana only). Returns the sleeve plus the exact catalogue. Never "
        "invents mint addresses; if the sleeve is missing the caller must "
        "escalate, not fake tickers."
    ),
    args_schema={"type": "object", "properties": {}},
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_glider_get_strategy,
)


async def _glider_get_portfolio(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    token = (ctx or {}).get("token") if isinstance(ctx, dict) else None
    if not token:
        return {"_tool_error": "no Go host token on this path"}
    client = get_go_client()
    try:
        return await client.get_investment_portfolio(token)
    except Exception as e:  # fail closed, never invent a portfolio
        return {"_tool_error": f"portfolio unreadable: {e}"}


registry.register(
    name="glider_get_portfolio",
    description=(
        "Get the user's Glider-backed portfolio "
        "(live provider state via the Go host)."
    ),
    args_schema={"type": "object", "properties": {}},
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_glider_get_portfolio,
)


async def _glider_get_positions(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    token = (ctx or {}).get("token") if isinstance(ctx, dict) else None
    if not token:
        return {"positions": [], "_tool_error": "no Go host token on this path"}
    client = get_go_client()
    try:
        data = await client.get_investment_positions(token)
    except Exception as e:  # fail closed, charts say indexing instead
        return {"positions": [], "_tool_error": f"positions unreadable: {e}"}
    positions = data.get("positions") if isinstance(data, dict) else None
    if not positions:
        return {
            **(data if isinstance(data, dict) else {}),
            "indexing_note": "nothing indexed yet",
        }
    return data if isinstance(data, dict) else {"positions": positions}


registry.register(
    name="glider_get_positions",
    description=(
        "List live Glider positions (symbol + USD). Charts consume this, never "
        "invented numbers. Empty means indexing: say so and follow up, do not "
        "invent balances."
    ),
    args_schema={"type": "object", "properties": {}},
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_glider_get_positions,
)

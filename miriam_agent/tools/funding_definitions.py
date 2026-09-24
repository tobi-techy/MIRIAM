"""NGN <-> crypto funding read tools (RampHub / Paj / deposit rails).

Read-only, all auto-execute, all LOW. Mutations (initiate/verify OTP,
onramp/offramp creates) live in hands/funding.py behind a confirm_id tap
and are never registered here.

Tools:
- get_crypto_quote: best NGN quote across RampHub + Paj.
- get_funding_orders: last-50 orders for one rail (paj|ramp).
- get_funding_order_status: live poll for one order.
- get_paj_banks: Paj bank list or saved accounts.
- get_paj_verification_status: verified flag derived from saved accounts.
"""

from __future__ import annotations

from typing import Any

from miriam_agent.agents.tools import RiskLevel, get_registry
from miriam_agent.core.exceptions import IntegrationError

registry = get_registry()

_SCHEMA_STRING = {"type": "string"}
_SCHEMA_MONEY = {"type": "number", "exclusiveMinimum": 0}


def _token(ctx: dict[str, Any]) -> str | None:
    token = ctx.get("token")
    return token if isinstance(token, str) and token else None


async def _get_crypto_quote(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    token = _token(ctx)
    if not token:
        return {"_tool_error": "no user token"}
    side = str(args.get("side") or "onramp").lower()
    if side in ("buy",):
        side = "onramp"
    elif side in ("sell",):
        side = "offramp"
    if side not in ("onramp", "offramp", "buy", "sell"):
        return {"_tool_error": f"unknown side {args.get('side')!r}"}
    try:
        amount = float(args["amount"])
    except (KeyError, TypeError, ValueError):
        return {"_tool_error": "amount is required and must be positive"}
    if amount <= 0:
        return {"_tool_error": "amount must be positive"}
    from miriam_agent.integrations.go_client import get_go_client

    try:
        return await get_go_client().get_crypto_quote(token, side, amount)
    except (IntegrationError, Exception) as e:
        return {"_tool_error": str(e)[:200]}


async def _get_funding_orders(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    token = _token(ctx)
    if not token:
        return {"_tool_error": "no user token"}
    kind = str(args.get("kind") or "paj").lower()
    from miriam_agent.integrations.go_client import get_go_client

    try:
        orders = await get_go_client().get_funding_orders(token, kind)
    except (IntegrationError, Exception) as e:
        return {"_tool_error": str(e)[:200]}
    return {"orders": orders, "count": len(orders), "kind": kind}


async def _get_funding_order_status(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    token = _token(ctx)
    if not token:
        return {"_tool_error": "no user token"}
    kind = str(args.get("kind") or "paj").lower()
    order_id = str(args.get("order_id") or args.get("id") or "")
    if not order_id:
        return {"_tool_error": "order_id is required"}
    from miriam_agent.integrations.go_client import get_go_client

    try:
        return await get_go_client().get_funding_order_status(token, kind, order_id)
    except (IntegrationError, Exception) as e:
        return {"_tool_error": str(e)[:200]}


async def _get_paj_banks(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    token = _token(ctx)
    if not token:
        return {"_tool_error": "no user token"}
    saved = bool(args.get("saved"))
    from miriam_agent.integrations.go_client import get_go_client

    try:
        banks = await get_go_client().get_paj_banks(token, saved=saved)
    except (IntegrationError, Exception) as e:
        return {"_tool_error": str(e)[:200]}
    return {"banks": banks, "count": len(banks), "saved": saved}


async def _get_paj_verification_status(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    token = _token(ctx)
    if not token:
        return {"_tool_error": "no user token"}
    from miriam_agent.integrations.go_client import get_go_client

    try:
        return await get_go_client().get_paj_verification_status(token)
    except (IntegrationError, Exception) as e:
        return {"verified": False, "_tool_error": str(e)[:200]}


registry.register(
    name="get_crypto_quote",
    description=(
        "Get the best Naira quote for buying or selling crypto across RampHub "
        "and Paj: rate, estimated output, token amount, fee. Read-only. Every "
        "figure comes from Go; never invent a rate."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "side": {**_SCHEMA_STRING, "description": "onramp|offramp|buy|sell"},
            "amount": {**_SCHEMA_MONEY, "description": "Amount in NGN"},
        },
        "required": ["side", "amount"],
    },
    category="funding",
    risk_level=RiskLevel.LOW,
    handler=_get_crypto_quote,
)

registry.register(
    name="get_funding_orders",
    description=(
        "List the user's recent NGN funding orders for one rail (paj or ramp): "
        "order id, type, status, fiat and token amounts, rate, fee. Read-only."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "kind": {**_SCHEMA_STRING, "description": "paj or ramp (default paj)"},
        },
    },
    category="funding",
    risk_level=RiskLevel.LOW,
    handler=_get_funding_orders,
)

registry.register(
    name="get_funding_order_status",
    description=(
        "Poll the live status of one NGN funding order by rail and id. "
        "Read-only. Report the returned status verbatim."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "kind": {**_SCHEMA_STRING, "description": "paj or ramp"},
            "order_id": {**_SCHEMA_STRING, "description": "orderId or transactionId"},
        },
        "required": ["kind", "order_id"],
    },
    category="funding",
    risk_level=RiskLevel.LOW,
    handler=_get_funding_order_status,
)

registry.register(
    name="get_paj_banks",
    description=(
        "List Paj banks, or the user's saved Paj bank accounts when saved is "
        "true. Read-only. Call before describing where an offramp would pay to."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "saved": {"type": "boolean", "description": "saved accounts when true"},
        },
    },
    category="funding",
    risk_level=RiskLevel.LOW,
    handler=_get_paj_banks,
)

registry.register(
    name="get_paj_verification_status",
    description=(
        "Check whether the user's Paj recipient is verified (saved Paj accounts "
        "imply verified). Read-only. An onramp needs verified before it starts."
    ),
    args_schema={"type": "object", "properties": {}},
    category="funding",
    risk_level=RiskLevel.LOW,
    handler=_get_paj_verification_status,
)


__all__ = ["registry"]

"""Tool definitions for Miriam Financial Agent.

Each tool maps to a capability the agent exposes. Read-only tools fetch
data (from Go backend) and analyze it (in Python). Money-movement tools
delegate to the Go backend after staged confirmation.

Handlers receive their declared args plus a ``_context`` dict injected by
the ToolRegistry containing at minimum ``user_id`` and ``token``.
"""

from typing import Any

from miriam_agent.agents.tools import RiskLevel, get_registry
from miriam_agent.integrations.go_client import get_go_client

T = Any  # placeholder, replaced with real Context protocol in wiring

_SCHEMA_STRING = {"type": "string"}
_SCHEMA_NUMBER = {"type": "number"}
_SCHEMA_INT = {"type": "integer", "minimum": 0}
_SCHEMA_BOOL = {"type": "boolean"}

registry = get_registry()


# ---------------------------------------------------------------------------
# Overview tools
# ---------------------------------------------------------------------------


async def _get_balance(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    data = await client.get_balances(ctx["token"])
    wallets = data.get("wallets") or data.get("balances") or []
    return {
        "balances": wallets,
        "summary": data,
    }


registry.register(
    name="get_balance",
    description="Get the user's current balances across spend and stash (yield) wallets.",
    args_schema={"type": "object", "properties": {}},
    category="overview",
    risk_level=RiskLevel.LOW,
    handler=_get_balance,
)


async def _get_transactions(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    transactions = await client.get_transactions(
        ctx["token"],
        limit=args.get("limit", 20),
        offset=args.get("offset", 0),
        category=args.get("category"),
    )
    return {"transactions": transactions, "count": len(transactions)}


registry.register(
    name="get_transactions",
    description="Get the user's recent financial transactions.",
    args_schema={
        "type": "object",
        "properties": {
            "limit": {
                **_SCHEMA_INT,
                "description": "Max transactions to return (default 20)",
            },
            "offset": {**_SCHEMA_INT, "description": "Pagination offset"},
            "category": {**_SCHEMA_STRING, "description": "Filter by category"},
        },
    },
    category="history",
    risk_level=RiskLevel.LOW,
    handler=_get_transactions,
)


async def _get_spending_summary(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    period = args.get("period", "month")
    data = await client.get_spending_summary(ctx["token"], period=period)
    return {"period": period, **data}


registry.register(
    name="get_spending_summary",
    description="Summarize the user's spending for a period, with category breakdown.",
    args_schema={
        "type": "object",
        "properties": {
            "period": {
                "type": "string",
                "enum": ["week", "month", "year"],
                "description": "Period to summarize",
            }
        },
    },
    category="spending",
    risk_level=RiskLevel.LOW,
    handler=_get_spending_summary,
)


async def _analyze_portfolio(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from miriam_agent.financial.intelligence import get_financial_intelligence_singleton

    fi = get_financial_intelligence_singleton(get_go_client())
    return await fi.analyze_portfolio(ctx["user_id"])


registry.register(
    name="analyze_portfolio",
    description="Analyze the user's investment portfolio: returns, risk, diversification, and recommendations.",
    args_schema={"type": "object", "properties": {}},
    category="overview",
    risk_level=RiskLevel.LOW,
    handler=_analyze_portfolio,
)


async def _get_financial_plan(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_financial_plan(ctx["token"])


registry.register(
    name="get_financial_plan",
    description="Get the user's current personalized financial plan.",
    args_schema={"type": "object", "properties": {}},
    category="planning",
    risk_level=RiskLevel.LOW,
    handler=_get_financial_plan,
)


# ---------------------------------------------------------------------------
# Money movement (staged for confirmation)
# ---------------------------------------------------------------------------


async def _send_money(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.send_money(
        token=ctx["token"],
        recipient=args["to"],
        amount=args["amount"],
        message=args.get("message"),
        idempotency_key=ctx.get("idempotency_key"),
    )


registry.register(
    name="send_money",
    description="Send money to a recipient via Rail tag, email, or phone. Requires confirmation.",
    args_schema={
        "type": "object",
        "properties": {
            "to": {
                **_SCHEMA_STRING,
                "description": "Recipient Rail tag, email, or phone",
            },
            "amount": {**_SCHEMA_NUMBER, "description": "Amount in user's currency"},
            "message": {**_SCHEMA_STRING, "description": "Optional note"},
        },
        "required": ["to", "amount"],
    },
    category="action",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_send_money,
)


async def _transfer_stash_to_spending(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.transfer_to_spending(
        ctx["token"], args["amount"], idempotency_key=ctx.get("idempotency_key")
    )


registry.register(
    name="transfer_stash_to_spending",
    description="Move money from the yield stash to the spend wallet. Requires confirmation.",
    args_schema={
        "type": "object",
        "properties": {"amount": {**_SCHEMA_NUMBER, "description": "Amount to move"}},
        "required": ["amount"],
    },
    category="action",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_transfer_stash_to_spending,
)


async def _transfer_spending_to_stash(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.transfer_to_stash(
        ctx["token"], args["amount"], idempotency_key=ctx.get("idempotency_key")
    )


registry.register(
    name="transfer_spending_to_stash",
    description="Move money from the spend wallet to the yield stash. Requires confirmation.",
    args_schema={
        "type": "object",
        "properties": {"amount": {**_SCHEMA_NUMBER, "description": "Amount to move"}},
        "required": ["amount"],
    },
    category="action",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_transfer_spending_to_stash,
)


async def _execute_investment(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.execute_investment(
        ctx["token"],
        symbol=args["symbol"],
        amount=args["amount"],
        side=args.get("side", "buy"),
        idempotency_key=ctx.get("idempotency_key"),
    )


registry.register(
    name="execute_investment",
    description="Buy or sell an investment. Capped and requires confirmation.",
    args_schema={
        "type": "object",
        "properties": {
            "symbol": {
                **_SCHEMA_STRING,
                "description": "Ticker symbol (e.g. AAPL, VOO)",
            },
            "amount": {**_SCHEMA_NUMBER, "description": "Dollar amount"},
            "side": {
                "type": "string",
                "enum": ["buy", "sell"],
                "description": "Trade direction",
            },
        },
        "required": ["symbol", "amount"],
    },
    category="investment",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_execute_investment,
)


# ---------------------------------------------------------------------------
# Advice & planning
# ---------------------------------------------------------------------------


async def _budget_advice(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from miriam_agent.financial.intelligence import get_financial_intelligence_singleton

    fi = get_financial_intelligence_singleton(get_go_client())
    return await fi.generate_budget_plan(
        ctx["user_id"], goal=args.get("goal", "balance")
    )


registry.register(
    name="budget_advice",
    description="Generate a personalized budget plan and actionable savings advice.",
    args_schema={
        "type": "object",
        "properties": {
            "goal": {
                **_SCHEMA_STRING,
                "description": "Budgeting goal: balance, emergency_fund, retirement, goal_based, debt_paydown, zero_based",
            }
        },
    },
    category="planning",
    risk_level=RiskLevel.LOW,
    handler=_budget_advice,
)


async def _search_memory(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from miriam_agent.conversational.supermemory_memory import SupermemoryMemory
    from miriam_agent.integrations.supermemory_client import (
        container_tag_for,
        get_supermemory_client,
    )

    client = get_supermemory_client()
    if client.enabled:
        try:
            sm = SupermemoryMemory(client)
            container_tag = container_tag_for(ctx["user_id"])
            results = await sm.search(
                container_tag, args["query"], limit=args.get("limit", 5)
            )
            return {"results": results}
        except Exception:
            pass  # fall through to local store

    from miriam_agent.database.memory import get_memory_singleton

    store = get_memory_singleton()
    results = await store.search_memories(
        ctx["user_id"], args["query"], limit=args.get("limit", 5)
    )
    return {
        "results": [
            {"type": m.type, "content": m.content, "extra_data": m.extra_data}
            for m in results
        ]
    }


registry.register(
    name="search_memory",
    description="Search the user's long-term financial memories, preferences, and past conversations.",
    args_schema={
        "type": "object",
        "properties": {
            "query": {**_SCHEMA_STRING, "description": "What to search for"},
            "limit": {**_SCHEMA_INT, "description": "Max results"},
        },
        "required": ["query"],
    },
    category="memory",
    risk_level=RiskLevel.LOW,
    handler=_search_memory,
)


def build_tool_registry() -> Any:
    """Return the fully-populated default registry."""
    return registry

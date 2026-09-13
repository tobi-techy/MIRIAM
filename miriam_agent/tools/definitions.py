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
    description=(
        "Get the user's current balances across spend and stash (yield) wallets."
    ),
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
    return await fi.analyze_portfolio(ctx["user_id"], token=ctx.get("token"))


registry.register(
    name="analyze_portfolio",
    description=(
        "Analyze the user's investment portfolio: returns, risk, "
        "diversification, and recommendations."
    ),
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
    description=(
        "Build a practical, personalized financial plan from the user's "
        "financial health score, cash-flow forecast, and financial profile "
        "(savings target, emergency fund, risk tolerance). Use when the user "
        "asks what they should do next or wants a plan."
    ),
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
    description=(
        "Send money to a recipient via Rail tag, email, or phone. "
        "Requires confirmation."
    ),
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
    description=(
        "Move money from the yield stash to the spend wallet. Requires confirmation."
    ),
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
    description=(
        "Move money from the spend wallet to the yield stash. Requires confirmation."
    ),
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
        ctx["user_id"], token=ctx.get("token"), goal=args.get("goal", "balance")
    )


registry.register(
    name="budget_advice",
    description="Generate a personalized budget plan and actionable savings advice.",
    args_schema={
        "type": "object",
        "properties": {
            "goal": {
                **_SCHEMA_STRING,
                "description": (
                    "Budgeting goal: balance, emergency_fund, retirement, "
                    "goal_based, debt_paydown, zero_based"
                ),
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
    description=(
        "Search the user's long-term financial memories, preferences, "
        "and past conversations."
    ),
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


# ---------------------------------------------------------------------------
# Lookups, automations, obligations, schedules
# ---------------------------------------------------------------------------


async def _lookup_recipient(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.lookup_recipient(ctx["token"], args["identifier"])


registry.register(
    name="lookup_recipient",
    description=(
        "Look up whether a Rail tag, email, or phone can receive money. "
        "Call before send_money when the user names someone. Does not move money."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "identifier": {
                **_SCHEMA_STRING,
                "description": "Rail tag (@name), email, or phone",
            }
        },
        "required": ["identifier"],
    },
    category="action",
    risk_level=RiskLevel.LOW,
    handler=_lookup_recipient,
)


async def _list_automations(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    automations = await client.list_automations(ctx["token"])
    return {"automations": automations, "count": len(automations)}


registry.register(
    name="list_automations",
    description="List the user's automations and scheduled money rules.",
    args_schema={"type": "object", "properties": {}},
    category="automation",
    risk_level=RiskLevel.LOW,
    handler=_list_automations,
)


_WEEKDAYS = {
    "sun": 0,
    "sunday": 0,
    "mon": 1,
    "monday": 1,
    "tue": 2,
    "tues": 2,
    "tuesday": 2,
    "wed": 3,
    "wednesday": 3,
    "thu": 4,
    "thur": 4,
    "thurs": 4,
    "thursday": 4,
    "fri": 5,
    "friday": 5,
    "sat": 6,
    "saturday": 6,
}


def _automation_payload(args: dict[str, Any]) -> dict[str, Any]:
    """Map chat-friendly args onto Go's CreateAutomationRequest."""
    trigger_type = args.get("trigger_type") or "schedule"
    action_type = args.get("action_type")
    dest = str(args.get("to") or "").lower()
    if not action_type:
        if dest in {"stash", "yield"}:
            action_type = "transfer_to_stash"
        elif dest in {"spend", "spending"}:
            action_type = "transfer_to_spend"
        else:
            action_type = "transfer_to_stash"

    trigger_config = args.get("trigger_config")
    if not isinstance(trigger_config, dict):
        trigger_config = {"hour": 9, "weekdays": [5]}
        schedule = str(args.get("schedule") or "").lower()
        for name, num in _WEEKDAYS.items():
            if name in schedule:
                trigger_config["weekdays"] = [num]
                break
        if "month" in schedule:
            trigger_config = {"cron": "0 9 1 * *"}

    action_config = dict(args.get("action_config") or {})
    if args.get("amount") is not None:
        try:
            action_config.setdefault("amount", float(args["amount"]))
        except (TypeError, ValueError):
            action_config.setdefault("amount", args["amount"])
    if args.get("from"):
        action_config.setdefault("from_wallet", args["from"])
    if args.get("to"):
        action_config.setdefault("to_wallet", args["to"])

    payload: dict[str, Any] = {
        "name": args["name"],
        "trigger_type": trigger_type,
        "trigger_config": trigger_config,
        "action_type": action_type,
        "action_config": action_config,
    }
    if args.get("description"):
        payload["description"] = args["description"]
    return payload


async def _create_automation(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.create_automation(
        ctx["token"],
        _automation_payload(args),
        idempotency_key=ctx.get("idempotency_key"),
    )


registry.register(
    name="create_automation",
    description=(
        "Create a lasting automation (e.g. every Friday move $50 spend to stash). "
        "Requires confirmation. Future runs happen without another chat."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "name": {**_SCHEMA_STRING, "description": "Automation name"},
            "schedule": {
                **_SCHEMA_STRING,
                "description": "Human schedule, e.g. 'every Friday'",
            },
            "amount": {**_SCHEMA_NUMBER, "description": "Amount for transfers"},
            "from": {**_SCHEMA_STRING, "description": "Source wallet: spend or stash"},
            "to": {**_SCHEMA_STRING, "description": "Destination: spend or stash"},
            "trigger_type": {
                **_SCHEMA_STRING,
                "description": "schedule | balance_threshold | obligation_due | ...",
            },
            "action_type": {
                **_SCHEMA_STRING,
                "description": "transfer_to_stash | transfer_to_spend | notify | ...",
            },
            "description": {**_SCHEMA_STRING, "description": "Optional description"},
        },
        "required": ["name"],
    },
    category="automation",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_create_automation,
)


async def _update_automation(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    payload: dict[str, Any] = {}
    if "is_active" in args:
        payload["is_active"] = args["is_active"]
    if args.get("name"):
        payload["name"] = args["name"]
    return await client.update_automation(ctx["token"], args["id"], payload)


registry.register(
    name="update_automation",
    description="Pause, resume, or rename an automation. Requires confirmation.",
    args_schema={
        "type": "object",
        "properties": {
            "id": {**_SCHEMA_STRING, "description": "Automation id"},
            "is_active": {**_SCHEMA_BOOL, "description": "false pauses, true resumes"},
            "name": {**_SCHEMA_STRING, "description": "New name"},
        },
        "required": ["id"],
    },
    category="automation",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_update_automation,
)


async def _delete_automation(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.delete_automation(ctx["token"], args["id"])


registry.register(
    name="delete_automation",
    description="Delete an automation permanently. Requires confirmation.",
    args_schema={
        "type": "object",
        "properties": {"id": {**_SCHEMA_STRING, "description": "Automation id"}},
        "required": ["id"],
    },
    category="automation",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_delete_automation,
)


async def _list_obligations(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    obligations = await client.get_upcoming_bills(ctx["token"])
    return {"obligations": obligations, "count": len(obligations)}


registry.register(
    name="list_obligations",
    description="List the user's bills, debts, and other financial obligations.",
    args_schema={"type": "object", "properties": {}},
    category="planning",
    risk_level=RiskLevel.LOW,
    handler=_list_obligations,
)


async def _create_obligation(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    payload = {
        "type": args.get("type", "bill"),
        "name": args["name"],
        "amount": str(args["amount"]),
        "currency": args.get("currency", "USD"),
        "cadence": args.get("cadence", "monthly"),
    }
    if args.get("due_day") is not None:
        payload["due_day"] = args["due_day"]
    if args.get("counterparty"):
        payload["counterparty"] = args["counterparty"]
    return await client.create_obligation(ctx["token"], payload)


registry.register(
    name="create_obligation",
    description="Add a bill, debt, or other obligation Miriam should track.",
    args_schema={
        "type": "object",
        "properties": {
            "name": {**_SCHEMA_STRING, "description": "Obligation name"},
            "amount": {**_SCHEMA_NUMBER, "description": "Amount due"},
            "type": {**_SCHEMA_STRING, "description": "bill, debt, subscription, ..."},
            "cadence": {**_SCHEMA_STRING, "description": "monthly, weekly, once, ..."},
            "currency": {**_SCHEMA_STRING, "description": "USD, NGN, ..."},
            "due_day": {**_SCHEMA_INT, "description": "Day of month due"},
            "counterparty": {**_SCHEMA_STRING, "description": "Who it is paid to"},
        },
        "required": ["name", "amount"],
    },
    category="planning",
    risk_level=RiskLevel.LOW,
    handler=_create_obligation,
)


async def _mark_obligation_paid(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.update_obligation(ctx["token"], args["id"], {"status": "paid"})


registry.register(
    name="mark_obligation_paid",
    description="Mark an obligation as paid.",
    args_schema={
        "type": "object",
        "properties": {"id": {**_SCHEMA_STRING, "description": "Obligation id"}},
        "required": ["id"],
    },
    category="planning",
    risk_level=RiskLevel.LOW,
    handler=_mark_obligation_paid,
)


async def _list_scheduled_investments(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    items = await client.list_scheduled_investments(ctx["token"])
    return {"scheduled_investments": items, "count": len(items)}


registry.register(
    name="list_scheduled_investments",
    description="List recurring investment schedules.",
    args_schema={"type": "object", "properties": {}},
    category="investment",
    risk_level=RiskLevel.LOW,
    handler=_list_scheduled_investments,
)


async def _create_scheduled_investment(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    payload = {
        "symbol": args["symbol"],
        "amount": str(args["amount"]),
        "frequency": args.get("frequency", "weekly"),
    }
    if args.get("name"):
        payload["name"] = args["name"]
    if args.get("day_of_week") is not None:
        payload["day_of_week"] = args["day_of_week"]
    if args.get("day_of_month") is not None:
        payload["day_of_month"] = args["day_of_month"]
    return await client.create_scheduled_investment(ctx["token"], payload)


registry.register(
    name="create_scheduled_investment",
    description="Create a recurring investment. Requires confirmation.",
    args_schema={
        "type": "object",
        "properties": {
            "symbol": {**_SCHEMA_STRING, "description": "Ticker, e.g. VOO"},
            "amount": {**_SCHEMA_NUMBER, "description": "Dollar amount each run"},
            "frequency": {**_SCHEMA_STRING, "description": "daily, weekly, monthly"},
            "name": {**_SCHEMA_STRING, "description": "Optional label"},
            "day_of_week": {**_SCHEMA_INT, "description": "0=Sun .. 6=Sat"},
            "day_of_month": {**_SCHEMA_INT, "description": "1-28 for monthly"},
        },
        "required": ["symbol", "amount"],
    },
    category="investment",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_create_scheduled_investment,
)


async def _pause_scheduled_investment(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.pause_scheduled_investment(ctx["token"], args["id"])


registry.register(
    name="pause_scheduled_investment",
    description="Pause a recurring investment. Requires confirmation.",
    args_schema={
        "type": "object",
        "properties": {"id": {**_SCHEMA_STRING, "description": "Schedule id"}},
        "required": ["id"],
    },
    category="investment",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_pause_scheduled_investment,
)


async def _resume_scheduled_investment(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.resume_scheduled_investment(ctx["token"], args["id"])


registry.register(
    name="resume_scheduled_investment",
    description="Resume a paused recurring investment. Requires confirmation.",
    args_schema={
        "type": "object",
        "properties": {"id": {**_SCHEMA_STRING, "description": "Schedule id"}},
        "required": ["id"],
    },
    category="investment",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_resume_scheduled_investment,
)


async def _list_bill_beneficiaries(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    items = await client.list_bill_beneficiaries(
        ctx["token"], category=args.get("category")
    )
    return {"beneficiaries": items, "count": len(items)}


registry.register(
    name="list_bill_beneficiaries",
    description="List saved bill-payment beneficiaries (airtime, electricity, etc.).",
    args_schema={
        "type": "object",
        "properties": {
            "category": {**_SCHEMA_STRING, "description": "Optional category filter"}
        },
    },
    category="action",
    risk_level=RiskLevel.LOW,
    handler=_list_bill_beneficiaries,
)


async def _save_bill_beneficiary(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    payload = {
        "category": args["category"],
        "recipient": args["recipient"],
    }
    if args.get("name"):
        payload["name"] = args["name"]
    return await client.save_bill_beneficiary(ctx["token"], payload)


registry.register(
    name="save_bill_beneficiary",
    description="Save a bill-payment beneficiary so future payments are one step.",
    args_schema={
        "type": "object",
        "properties": {
            "category": {
                **_SCHEMA_STRING,
                "description": "airtime, data, electricity, cable, ...",
            },
            "recipient": {
                **_SCHEMA_STRING,
                "description": "Phone, meter, or smartcard",
            },
            "name": {**_SCHEMA_STRING, "description": "Optional display name"},
        },
        "required": ["category", "recipient"],
    },
    category="action",
    risk_level=RiskLevel.LOW,
    handler=_save_bill_beneficiary,
)


async def _list_bill_providers(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    items = await client.list_bill_providers(
        ctx["token"], args["category"], network_id=args.get("network_id")
    )
    return {"providers": items, "count": len(items)}


registry.register(
    name="list_bill_providers",
    description=(
        "List bill-payment providers/plans for a category "
        "(electricity, cable, data, ...)."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "category": {
                **_SCHEMA_STRING,
                "description": "electricity, cable, data, ...",
            },
            "network_id": {
                **_SCHEMA_STRING,
                "description": "Network ID (01..04) to filter data plans",
            },
        },
        "required": ["category"],
    },
    category="action",
    risk_level=RiskLevel.LOW,
    handler=_list_bill_providers,
)


async def _list_data_plans(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    items = await client.get_data_plans(ctx["token"], network_id=args.get("network_id"))
    return {"plans": items, "count": len(items)}


registry.register(
    name="list_data_plans",
    description="List mobile data plans, optionally for a specific network.",
    args_schema={
        "type": "object",
        "properties": {
            "network_id": {**_SCHEMA_STRING, "description": "Network ID (01..04)"}
        },
    },
    category="action",
    risk_level=RiskLevel.LOW,
    handler=_list_data_plans,
)


async def _list_cable_packages(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    items = await client.get_cable_packages(ctx["token"])
    return {"packages": items, "count": len(items)}


registry.register(
    name="list_cable_packages",
    description="List cable TV bouquets and packages.",
    args_schema={"type": "object", "properties": {}},
    category="action",
    risk_level=RiskLevel.LOW,
    handler=_list_cable_packages,
)


async def _detect_network(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.detect_network(ctx["token"], args["phone"])


registry.register(
    name="detect_network",
    description="Detect a phone number's mobile network (for airtime or data).",
    args_schema={
        "type": "object",
        "properties": {
            "phone": {**_SCHEMA_STRING, "description": "Phone number to check"}
        },
        "required": ["phone"],
    },
    category="action",
    risk_level=RiskLevel.LOW,
    handler=_detect_network,
)


async def _validate_meter(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    return await client.validate_meter(ctx["token"], args["meter_no"], args["elect_id"])


registry.register(
    name="validate_meter",
    description="Validate an electricity meter number and return the account name.",
    args_schema={
        "type": "object",
        "properties": {
            "meter_no": {**_SCHEMA_STRING, "description": "Meter number"},
            "elect_id": {**_SCHEMA_STRING, "description": "Electricity disco ID"},
        },
        "required": ["meter_no", "elect_id"],
    },
    category="action",
    risk_level=RiskLevel.LOW,
    handler=_validate_meter,
)


async def _get_bill_payment_history(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    items = await client.get_bill_payment_history(ctx["token"])
    return {"history": items, "count": len(items)}


registry.register(
    name="get_bill_payment_history",
    description="List the user's recent bill-payment receipts.",
    args_schema={"type": "object", "properties": {}},
    category="history",
    risk_level=RiskLevel.LOW,
    handler=_get_bill_payment_history,
)


async def _pay_bill(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    client = get_go_client()
    payload: dict[str, Any] = {
        "category": args["category"],
        "recipient": args["recipient"],
        "amount_ngn": args["amount_ngn"],
    }
    for key in ("network_id", "prod_id", "elect_id", "recipient_name"):
        if args.get(key):
            payload[key] = args[key]
    return await client.pay_bill(
        ctx["token"], payload, idempotency_key=ctx.get("idempotency_key")
    )


registry.register(
    name="pay_bill",
    description=(
        "Pay a bill (airtime, data, electricity, cable) for the user. "
        "Requires confirmation."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["airtime", "data", "electricity", "cable"],
                "description": "Bill category",
            },
            "recipient": {
                **_SCHEMA_STRING,
                "description": "Phone, meter, or smartcard number to pay for",
            },
            "amount_ngn": {
                **_SCHEMA_NUMBER,
                "description": "Face value in NGN",
            },
            "network_id": {
                **_SCHEMA_STRING,
                "description": (
                    "Network ID (01..04) for airtime/data; auto-detected if omitted"
                ),
            },
            "prod_id": {
                **_SCHEMA_STRING,
                "description": "Plan/provider ID from a bill provider lookup",
            },
            "elect_id": {
                **_SCHEMA_STRING,
                "description": "Electricity disco ID from a provider lookup",
            },
            "recipient_name": {
                **_SCHEMA_STRING,
                "description": "Validated meter/account holder name, if known",
            },
        },
        "required": ["category", "recipient", "amount_ngn"],
    },
    category="action",
    risk_level=RiskLevel.HIGH,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_pay_bill,
)


async def _get_cash_flow_forecast(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_cash_flow_forecast(ctx["token"])


registry.register(
    name="get_cash_flow_forecast",
    description=(
        "Forecast the user's end-of-month balance and safe daily spend from "
        "month-to-date income/spending, recurring expenses, current balances, "
        "and budget. Use when the user asks 'can I afford this', will they run "
        "out of money, or what they can safely spend per day."
    ),
    args_schema={"type": "object", "properties": {}},
    category="planning",
    risk_level=RiskLevel.LOW,
    handler=_get_cash_flow_forecast,
)


async def _get_financial_health(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    client = get_go_client()
    return await client.get_financial_health(
        ctx["token"], period=args.get("period", "last_90_days")
    )


registry.register(
    name="get_financial_health",
    description=(
        "Calculate the user's financial health score from balances, savings "
        "rate, budget progress, cash flow, and profile targets. Use for 'how "
        "am I doing', financial score, financial health, or progress-check "
        "questions. Supports multi-period analysis; use last_6_months or "
        "last_12_months for long-term health trends."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "period": {
                "type": "string",
                "enum": [
                    "last_90_days",
                    "last_6_months",
                    "last_12_months",
                    "this_month",
                    "last_month",
                ],
                "description": (
                    "Time period to analyze. Default to last_90_days for a "
                    "comprehensive view."
                ),
            }
        },
    },
    category="planning",
    risk_level=RiskLevel.LOW,
    handler=_get_financial_health,
)


def build_tool_registry() -> Any:
    """Return the fully-populated default registry."""
    return registry

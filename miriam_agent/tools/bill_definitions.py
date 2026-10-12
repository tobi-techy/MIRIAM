"""Bill-payment and deposit tool definitions.

Split out of :mod:`miriam_agent.tools.definitions` for the blob ratchet; the
handlers and their registrations are unchanged. Importing this module registers
the tools on the shared registry (the same singleton ``definitions`` uses).
"""

from __future__ import annotations

from typing import Any

from miriam_agent.agents.tools import RiskLevel, get_registry
from miriam_agent.integrations.go_client import get_go_client
from miriam_agent.tools.schemas import (
    _SCHEMA_MONEY,
    _SCHEMA_STRING,
)

registry = get_registry()


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
    description=(
        "Save a bill-payment beneficiary so future payments are one step. "
        "Requires confirmation."
    ),
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
    risk_level=RiskLevel.MEDIUM,
    is_mutation=True,
    requires_approval=True,
    allow_auto_execute=False,
    handler=_save_bill_beneficiary,
)


async def _get_deposit_details(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Return the user's Naira deposit / bank-transfer details.

    Tries the NGN virtual account first (the account the user sends Naira
    to), then falls back to the crypto deposit-address endpoint so the tool
    always answers with real backend data. Tool errors surface as a
    structured ``_tool_error`` payload (never a fake account), and the
    handler raises nothing on its own: auth, transport, and backend-shape
    failures are all reported, never hidden.
    """
    client = get_go_client()
    token = ctx.get("token")
    if not token:
        return {
            "_tool_error": "missing auth token",
            "hint": "Sign in again, then ask for the deposit account.",
        }
    errors: list[str] = []
    try:
        ngn = await client.get_ngn_virtual_account(token)
    except Exception as e:  # noqa: BLE001 - surfaced below, never swallowed
        ngn = None
        errors.append(f"naira account lookup failed: {e}")
    if isinstance(ngn, dict):
        account = ngn.get("virtual_account")
        if isinstance(account, dict) and account:
            return {
                "currency": "NGN",
                "rail": "bank_transfer",
                "virtual_account": account,
                "how_to_use": (
                    "Send Naira to this account from any Nigerian bank app. "
                    "It lands in your Rail balance."
                ),
            }
        if ngn.get("_tool_error"):
            errors.append(str(ngn["_tool_error"]))
    crypto: dict[str, Any] | None = None
    try:
        crypto = await client.create_deposit_address(
            token, chain=(args.get("chain") or "base"), currency="USDC"
        )
    except Exception as e:  # noqa: BLE001 - surfaced below, never swallowed
        crypto = None
        errors.append(f"crypto deposit lookup failed: {e}")
    if isinstance(crypto, dict) and crypto.get("_tool_error"):
        errors.append(str(crypto["_tool_error"]))
    if isinstance(crypto, dict) and (
        crypto.get("address") or crypto.get("deposit_address")
    ):
        return {
            "currency": crypto.get("currency") or "USDC",
            "rail": "crypto",
            "deposit": crypto,
            "note": (
                "No Naira bank-transfer account is available on this account "
                "right now. This crypto deposit address works instead."
            ),
            **({"lookup_notes": errors} if errors else {}),
        }
    detail = "; ".join(errors) if errors else "no deposit details came back"
    return {
        "_tool_error": detail,
        "hint": (
            "The deposit service did not return an account. Try again in a "
            "moment, or fund from the Rail app home screen."
        ),
    }


registry.register(
    name="get_deposit_details",
    description=(
        "Get the user's deposit details: their Naira bank-transfer virtual "
        "account (bank name, account number, account name) and, as a "
        "fallback, a crypto deposit address. Call whenever the user asks "
        "how to deposit, fund, add money, or asks for the account to send "
        "Naira to. Answers come from the live backend, never invented."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "chain": {
                **_SCHEMA_STRING,
                "description": "Crypto fallback chain (default base)",
            },
        },
    },
    category="action",
    risk_level=RiskLevel.LOW,
    handler=_get_deposit_details,
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
    token = ctx.get("token")
    if not token:
        return {
            "_tool_error": "missing auth token",
            "hint": "Sign in again, then retry the airtime lookup.",
        }
    phone = str(args.get("phone") or "").strip()
    if not phone:
        return {
            "_tool_error": "phone is required",
            "hint": "Ask the user for the phone number to top up.",
        }
    try:
        result = await client.detect_network(token, phone)
    except Exception as e:  # noqa: BLE001 - structured, never swallowed
        return {
            "_tool_error": f"network lookup failed: {e}",
            "phone": phone,
            "hint": (
                "The bill service did not answer. The user can still pick "
                "their network manually (MTN, Glo, 9mobile, Airtel)."
            ),
        }
    if not isinstance(result, dict) or (
        not result.get("network_id") and not result.get("network")
    ):
        if isinstance(result, dict) and result.get("_tool_error"):
            return {**result, "phone": phone}
        return {
            "_tool_error": "network lookup returned no network",
            "phone": phone,
            "raw": result,
            "hint": (
                "The bill service returned nothing for that number. Ask the "
                "user to pick their network manually."
            ),
        }
    out = dict(result)
    out.setdefault("phone", phone)
    return out


registry.register(
    name="detect_network",
    description=(
        "Detect a phone number's mobile network for airtime or data "
        "(returns network_id and network name). Call this as soon as the "
        "user gives a phone number for airtime or data, before asking for "
        "the network or giving up."
    ),
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
                **_SCHEMA_MONEY,
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

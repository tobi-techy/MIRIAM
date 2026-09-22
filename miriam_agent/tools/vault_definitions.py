"""Retirement vault read tools (Sleeve A: locked dollar retirement vault).

Rail owns the mix (YAML) and the lot engine. Miriam runs the USER PLAN:
tier label, retirement age, vault percent, spendable floor. She never picks
assets, never names providers, never authors strategies, never debits lots.

Four read-only tools, all auto-execute, all LOW:

- ``get_vault_context``: Go truth about the vault, or ``{exists: False}``.
- ``preview_plan``: local arithmetic on the last inflow. No provider call.
- ``preview_withdraw``: Go preview numbers, passed through unchanged.
- ``propose_vault_plan``: a staged draft + spoken facts + the exact envelope
  the app will POST to Go after the user confirms there. No POST here.

There are no mutation tools in this module on purpose. Execution is the app
via Go confirm + passcode. Importing this module registers the tools on the
shared singleton registry.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from miriam_agent.agents.tools import RiskLevel, get_registry
from miriam_agent.core.exceptions import IntegrationError
from miriam_agent.money.vault_copy import (
    NOT_LIVE_LINE,
    assert_known_tier,
    check_vault_copy,
    tier_risk_line,
)

registry = get_registry()

_SCHEMA_OBJECT = {"type": "object"}
_SCHEMA_STRING = {"type": "string"}
_SCHEMA_MONEY = {"type": "number", "exclusiveMinimum": 0}
_SCHEMA_PCT = {"type": "number", "minimum": 0, "maximum": 100}
_SCHEMA_AGE = {"type": "integer", "minimum": 18, "maximum": 100}

_NOT_LIVE_SPOKEN = (
    "Your locked dollar sleeve is not set up, and "
    + NOT_LIVE_LINE
    + " I have not drafted anything."
)


def _token(ctx: dict[str, Any]) -> str | None:
    token = ctx.get("token")
    return token if isinstance(token, str) and token else None


def _unlock_date(retirement_age: int, today: date | None = None) -> str:
    return str((today or date.today()).year + max(retirement_age - 30, 0))


async def _get_money_plan_vault_view(ctx: dict[str, Any]) -> dict[str, Any] | None:
    token = _token(ctx)
    if not token:
        return None
    try:
        from miriam_agent.integrations.go_client import get_go_client

        return await get_go_client().get_vault(token)
    except (IntegrationError, Exception):
        return None


async def _get_vault_context(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    token = _token(ctx)
    if not token:
        out: dict[str, Any] = {"exists": False, "_tool_error": "no user token"}
        return out
    from miriam_agent.integrations.go_client import get_go_client

    client = get_go_client()
    try:
        vault = await client.get_vault(token)
    except (IntegrationError, Exception) as e:
        return {"exists": False, "_tool_error": str(e)[:200]}
    if not vault or vault.get("exists") is False:
        return {"exists": False}
    try:
        activity = await client.get_vault_activity(token, limit=5)
    except (IntegrationError, Exception):
        activity = []
    try:
        strategies = await client.list_vault_strategies(token)
    except (IntegrationError, Exception):
        strategies = {}
    plan_available = True
    if isinstance(strategies, dict):
        status = str(strategies.get("status") or "").lower()
        if status in ("unseeded", "unavailable", "empty"):
            plan_available = False
        items = strategies.get("strategies")
        if isinstance(items, list) and not items:
            plan_available = False
    lots: list[Any] = []
    raw_lots = vault.get("lots") or vault.get("last_lots")
    if isinstance(raw_lots, list):
        lots = raw_lots[-5:]
    out = {
        "exists": True,
        "plan": vault.get("plan"),
        "principal": vault.get("principal"),
        "earnings": vault.get("earnings"),
        "total": vault.get("total"),
        "unlock_at": vault.get("unlock_at") or vault.get("unlock_date"),
        "penalty_if_withdraw_today": vault.get("penalty_if_withdraw_today")
        or vault.get("penalty"),
        "auto_pct": vault.get("auto_pct") or vault.get("vault_pct"),
        "tier_label": vault.get("tier") or vault.get("tier_label"),
        "status": vault.get("status"),
        "health": vault.get("health"),
        "skip": vault.get("skip"),
        "plan_available": plan_available,
        "last_lots": lots,
        "recent_activity": activity if isinstance(activity, list) else [],
    }
    if not plan_available:
        out["spoken"] = _NOT_LIVE_SPOKEN
        check_vault_copy(out["spoken"])
    return out


async def _preview_plan(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    tier = assert_known_tier(str(args.get("tier") or ""))
    retirement_age = int(args.get("retirement_age"))
    vault_pct = float(args.get("vault_pct"))
    inflow = float(args.get("sample_inflow"))
    if not (0 < vault_pct <= 100):
        raise ValueError("vault_pct must be between 0 and 100")
    if inflow <= 0:
        raise ValueError("sample_inflow must be positive")
    to_vault = round(inflow * vault_pct / 100, 2)
    left_spendable = round(inflow - to_vault, 2)
    unlock = _unlock_date(retirement_age)
    risk = tier_risk_line(tier)
    spoken = (
        f"On your last inflow of {inflow:g}, {to_vault:g} would lock "
        f"({tier}, {vault_pct:g} percent). {left_spendable:g} stays spendable. "
        f"Unlock {unlock}. {risk}"
    )
    check_vault_copy(spoken)
    return {
        "tier": tier,
        "retirement_age": retirement_age,
        "vault_pct": vault_pct,
        "to_vault": to_vault,
        "left_spendable": left_spendable,
        "unlock_date": unlock,
        "tier_risk": risk,
        "spoken": spoken,
    }


async def _preview_withdraw(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    token = _token(ctx)
    if not token:
        return {"_tool_error": "no user token"}
    amount = float(args.get("amount"))
    from miriam_agent.integrations.go_client import get_go_client

    preview = await get_go_client().preview_vault_withdraw(token, amount)
    if not isinstance(preview, dict):
        return {"_tool_error": "unexpected vault preview shape"}
    return {
        "principal_out": preview.get("principal_out"),
        "earnings_out": preview.get("earnings_out"),
        "penalty": preview.get("penalty"),
        "payout": preview.get("payout"),
        "qualified": preview.get("qualified"),
        "unlock_at": preview.get("unlock_at"),
        "raw": preview,
    }


async def _propose_vault_plan(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    tier = assert_known_tier(str(args.get("tier") or ""))
    retirement_age = int(args.get("retirement_age"))
    vault_pct = float(args.get("vault_pct"))
    floor = args.get("floor")
    if not (0 < vault_pct <= 100):
        raise ValueError("vault_pct must be between 0 and 100")
    vault_view = await _get_money_plan_vault_view(ctx)
    plan_available = True
    if isinstance(vault_view, dict):
        strategies_status = str(vault_view.get("strategies_status") or "").lower()
        if strategies_status in ("unseeded", "unavailable", "empty"):
            plan_available = False
    if plan_available:
        try:
            from miriam_agent.integrations.go_client import get_go_client

            token = _token(ctx)
            if token:
                strategies = await get_go_client().list_vault_strategies(token)
                if isinstance(strategies, dict):
                    status = str(strategies.get("status") or "").lower()
                    if status in ("unseeded", "unavailable", "empty"):
                        plan_available = False
                    items = strategies.get("strategies")
                    if isinstance(items, list) and not items:
                        plan_available = False
        except (IntegrationError, Exception):
            pass
    if not plan_available:
        return {"staged": False, "spoken": _NOT_LIVE_SPOKEN}
    unlock = _unlock_date(retirement_age)
    facts = [
        "Locked dollar retirement sleeve.",
        "Deposits can come out.",
        "Growth before unlock costs 10% of the growth taken.",
        f"Opens on {unlock}.",
    ]
    draft = {
        "tier": tier,
        "retirement_age": retirement_age,
        "vault_pct": vault_pct,
        "floor": floor,
        "unlock_date": unlock,
    }
    envelope = {
        "path": "/api/v1/vault",
        "method": "POST",
        "payload": {
            "tier": tier,
            "retirement_age": retirement_age,
            "vault_pct": vault_pct,
            **({"floor": floor} if floor is not None else {}),
        },
        "needs": ["app_confirm", "passcode", "confirmation_token"],
    }
    spoken = " ".join(facts) + (
        f" I propose {tier} at {vault_pct:g} percent, opening {unlock}. "
        "Approve in the app and the locked sleeve opens."
    )
    check_vault_copy(spoken)
    for fact in facts:
        check_vault_copy(fact)
    return {
        "staged": True,
        "draft": draft,
        "spoken_four_facts": facts,
        "spoken": spoken,
        "vault_envelope": envelope,
    }


registry.register(
    name="get_vault_context",
    description=(
        "Read the user's locked dollar retirement sleeve as Go reports it: plan, "
        "principal, earnings, total, unlock date, penalty if withdrawn today, "
        "auto percent, tier label, status, health, last lots. Call before any "
        "vault claim. Empty vault returns exists false. Never invents a balance."
    ),
    args_schema={"type": "object", "properties": {}},
    category="vault",
    risk_level=RiskLevel.LOW,
    handler=_get_vault_context,
)

registry.register(
    name="preview_plan",
    description=(
        "Local arithmetic for a locked dollar proposal: how much of a sample "
        "inflow would lock at a tier and percent, how much stays spendable, the "
        "unlock date, and a one-line tier risk. No provider call. Tiers are "
        "Steady, Balanced, or Growth labels only."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "tier": {**_SCHEMA_STRING, "description": "Steady, Balanced, or Growth"},
            "retirement_age": {**_SCHEMA_AGE, "description": "Age the sleeve opens"},
            "vault_pct": {**_SCHEMA_PCT, "description": "Percent of inflow to lock"},
            "sample_inflow": {**_SCHEMA_MONEY, "description": "Sample inflow amount"},
        },
        "required": ["tier", "retirement_age", "vault_pct", "sample_inflow"],
    },
    category="vault",
    risk_level=RiskLevel.LOW,
    handler=_preview_plan,
)

registry.register(
    name="preview_withdraw",
    description=(
        "Go-computed early withdrawal math for an amount: principal out, "
        "earnings out, penalty, payout, qualified. Call before talking a number "
        "about taking money out. Numbers come from Go unchanged."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "amount": {**_SCHEMA_MONEY, "description": "Amount to take out"},
        },
        "required": ["amount"],
    },
    category="vault",
    risk_level=RiskLevel.LOW,
    handler=_preview_withdraw,
)

registry.register(
    name="propose_vault_plan",
    description=(
        "Stage a locked dollar proposal for the app to confirm: tier, "
        "retirement age, vault percent, spendable floor. Returns the draft, the "
        "four spoken facts, and the envelope the app posts to Go after the user "
        "confirms there. Stages only, never moves money."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "tier": {**_SCHEMA_STRING, "description": "Steady, Balanced, or Growth"},
            "retirement_age": {**_SCHEMA_AGE, "description": "Age the sleeve opens"},
            "vault_pct": {**_SCHEMA_PCT, "description": "Percent of inflow to lock"},
            "floor": {**_SCHEMA_OBJECT, "description": "Optional spendable floor"},
        },
        "required": ["tier", "retirement_age", "vault_pct"],
    },
    category="vault",
    risk_level=RiskLevel.LOW,
    handler=_propose_vault_plan,
)


__all__ = ["registry"]

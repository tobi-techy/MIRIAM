"""Money plan tool: the one math door, exposed to the model.

Miriam cannot answer a money question without this. The rule in the operator
prompt is "before any money claim, call `get_money_plan`", and this is the tool
that satisfies it. It is read-only: no mutation, no approval, no money movement.

Everything it returns was computed by ``miriam_agent.money``. The model may
rewrite the wording of the diagnosis and the actions; it cannot produce a
different split, book, or Glider decision, because it never sees the inputs.

Connected balances come from the Go ledger through the existing
``get_financial_health`` projection rather than by re-parsing raw payloads, so
the shape stays owned in one place. If the backend is unreachable the tool
degrades to whatever the model supplied, and the plan honestly reports a data
gap instead of inventing a balance.
"""

from __future__ import annotations

import logging
from typing import Any

from miriam_agent.agents.tools import RiskLevel, get_registry
from miriam_agent.core.exceptions import IntegrationError
from miriam_agent.money.intake import AccountsSnapshot
from miriam_agent.money.plan import (
    GliderState,
    build_money_plan,
    render_plan,
    render_spoken,
)

logger = logging.getLogger(__name__)

registry = get_registry()

_SCHEMA_OBJECT = {"type": "object"}


async def _connected_balances(ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Cash and investments from the ledger, or ``None`` when unavailable.

    Fails open on purpose: a missing connection must not break the turn, it must
    produce a plan that says a number is missing.
    """
    token = ctx.get("token")
    if not token:
        return None
    try:
        from miriam_agent.financial.intelligence import financial_health_live
        from miriam_agent.integrations.go_client import get_go_client

        health = await financial_health_live(get_go_client(), token)
    except (IntegrationError, Exception) as e:  # noqa: BLE001
        logger.info("money plan: connected balances unavailable: %s", e)
        return None

    if not isinstance(health, dict):
        return None
    spend = health.get("spend_balance")
    stash = health.get("stash_balance")
    total = health.get("total_balance")
    cash = total if total is not None else _sum(spend, stash)
    if cash is None:
        return None
    return {
        "cash": str(cash),
        "currency": health.get("primary_currency"),
        "source": "ledger",
    }


def _sum(*values: Any) -> float | None:
    present = [float(v) for v in values if v is not None]
    return sum(present) if present else None


def _merge_accounts(supplied: Any, connected: dict[str, Any]) -> dict[str, Any]:
    """The ledger wins over anything stated by hand, field by field."""
    merged: dict[str, Any] = dict(supplied) if isinstance(supplied, dict) else {}
    merged["cash"] = connected.get("cash")
    merged["source"] = connected.get("source", "ledger")
    if connected.get("currency"):
        merged["currency"] = connected["currency"]
    return merged


async def _vault_view(ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Connected vault view from Go, or ``None`` when unavailable.

    Fails open like balances: a missing vault read must not break the turn,
    it must produce a plan that says what is missing.
    """
    token = ctx.get("token")
    if not token:
        return None
    try:
        from miriam_agent.integrations.go_client import get_go_client

        return await get_go_client().get_vault(token)
    except (IntegrationError, Exception) as e:  # noqa: BLE001
        logger.info("money plan: connected vault unavailable: %s", e)
        return None


def _vault_state(view: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalise a Go vault view into planner ``vault_state``.

    Active means Go reports an open vault. Anything else (no vault, 404
    sentinel, error shape) is no-vault, so the planner may still propose
    activating one of the three Rail tiers.
    """
    if not isinstance(view, dict):
        return None
    if view.get("exists") is False:
        return None
    status = str(view.get("status") or "").lower()
    if status in ("", "none", "closed", "empty"):
        if view.get("total") is None and view.get("principal") is None:
            return None
    return {
        "active": True,
        "tier_label": str(view.get("tier") or view.get("tier_label") or ""),
        "vault_pct": view.get("auto_pct") or view.get("vault_pct") or 0,
        "unlock_date": str(view.get("unlock_at") or view.get("unlock_date") or ""),
        "total": view.get("total"),
    }


async def _get_money_plan(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    profile = dict(args.get("profile") or {})
    accounts = args.get("accounts")
    portfolio = args.get("portfolio")
    positions = args.get("positions")

    connected = await _connected_balances(ctx)
    if connected:
        accounts = _merge_accounts(accounts, connected)
        if connected.get("currency"):
            profile["currency"] = connected["currency"]

    glider_state = None
    if portfolio or positions:
        glider_state = GliderState(portfolio=portfolio, positions=positions or [])

    vault_state = _vault_state(await _vault_view(ctx))

    plan = build_money_plan(profile, accounts, glider_state, vault_state)
    return {
        "plan": plan.model_dump(mode="json"),
        "spoken": render_spoken(plan),
        "structured": render_plan(plan) if args.get("plan_mode") else "",
        "confidence": plan.confidence,
        "assumptions": plan.assumptions,
        "next_action": plan.actions_90d[0].what if plan.actions_90d else "",
        "investing": plan.is_investing(),
        "glider_kind": plan.glider.kind,
        "vault_active": bool(vault_state),
    }


registry.register(
    name="get_money_plan",
    description=(
        "Compute THIS user's money plan and speak from it. Call before making any "
        "money claim (income, spending, debt, buffer, saving, investing, "
        "allocation, crypto, Glider, affordability). Returns the computed plan, a "
        "short spoken line, and the full structured plan when plan_mode is true. "
        "Pass `profile` with what the user has told you (income_amount, "
        "income_frequency, income_volatility, fixed_costs, variable_spend, "
        "country, currency, debts, cash_on_hand, goals, dependents, "
        "job_stability, can_self_custody). Connected balances are read from the "
        "ledger automatically. You may rewrite the diagnosis sentence and the "
        "90-day actions in your own words; you may NOT change the surplus, the "
        "book weights, glider_kind, the buffer target, or the debt actions. If "
        "the plan refuses to invest, say so plainly and say why."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "profile": {
                **_SCHEMA_OBJECT,
                "description": (
                    "What the user told you. Omit anything they did not say; a "
                    "missing number is a question, not a zero."
                ),
            },
            "accounts": {
                **_SCHEMA_OBJECT,
                "description": (
                    "Optional balances. Connected ledger balances override these."
                ),
            },
            "portfolio": {
                **_SCHEMA_OBJECT,
                "description": "Existing Glider portfolio, if the user has one",
            },
            "positions": {
                "type": "array",
                "description": "Existing Glider positions, if any",
            },
            "plan_mode": {
                "type": "boolean",
                "description": (
                    "True when the user asked for the plan or the numbers, so the "
                    "full structured block is returned"
                ),
            },
        },
    },
    category="money",
    risk_level=RiskLevel.LOW,
    handler=_get_money_plan,
)


__all__ = ["AccountsSnapshot", "registry"]

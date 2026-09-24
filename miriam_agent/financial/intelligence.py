import logging
from datetime import date, datetime
from typing import Any

import numpy as np

from miriam_agent.core.exceptions import FinancialError

logger = logging.getLogger(__name__)

_MONTH_DAYS = 30.4375


def _first_numeric(data: dict[str, Any], *keys: str) -> float | None:
    """Return the first present, parseable numeric value among ``keys``.

    The Go backend serializes money amounts as strings (e.g. ``"420.00"``),
    and different endpoints don't all use the same field name for the same
    concept, so callers pass a few candidate names in priority order.
    """
    for key in keys:
        if key not in data:
            continue
        try:
            return float(data[key])
        except (TypeError, ValueError):
            continue
    return None


def _money(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _calendar() -> tuple[int, int, int]:
    """days_elapsed, days_in_month, days_remaining (elapsed at least 1)."""
    today = date.today()
    elapsed = max(1, today.day)
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)
    days_in_month = (next_month - date(today.year, today.month, 1)).days
    remaining = max(0, days_in_month - today.day)
    return elapsed, days_in_month, remaining


# ---------------------------------------------------------------------------
# Financial-snapshot engine
#
# These functions consume the ledger-backed snapshot returned by Go's
# ``GET /api/v1/analytics/financial-snapshot`` (balances, ``money_flow``,
# ``monthly_flow`` series, ``budget``, ``profile``) plus optional
# ``upcoming_obligations``. They intentionally mirror the Go orchestrator's
# scoring so the delegated brain produces the same numbers as the in-process
# engine used before agent delegation.
# ---------------------------------------------------------------------------


def period_to_window(
    period: str, today: date | None = None
) -> tuple[str | None, str | None]:
    """Map a health-audit period to an (from, to) YYYY-MM-DD window.

    The implementation lives in ``core.periods``; this re-export keeps the
    historical import path working.
    """
    from miriam_agent.core.periods import period_to_window as _impl

    return _impl(period, today)


async def financial_plan_live(client: Any, token: str) -> dict[str, Any]:
    """Real financial plan, computed by this engine from Go's snapshot.

    The composition used to live on the Go client, which made the
    integration layer import the domain engine. The client now stays a raw
    adapter and is injected here instead.
    """
    snapshot = await client.engine_snapshot(token)
    return compute_financial_plan(snapshot)


async def cash_flow_forecast_live(client: Any, token: str) -> dict[str, Any]:
    """Forecast computed from the ledger-backed financial snapshot."""
    snapshot = await client.engine_snapshot(token)
    return compute_cash_flow_forecast(snapshot)


async def financial_health_live(
    client: Any, token: str, period: str = "last_90_days"
) -> dict[str, Any]:
    """Health score computed from the windowed ledger-backed snapshot."""
    from_date, to_date = period_to_window(period)
    snapshot = await client.engine_snapshot(token, from_date, to_date)
    return compute_financial_health(snapshot, period=period)


def _period_bounds(snapshot: dict[str, Any]) -> tuple[str | None, str | None]:
    period = snapshot.get("period")
    if not isinstance(period, dict):
        return None, None
    return period.get("from"), period.get("to")


def _flow_metrics(snapshot: dict[str, Any]) -> dict[str, float]:
    """Money-in/out for the snapshot window.

    Primary source is Go's ``money_flow`` block (string amounts). Falls back
    to the legacy ``spending_summary`` shape so callers that still scrape
    ``/api/v1/analytics/dashboard`` degrade gracefully instead of breaking.
    """
    flow = snapshot.get("money_flow")
    if isinstance(flow, dict) and not flow.get("error"):
        withdrawals = _money(flow.get("total_withdrawals"))
        card = _money(flow.get("total_card_spend"))
        p2p = _money(flow.get("total_p2p"))
        receipts = _money(flow.get("total_receipts"))
        income = _money(flow.get("total_deposits"))
        deposit_count = _money(flow.get("deposit_count"))
        outflow = withdrawals + card + p2p + receipts
        return {
            "income": income,
            "outflow": outflow,
            "card_spend": card,
            "net": income - outflow,
            "deposit_count": deposit_count,
        }

    legacy = snapshot.get("spending_summary") or {}
    if not isinstance(legacy, dict):
        legacy = {}
    income = _first_numeric(legacy, "income", "total_income", "inflow") or 0.0
    outflow = _first_numeric(legacy, "total_spent", "spent", "total", "outflow") or 0.0
    if outflow == 0.0:
        cats = legacy.get("top_categories") or legacy.get("categories") or []
        if isinstance(cats, list):
            outflow = sum(
                _first_numeric(c, "amount", "monthly_amount", "total") or 0.0
                for c in cats
                if isinstance(c, dict)
            )
    return {
        "income": income,
        "outflow": outflow,
        "card_spend": outflow,
        "net": income - outflow,
        "deposit_count": _first_numeric(legacy, "deposit_count", "income_count") or 0.0,
    }


def _budget_block(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    budget = snapshot.get("budget")
    if not isinstance(budget, dict) or not budget.get("set"):
        return None
    limit = _money(budget.get("monthly_limit"))
    if limit <= 0:
        return None
    return {"limit": limit, "currency": budget.get("currency")}


def _profile_block(snapshot: dict[str, Any]) -> dict[str, Any]:
    profile = snapshot.get("profile")
    if not isinstance(profile, dict) or not profile.get("has_profile"):
        return {
            "has_profile": False,
            "primary_currency": "USD",
            "income_frequency": "monthly",
            "financial_goal": None,
            "risk_tolerance": "moderate",
            "investment_horizon": "medium",
            "monthly_income": 0.0,
            "monthly_fixed_costs": 0.0,
            "monthly_savings_target": 0.0,
            "emergency_fund_target": 0.0,
        }
    return {
        "has_profile": True,
        "primary_currency": profile.get("primary_currency") or "USD",
        "income_frequency": profile.get("income_frequency") or "monthly",
        "financial_goal": profile.get("financial_goal"),
        "risk_tolerance": profile.get("risk_tolerance") or "moderate",
        "investment_horizon": profile.get("investment_horizon") or "medium",
        "monthly_income": _money(profile.get("monthly_income")),
        "monthly_fixed_costs": _money(profile.get("monthly_fixed_costs")),
        "monthly_savings_target": _money(profile.get("monthly_savings_target")),
        "emergency_fund_target": _money(profile.get("emergency_fund_target")),
    }


def _bills_due(snapshot: dict[str, Any]) -> float:
    obligations = snapshot.get("upcoming_obligations") or []
    total = 0.0
    if not isinstance(obligations, list):
        return total
    for ob in obligations:
        if not isinstance(ob, dict):
            continue
        status = str(ob.get("status") or ob.get("Status") or "").lower()
        if status in {"paid", "cancelled"}:
            continue
        total += _first_numeric(ob, "amount", "Amount") or 0.0
    return total


def _window_metrics(snapshot: dict[str, Any]) -> dict[str, float]:
    """Observed totals plus monthly averages for the snapshot window.

    Mirrors Go's ``observedMonths`` (window days / 30.4375, clamped to >=1)
    so monthly figures stay comparable across single- and multi-month audits.
    """
    flow = _flow_metrics(snapshot)
    from_date, to_date = _period_bounds(snapshot)
    observed_months = 1.0
    if from_date and to_date:
        try:
            start = datetime.strptime(from_date, "%Y-%m-%d")
            end = datetime.strptime(to_date, "%Y-%m-%d")
            observed_months = max(1.0, (end - start).days / _MONTH_DAYS)
        except ValueError:
            observed_months = 1.0
    return {
        "income": flow["income"],
        "outflow": flow["outflow"],
        "net": flow["net"],
        "deposit_count": flow["deposit_count"],
        "observed_months": observed_months,
        "monthly_income": flow["income"] / observed_months,
        "monthly_outflow": flow["outflow"] / observed_months,
        "monthly_net": flow["net"] / observed_months,
    }


def _monthly_trend(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Per-month buckets from the snapshot's ``monthly_flow`` series and a
    direction label (improving / worsening / flat), or ([], "flat").
    """
    series = snapshot.get("monthly_flow")
    buckets: list[dict[str, Any]] = []
    nets: list[float] = []
    if isinstance(series, list):
        for bucket in series:
            if not isinstance(bucket, dict):
                continue
            inflow = _money(bucket.get("total_deposits"))
            outflow = _money(bucket.get("total_outflow"))
            buckets.append(
                {
                    "month": bucket.get("month"),
                    "income": round(inflow, 2),
                    "outflow": round(outflow, 2),
                    "net": round(inflow - outflow, 2),
                }
            )
            nets.append(inflow - outflow)
    if len(buckets) < 2 or len(nets) < 2:
        return buckets, "flat"
    half = max(1, len(nets) // 2)
    first_half = sum(nets[:half]) / half
    second_half = sum(nets[half:]) / (len(nets) - half)
    delta = second_half - first_half
    noise = max(1.0, abs(first_half) * 0.1)
    if delta > noise:
        direction = "improving"
    elif delta < -noise:
        direction = "worsening"
    else:
        direction = "flat"
    return buckets, direction


def _period_label(period: str, from_date: str | None, to_date: str | None) -> str:
    if period == "this_month":
        return "This month to date"
    if period == "last_month":
        return "Last month"
    if from_date and to_date:
        return f"{from_date} to {to_date}"
    return period.replace("_", " ")


def compute_financial_health(
    snapshot: dict[str, Any], period: str = "last_90_days"
) -> dict[str, Any]:
    """Score 0-100 across savings rate, budget control, runway, and stash.

    Scoring mirrors the Go orchestrator (savings 25 + budget 20 + runway 25 +
    stash 20 + 10 base) and adds a month-over-month trend when the snapshot
    window spans two or more months.
    """
    from_date, to_date = _period_bounds(snapshot)
    balances = snapshot.get("balances") or {}
    spend = _money(balances.get("spending_balance"))
    stash = _money(balances.get("stash_balance"))
    total = _money(balances.get("total_balance"))
    if total <= 0:
        total = spend + stash

    m = _window_metrics(snapshot)
    budget = _budget_block(snapshot)
    bills = _bills_due(snapshot)

    budget_score = 20
    budget_status = "not_set"
    budget_limit = 0.0
    budget_remaining = 0.0
    if budget:
        budget_limit = budget["limit"]
        budget_remaining = budget_limit - m["monthly_outflow"]
        used = (m["monthly_outflow"] / budget_limit * 100) if budget_limit > 0 else 0.0
        budget_status = "on_track"
        if used <= 70:
            budget_score = 20
        elif used <= 90:
            budget_score, budget_status = 14, "tight"
        elif used <= 100:
            budget_score, budget_status = 8, "near_limit"
        else:
            budget_score, budget_status = 2, "over_budget"

    savings_rate = (m["net"] / m["income"] * 100.0) if m["income"] > 0 else 0.0
    savings_score = 8
    if savings_rate >= 25:
        savings_score = 25
    elif savings_rate >= 15:
        savings_score = 20
    elif savings_rate >= 5:
        savings_score = 14
    elif savings_rate >= 0:
        savings_score = 8
    else:
        savings_score = 2

    runway_score = 10
    runway_days: float | None = None
    if m["monthly_outflow"] > 0:
        avg_daily_out = m["monthly_outflow"] / _MONTH_DAYS
        runway_days = total / avg_daily_out if avg_daily_out > 0 else 0.0
        if runway_days >= 90:
            runway_score = 25
        elif runway_days >= 30:
            runway_score = 18
        elif runway_days >= 14:
            runway_score = 10
        else:
            runway_score = 4

    stash_pct = (stash / total * 100.0) if total > 0 else 0.0
    stash_score = 5
    if total > 0 and stash_pct >= 30:
        stash_score = 20
    elif total > 0 and stash_pct >= 20:
        stash_score = 15
    elif total > 0 and stash_pct >= 10:
        stash_score = 10

    score = max(
        0, min(100, savings_score + budget_score + runway_score + stash_score + 10)
    )
    if score >= 80:
        status = "strong"
    elif score >= 60:
        status = "steady"
    elif score >= 40:
        status = "fragile"
    else:
        status = "needs_attention"

    buckets, trend_direction = _monthly_trend(snapshot)

    actions: list[str] = []
    if budget_status == "not_set":
        actions.append(
            "Set a monthly spending budget so Miriam can track safe daily spend."
        )
    elif budget_status == "over_budget" and budget_remaining < 0:
        actions.append(
            f"Pause non-essential spend; you are ${abs(budget_remaining):.2f} "
            "over budget on average."
        )
    if savings_rate < 10 and m["income"] > 0:
        actions.append("Aim to save at least 10% of incoming money.")
    if stash < spend * 0.25 and spend > 20:
        actions.append(
            "Move a small amount from Spend to Stash so more of your money earns yield."
        )
    if runway_days is not None and runway_days < 14:
        actions.append(
            "At the current outflow rate, available cash lasts under two weeks."
        )
    if bills > total > 0:
        actions.append(
            "Upcoming bills are larger than cash on hand this month. Trim spend "
            "or move money from Stash."
        )
    if not actions:
        actions.append("Keep your current pace and review your forecast weekly.")

    result: dict[str, Any] = {
        "source": "python",
        "engine": "financial-snapshot",
        "score": int(score),
        "status": status,
        "period": period,
        "period_label": _period_label(period, from_date, to_date),
        "spend_balance": round(spend, 2),
        "stash_balance": round(stash, 2),
        "total_balance": round(total, 2),
        "total_income": round(m["income"], 2),
        "total_outflow": round(m["outflow"], 2),
        "total_net_flow": round(m["net"], 2),
        "monthly_income": round(m["monthly_income"], 2),
        "monthly_outflow": round(m["monthly_outflow"], 2),
        "monthly_net_flow": round(m["monthly_net"], 2),
        "savings_rate_pct": round(savings_rate, 1),
        "budget_status": budget_status,
        "budget_limit": round(budget_limit, 2),
        "budget_remaining": round(budget_remaining, 2),
        "stash_pct": round(stash_pct, 1),
        "runway_days": round(runway_days, 1) if runway_days is not None else None,
        "recommended_actions": actions,
        "score_components": [
            {"name": "Savings Rate", "score": savings_score, "max": 25},
            {"name": "Budget Control", "score": budget_score, "max": 20},
            {"name": "Runway", "score": runway_score, "max": 25},
            {"name": "Stash Discipline", "score": stash_score, "max": 20},
        ],
        "data_used": [
            "balances",
            "money_flow",
            "monthly_flow",
            "budget",
            "financial_profile",
        ],
    }
    if buckets and trend_direction != "flat":
        result["monthly_trend"] = buckets
        result["trend_direction"] = trend_direction
    return result


def _next_month_anchor(snapshot: dict[str, Any], fallback_net: float) -> dict[str, Any]:
    series = snapshot.get("monthly_flow")
    months: list[tuple[float, float]] = []
    if isinstance(series, list):
        for bucket in series:
            if not isinstance(bucket, dict):
                continue
            months.append(
                (
                    _money(bucket.get("total_deposits")),
                    _money(bucket.get("total_outflow")),
                )
            )
    if not months:
        return {
            "expected_income": 0.0,
            "expected_outflow": 0.0,
            "expected_net": round(fallback_net, 2),
        }
    last_three = months[-3:]
    expected_income = sum(x[0] for x in last_three) / len(last_three)
    expected_outflow = sum(x[1] for x in last_three) / len(last_three)
    return {
        "expected_income": round(expected_income, 2),
        "expected_outflow": round(expected_outflow, 2),
        "expected_net": round(expected_income - expected_outflow, 2),
    }


def compute_cash_flow_forecast(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project the rest of this month from live balances, spend, and budget.

    Mirrors Go's forecast: daily burn rate from month-to-date outflow, a
    projected end-of-month balance, and a safe daily spend capped by whatever
    budget is left. The snapshot's per-month series anchors a next-month
    estimate.
    """
    today = date.today()
    elapsed, days_in_month, remaining = _calendar()

    flow = _flow_metrics(snapshot)
    balances = snapshot.get("balances") or {}
    spend = _money(balances.get("spending_balance"))
    stash = _money(balances.get("stash_balance"))
    total = _money(balances.get("total_balance"))
    if total <= 0:
        total = spend + stash
    budget = _budget_block(snapshot)

    daily_burn = flow["outflow"] / elapsed if elapsed else 0.0
    projected_out = daily_burn * days_in_month
    projected_net = flow["income"] - projected_out
    projected_end = max(0.0, total - daily_burn * remaining)

    safe_daily = 0.0
    primary_action = "Keep spending near your current daily average."
    if budget and remaining > 0:
        left_in_budget = budget["limit"] - flow["outflow"]
        if left_in_budget > 0:
            safe_daily = left_in_budget / remaining
            primary_action = (
                f"Stay under ${safe_daily:.2f}/day for the rest of the month."
            )
        elif left_in_budget < 0:
            primary_action = (
                f"You are ${abs(left_in_budget):.2f} over budget; "
                "pause discretionary spend."
            )
    if safe_daily <= 0 and remaining > 0 and spend > 0:
        safe_daily = spend / remaining

    confidence = "medium"
    if flow["deposit_count"] > 0 and flow["outflow"] > 0:
        confidence = "high"
    elif flow["outflow"] <= 0:
        confidence = "low"

    return {
        "source": "python",
        "engine": "financial-snapshot",
        "period": f"{today.strftime('%B')} {today.year}",
        "days_elapsed": elapsed,
        "days_remaining": remaining,
        "income_so_far": round(flow["income"], 2),
        "spent_so_far": round(flow["outflow"], 2),
        "daily_burn_rate": round(daily_burn, 2),
        "safe_daily_spend": round(safe_daily, 2),
        "projected_outflow": round(projected_out, 2),
        "projected_net_flow": round(projected_net, 2),
        "projected_end_balance": round(projected_end, 2),
        "spend_balance": round(spend, 2),
        "stash_balance": round(stash, 2),
        "next_month": _next_month_anchor(snapshot, projected_net),
        "confidence": confidence,
        "primary_action": primary_action,
        "data_used": ["money_flow", "balances", "budget", "monthly_flow"],
    }


def compute_financial_plan(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Assemble a practical plan from health, forecast, and the profile."""
    health = compute_financial_health(snapshot, period="last_90_days")
    forecast = compute_cash_flow_forecast(snapshot)
    profile = _profile_block(snapshot)

    steps: list[dict[str, Any]] = [
        {
            "priority": 1,
            "title": "Protect this month",
            "action": forecast["primary_action"],
        },
        {
            "priority": 2,
            "title": "Build automatic savings",
            "action": "Use Stash as the default place for money you do not need this week.",
        },
        {
            "priority": 3,
            "title": "Review recurring spend",
            "action": "Check subscriptions and recurring merchants before increasing savings targets.",
        },
    ]
    if profile["has_profile"]:
        available = health["stash_balance"] + health["spend_balance"]
        if profile["emergency_fund_target"] > 0:
            gap = max(0.0, profile["emergency_fund_target"] - available)
            if gap > 0:
                steps.append(
                    {
                        "priority": 4,
                        "title": "Close the emergency-fund gap",
                        "action": (
                            f"About ${gap:,.2f} more toward your emergency fund "
                            f"target of ${profile['emergency_fund_target']:,.2f}."
                        ),
                    }
                )
        if profile["monthly_savings_target"] > 0 and health["monthly_income"] > 0:
            target_pct = (
                profile["monthly_savings_target"] / health["monthly_income"] * 100
            )
            if health["savings_rate_pct"] < target_pct:
                steps.append(
                    {
                        "priority": 5,
                        "title": "Hit your savings target",
                        "action": (
                            f"Aim to save about ${profile['monthly_savings_target']:,.2f} "
                            "each month."
                        ),
                    }
                )

    return {
        "source": "python",
        "engine": "financial-snapshot",
        "health": health,
        "forecast": forecast,
        "profile": {
            "has_profile": profile["has_profile"],
            "primary_currency": profile["primary_currency"],
            "income_frequency": profile["income_frequency"],
            "financial_goal": profile["financial_goal"],
            "risk_tolerance": profile["risk_tolerance"],
            "investment_horizon": profile["investment_horizon"],
            "monthly_savings_target": round(profile["monthly_savings_target"], 2),
            "emergency_fund_target": round(profile["emergency_fund_target"], 2),
        },
        "next_steps": steps,
        "data_used": [
            "financial_health",
            "cash_flow_forecast",
            "financial_profile",
            "budget",
        ],
    }


class FinancialIntelligence:
    """Financial intelligence and analysis module for Miriam Financial Agent."""

    def __init__(self, memory_store: Any, go_client: Any = None):
        self.memory_store = memory_store
        self.go_client = go_client
        self.llm_cache = {}
        self.risk_cache = {}
        self.pattern_cache = {}

    async def analyze_intent(
        self, message: str, financial_profile: Any
    ) -> dict[str, Any]:
        """Analyze user message to determine intent."""
        try:
            # Simple intent detection based on keywords
            intent_keywords = {
                "analysis": ["analyze", "check", "review", "show", "view", "see"],
                "planning": [
                    "plan",
                    "create",
                    "set up",
                    "establish",
                    "develop",
                    "design",
                ],
                "transaction": [
                    "transfer",
                    "send",
                    "receive",
                    "pay",
                    "spend",
                    "earn",
                ],
                "advice": [
                    "advice",
                    "suggest",
                    "recommend",
                    "help",
                    "guide",
                    "tell me",
                ],
                "alert": [
                    "alert",
                    "notify",
                    "warn",
                    "important",
                    "urgent",
                    "attention",
                ],
            }

            message_lower = message.lower()
            detected_intent = "general"

            for intent, keywords in intent_keywords.items():
                if any(keyword in message_lower for keyword in keywords):
                    detected_intent = intent
                    break

            # Extract additional context
            timeframe = self._extract_timeframe(message)
            target_amount = self._extract_amount(message)
            categories = self._extract_categories(message)

            return {
                "type": detected_intent,
                "description": self._generate_intent_description(
                    detected_intent, message
                ),
                "timeframe": timeframe,
                "target_amount": target_amount,
                "categories": categories,
                "urgency": self._assess_urgency(message),
                "risk_level": self._assess_risk_level(message, financial_profile),
            }

        except Exception as e:
            logger.error(
                "Error analyzing intent",
                exc_info=True,
            )
            raise FinancialError(f"Failed to analyze intent: {str(e)}")

    def _extract_timeframe(self, message: str) -> str | None:
        """Extract timeframe from message."""
        timeframes = {
            "today": "day",
            "tomorrow": "day",
            "this week": "week",
            "this month": "month",
            "this year": "year",
            "next week": "week",
            "next month": "month",
        }

        message_lower = message.lower()
        for phrase, timeframe in timeframes.items():
            if phrase in message_lower:
                return timeframe

        return None

    def _extract_amount(self, message: str) -> float | None:
        """Extract monetary amount from message."""
        import re

        # Match patterns like $100, 100 USD, 100.50, etc.
        amount_pattern = r"\$?\s*(\d+(?:\.\d{2})?)\s*(?:USD|dollars?|USD)?"
        match = re.search(amount_pattern, message)

        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass

        return None

    def _extract_categories(self, message: str) -> list[str]:
        """Extract financial categories from message."""
        categories = {
            "food": ["food", "groceries", "dining", "restaurant", "meal"],
            "transport": [
                "transport",
                "car",
                "gas",
                "fuel",
                "ride",
                "taxi",
                "uber",
            ],
            "housing": ["housing", "rent", "mortgage", "utilities"],
            "entertainment": [
                "entertainment",
                "movies",
                "show",
                "game",
                "streaming",
            ],
            "shopping": [
                "shopping",
                "clothes",
                "purchase",
                "buy",
                "online",
            ],
            "health": [
                "health",
                "medical",
                "doctor",
                "pharmacy",
                "prescription",
            ],
            "savings": ["savings", "saving", "invest", "investment"],
        }

        message_lower = message.lower()
        detected_categories = []

        for category, keywords in categories.items():
            if any(keyword in message_lower for keyword in keywords):
                detected_categories.append(category)

        return detected_categories

    def _generate_intent_description(self, intent_type: str, message: str) -> str:
        """Generate description of the detected intent."""
        descriptions = {
            "analysis": "User wants to analyze their financial situation or data",
            "planning": "User wants to create or modify financial plans",
            "transaction": "User wants to perform or review transactions",
            "advice": "User is seeking financial advice or recommendations",
            "alert": "User wants to be alerted about important financial matters",
            "general": "User is asking general financial questions",
        }

        base_description = descriptions.get(intent_type, "User has a financial inquiry")

        # Add context from message
        if "budget" in message.lower():
            return f"{base_description} related to budgeting"

        if "investment" in message.lower():
            return f"{base_description} related to investments"

        if "debt" in message.lower():
            return f"{base_description} related to debt management"

        return base_description

    def _assess_urgency(self, message: str) -> str:
        """Assess urgency level of the message."""
        urgent_words = [
            "urgent",
            "asap",
            "immediately",
            "right now",
            "today",
            "critical",
            "emergency",
            "important",
            "time sensitive",
        ]

        message_lower = message.lower()
        if any(word in message_lower for word in urgent_words):
            return "high"

        # Check for negative words that might indicate urgency
        if any(
            word in message_lower
            for word in ["problem", "issue", "error", "wrong", "mistake"]
        ):
            return "medium"

        return "low"

    def _assess_risk_level(self, message: str, financial_profile: Any) -> str:
        """Assess risk level of the requested action."""
        # Check message for high-risk keywords
        high_risk_words = [
            "large amount",
            "significant",
            "substantial",
            "major",
            "big",
            "high value",
            "expensive",
            "large transaction",
        ]

        message_lower = message.lower()
        if any(word in message_lower for word in high_risk_words):
            return "high"

        # Check financial profile for risk indicators
        if financial_profile and hasattr(financial_profile, "risk_tolerance"):
            if financial_profile.risk_tolerance.lower() == "high":
                return "medium"

        return "low"

    async def analyze_portfolio(
        self, user_id: str, token: str | None = None, period: str = "month"
    ) -> dict[str, Any]:
        """Analyze user's investment portfolio.

        Bug fix: this used to call ``memory_store.get_portfolio_data(user_id,
        period)``, but that method only ever accepted ``user_id`` -- every
        call raised ``TypeError`` (wrapped into ``FinancialError`` by the
        outer except below). When ``token`` is supplied, real positions are
        now pulled from the Go backend (the actual source of truth for
        holdings) instead.
        """
        try:
            portfolio_data: dict[str, Any] = {}
            if token and self.go_client is not None:
                try:
                    positions = await self.go_client.get_investment_positions(token)
                    portfolio_data = self._shape_positions(positions)
                except Exception as e:
                    logger.warning("Falling back to local portfolio data: %s", e)
            if not portfolio_data.get("holdings"):
                # No token, Go unreachable, or genuinely no positions: fall
                # back to the local snapshot (currently always empty, since
                # Miriam doesn't independently track investment positions).
                portfolio_data = await self.memory_store.get_portfolio_data(user_id)

            if not portfolio_data or not portfolio_data.get("holdings"):
                return {
                    "error": "No portfolio data found",
                    "suggestions": "Please connect your accounts to analyze portfolio",
                }

            # Calculate performance metrics
            performance = self._calculate_portfolio_performance(portfolio_data, period)

            # Identify risk factors
            risk_factors = self._identify_risk_factors(portfolio_data)

            # Generate recommendations
            recommendations = await self._generate_portfolio_recommendations(
                portfolio_data, performance, risk_factors
            )

            return {
                "performance": performance,
                "risk_factors": risk_factors,
                "recommendations": recommendations,
                "diversification_score": self._calculate_diversification_score(
                    portfolio_data
                ),
            }

        except Exception as e:
            logger.error(
                "Error analyzing portfolio",
                exc_info=True,
            )
            raise FinancialError(f"Failed to analyze portfolio: {str(e)}")

    def _calculate_portfolio_performance(
        self, portfolio_data: dict[str, Any], period: str
    ) -> dict[str, Any]:
        """Calculate portfolio performance metrics."""
        try:
            # Calculate returns
            total_value = sum(
                holding["value"] for holding in portfolio_data.get("holdings", [])
            )
            cost_basis = sum(
                holding["cost_basis"] for holding in portfolio_data.get("holdings", [])
            )

            total_return = total_value - cost_basis
            return_percentage = (
                (total_return / cost_basis * 100) if cost_basis > 0 else 0
            )

            # Calculate volatility (simplified)
            returns = self._calculate_daily_returns(portfolio_data)
            volatility = np.std(returns) * np.sqrt(252) if returns else 0

            # Calculate Sharpe ratio (simplified). Bug fix: `returns` is a
            # plain list, and `list - float` isn't valid Python (only numpy
            # arrays support elementwise subtraction) -- this raised
            # TypeError on every call that reached this line.
            risk_free_rate = 0.02  # 2% risk-free rate
            excess_returns = [r - risk_free_rate for r in returns] if returns else []
            sharpe_ratio = (
                (np.mean(excess_returns) / np.std(returns))
                if returns and np.std(returns) > 0
                else 0
            )

            return {
                "total_value": total_value,
                "cost_basis": cost_basis,
                "total_return": total_return,
                "return_percentage": return_percentage,
                "volatility": volatility,
                "sharpe_ratio": sharpe_ratio,
                "period": period,
            }

        except Exception as e:
            logger.error(
                "Error calculating portfolio performance",
                exc_info=True,
            )
            return {"error": str(e)}

    def _identify_risk_factors(
        self, portfolio_data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Identify risk factors in the portfolio."""
        risk_factors = []

        try:
            holdings = portfolio_data.get("holdings", [])

            # Check concentration risk
            if holdings:
                total_value = sum(holding["value"] for holding in holdings)
                if total_value > 0:
                    max_position = max(
                        (holding["value"] / total_value) for holding in holdings
                    )
                    if max_position > 0.5:
                        risk_factors.append(
                            {
                                "type": "concentration",
                                "severity": "high",
                                "description": f"Largest position represents {max_position * 100:.1f}% of portfolio",
                            }
                        )

            # Check sector concentration
            sectors = {}
            for holding in holdings:
                sector = holding.get("sector", "unknown")
                sectors[sector] = sectors.get(sector, 0) + holding["value"]

            if sectors:
                total_value = sum(sectors.values())
                for sector, value in sectors.items():
                    if value / total_value > 0.4:
                        risk_factors.append(
                            {
                                "type": "sector_concentration",
                                "severity": "medium",
                                "description": f"{sector} represents {(value / total_value) * 100:.1f}% of portfolio",
                            }
                        )

            # Check cash position
            cash_percentage = self._get_cash_percentage(portfolio_data)
            if cash_percentage > 0.2:
                risk_factors.append(
                    {
                        "type": "low_return_assets",
                        "severity": "low",
                        "description": f"Cash and cash equivalents represent {cash_percentage * 100:.1f}% of portfolio",
                    }
                )

        except Exception:
            logger.error(
                "Error identifying risk factors",
                exc_info=True,
            )

        return risk_factors

    async def _generate_portfolio_recommendations(
        self,
        portfolio_data: dict[str, Any],
        performance: dict[str, Any],
        risk_factors: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Generate portfolio recommendations."""
        recommendations = []

        try:
            # Diversification recommendation
            diversification_score = self._calculate_diversification_score(
                portfolio_data
            )
            if diversification_score < 0.7:
                recommendations.append(
                    {
                        "type": "diversification",
                        "priority": "high",
                        "description": "Consider diversifying your portfolio across different asset classes and sectors",
                        "expected_impact": "Reduced risk, potentially better risk-adjusted returns",
                    }
                )

            # Rebalancing recommendation
            if performance.get("return_percentage", 0) > 20:
                recommendations.append(
                    {
                        "type": "rebalancing",
                        "priority": "medium",
                        "description": "Portfolio has experienced significant gains. Consider rebalancing to maintain target allocation",
                        "expected_impact": "Maintained risk profile and locked in gains",
                    }
                )

            # Risk management recommendation
            high_risk_factors = [
                factor for factor in risk_factors if factor.get("severity") == "high"
            ]
            if high_risk_factors:
                recommendations.append(
                    {
                        "type": "risk_management",
                        "priority": "high",
                        "description": "High risk factors detected. Consider implementing risk mitigation strategies",
                        "expected_impact": "Reduced portfolio volatility and potential losses",
                    }
                )

            # Income generation recommendation
            cash_percentage = self._get_cash_percentage(portfolio_data)
            if cash_percentage < 0.1:
                recommendations.append(
                    {
                        "type": "income_generation",
                        "priority": "low",
                        "description": "Consider allocating a portion of your portfolio to income-generating assets",
                        "expected_impact": "Additional cash flow and diversification",
                    }
                )

            # Default recommendation if no specific recommendations
            if not recommendations:
                recommendations.append(
                    {
                        "type": "maintenance",
                        "priority": "low",
                        "description": "Your portfolio looks well-balanced. Continue with regular monitoring and contributions",
                        "expected_impact": "Maintained financial goals on track",
                    }
                )

        except Exception:
            logger.error(
                "Error generating portfolio recommendations",
                exc_info=True,
            )

        return recommendations

    def _calculate_diversification_score(self, portfolio_data: dict[str, Any]) -> float:
        """Calculate portfolio diversification score."""
        try:
            holdings = portfolio_data.get("holdings", [])

            if not holdings:
                return 0.0

            # Calculate Herfindahl-Hirschman Index (HHI)
            total_value = sum(holding["value"] for holding in holdings)
            if total_value <= 0:
                return 0.0

            hhi = sum((holding["value"] / total_value) ** 2 for holding in holdings)

            # Convert HHI to diversification score (0-1, where 1 is most diversified)
            diversification_score = max(0, 1 - hhi)

            return diversification_score

        except Exception:
            logger.error(
                "Error calculating diversification score",
                exc_info=True,
            )
            return 0.0

    @staticmethod
    def _shape_positions(positions: Any) -> dict[str, Any]:
        """Convert Go's investment-positions payload into the internal
        ``{"holdings": [...]}`` shape the analysis methods above expect.

        There's no shared schema pinning down Go's exact field names here,
        so this reads the common aliases defensively (``value``/``market_value``/
        ``current_value``, ``cost_basis``/``book_value``) and skips any entry
        with no positive value rather than guessing at a number.
        """
        if isinstance(positions, dict):
            positions = positions.get("positions", [])
        holdings = []
        for p in positions or []:
            if not isinstance(p, dict):
                continue
            value = _first_numeric(p, "value", "market_value", "current_value")
            if value is None or value <= 0:
                continue
            cost_basis = _first_numeric(p, "cost_basis", "book_value", "cost") or value
            holdings.append(
                {
                    "symbol": p.get("symbol") or p.get("ticker") or "UNKNOWN",
                    "value": value,
                    "cost_basis": cost_basis,
                    "sector": p.get("sector", "unknown"),
                    "type": p.get("type", "investment"),
                }
            )
        return {"holdings": holdings}

    def _get_cash_percentage(self, portfolio_data: dict[str, Any]) -> float:
        """Get percentage of portfolio in cash."""
        try:
            holdings = portfolio_data.get("holdings", [])

            if not holdings:
                return 0.0

            total_value = sum(holding["value"] for holding in holdings)
            if total_value <= 0:
                return 0.0

            cash_value = sum(
                holding["value"]
                for holding in holdings
                if holding.get("type", "") == "cash"
            )

            return cash_value / total_value

        except Exception:
            logger.error(
                "Error calculating cash percentage",
                exc_info=True,
            )
            return 0.0

    async def generate_budget_plan(
        self, user_id: str, token: str | None = None, goal: str = "balance"
    ) -> dict[str, Any]:
        """Generate a budget plan for the user.

        Bug fix: this used to crash for any user with no locally-stored
        income (i.e. almost everyone, since nothing populates a
        ``FinancialProfile`` row yet) via a division-by-zero in
        ``_allocate_budget``, and separately via a
        ``float - dict`` TypeError when computing ``net_cash_flow`` (see
        the fixes in ``_allocate_budget`` and below). Income still comes
        from the local financial profile; expenses now come from the Go
        backend's real spending summary when ``token`` is available,
        replacing a stub that always reported zero spending.
        """
        try:
            # Get user's financial data
            income_data = await self.memory_store.get_income_data(user_id)
            expense_data = await self._get_expense_data(user_id, token)

            # Calculate monthly income
            monthly_income = self._calculate_monthly_income(income_data)

            # Calculate monthly expenses (category -> monthly amount)
            monthly_expenses = self._calculate_monthly_expenses(expense_data)

            # Determine savings target
            savings_target = self._calculate_savings_target(monthly_income, goal)

            # Allocate budget to categories
            budget_allocation = self._allocate_budget(
                monthly_income, expense_data, savings_target
            )

            return {
                "monthly_income": monthly_income,
                "monthly_expenses": monthly_expenses,
                "savings_target": savings_target,
                "budget_allocation": budget_allocation,
                "net_cash_flow": (
                    monthly_income - sum(monthly_expenses.values()) - savings_target
                ),
                "recommendations": await self._generate_budget_recommendations(
                    budget_allocation, expense_data
                ),
            }

        except Exception as e:
            logger.error(
                "Error generating budget plan",
                exc_info=True,
            )
            raise FinancialError(f"Failed to generate budget plan: {str(e)}")

    async def _get_expense_data(
        self, user_id: str, token: str | None
    ) -> dict[str, Any]:
        """Real expenses from Go's spending summary when reachable, else the
        local stub (which currently always reports zero spend, since Miriam
        doesn't independently track transactions).
        """
        if token and self.go_client is not None:
            try:
                summary = await self.go_client.get_spending_summary(
                    token, period="month"
                )
                categories = (
                    summary.get("top_categories") or summary.get("categories") or []
                )
                category_expenses = {}
                for c in categories:
                    if not isinstance(c, dict):
                        continue
                    name = c.get("category") or c.get("name")
                    amount = _first_numeric(c, "amount", "monthly_amount", "total")
                    if name and amount is not None:
                        category_expenses[name] = {"monthly_amount": amount}
                if category_expenses:
                    return {"category_expenses": category_expenses}
            except Exception as e:
                logger.warning("Falling back to local expense data: %s", e)
        return await self.memory_store.get_expense_data(user_id)

    def _calculate_monthly_income(self, income_data: dict[str, Any]) -> float:
        """Calculate monthly income from income data."""
        try:
            monthly_income = 0.0

            # Sum regular income
            for income in income_data.get("regular_income", []):
                amount = income.get("amount", 0)
                frequency = income.get("frequency", "monthly")

                if frequency == "monthly":
                    monthly_income += amount
                elif frequency == "weekly":
                    monthly_income += amount * 4.33
                elif frequency == "biweekly":
                    monthly_income += amount * 2.17
                elif frequency == "quarterly":
                    monthly_income += amount / 3
                elif frequency == "annual":
                    monthly_income += amount / 12

            # Add bonus/income
            for income in income_data.get("bonus_income", []):
                monthly_income += income.get("monthly_amount", 0)

            return monthly_income

        except Exception:
            logger.error(
                "Error calculating monthly income",
                exc_info=True,
            )
            return 0.0

    def _calculate_monthly_expenses(
        self, expense_data: dict[str, Any]
    ) -> dict[str, float]:
        """Calculate monthly expenses by category.

        Bug fix: ``category_expenses`` maps each category to a single
        ``{"monthly_amount": float}`` entry (matching what ``_allocate_budget``
        below already expected). This used to iterate each entry as if it
        were a list of expense records, which silently returned ``{}`` the
        moment real category data showed up instead of crashing loudly.
        """
        try:
            monthly_expenses = {}

            # Average expenses by category
            for category, data in expense_data.get("category_expenses", {}).items():
                monthly_expenses[category] = float(data.get("monthly_amount", 0) or 0)

            return monthly_expenses

        except Exception:
            logger.error(
                "Error calculating monthly expenses",
                exc_info=True,
            )
            return {}

    def _calculate_savings_target(self, monthly_income: float, goal: str) -> float:
        """Calculate monthly savings target based on goal."""
        try:
            if goal == "zero_based":
                return 0.0
            elif goal == "emergency_fund":
                return monthly_income * 0.20  # 20% of income
            elif goal == "retirement":
                return monthly_income * 0.15  # 15% of income
            elif goal == "goal_based":
                return monthly_income * 0.10  # 10% of income
            elif goal == "debt_paydown":
                return min(monthly_income * 0.20, 1000)  # Up to $1000 or 20% of income
            else:  # balance or default
                return monthly_income * 0.10  # 10% of income

        except Exception:
            logger.error(
                "Error calculating savings target",
                exc_info=True,
            )
            return 0.0

    def _allocate_budget(
        self,
        monthly_income: float,
        expense_data: dict[str, Any],
        savings_target: float,
    ) -> dict[str, float]:
        """Allocate budget to categories based on expenses and income."""
        try:
            # Get current expense percentages
            expense_categories = expense_data.get("category_expenses", {})

            # Calculate total monthly expenses
            total_expenses = sum(
                category_data["monthly_amount"]
                for category_data in expense_categories.values()
            )

            # Allocate based on current spending patterns
            budget_allocation = {}

            if total_expenses > 0 and monthly_income > 0:
                for category, category_data in expense_categories.items():
                    monthly_amount = category_data["monthly_amount"]
                    percentage = (monthly_amount / monthly_income) * 100

                    budget_allocation[category] = {
                        "monthly_amount": monthly_amount,
                        "percentage": percentage,
                    }

            # Add savings category. Guard against monthly_income == 0 (no
            # financial profile yet): this used to raise ZeroDivisionError,
            # crashing every budget request for a user with no profile set.
            savings_percentage = (
                (savings_target / monthly_income) * 100 if monthly_income > 0 else 0.0
            )
            budget_allocation["savings"] = {
                "monthly_amount": savings_target,
                "percentage": savings_percentage,
            }

            return budget_allocation

        except Exception:
            logger.error(
                "Error allocating budget",
                exc_info=True,
            )
            return {}

    async def _generate_budget_recommendations(
        self,
        budget_allocation: dict[str, Any],
        expense_data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Generate budget recommendations."""
        recommendations = []

        try:
            # Check for overspending categories
            for category, allocation in budget_allocation.items():
                if category != "savings":
                    # Compare with recommended percentages
                    recommended_percentages = {
                        "food": 10,
                        "transport": 15,
                        "housing": 30,
                        "entertainment": 10,
                        "shopping": 10,
                        "health": 5,
                        "utilities": 5,
                    }

                    recommended = recommended_percentages.get(category, 10)
                    current_percentage = allocation["percentage"]

                    if current_percentage > recommended + 5:
                        recommendations.append(
                            {
                                "type": "reduce_spending",
                                "category": category,
                                "priority": "high",
                                "description": f"You're spending {current_percentage:.1f}% of your income on {category}, which is significantly higher than the recommended {recommended}%",
                                "suggestion": f"Consider reducing {category} expenses by {current_percentage - recommended:.1f}% points",
                            }
                        )

            # Check savings rate
            savings_percentage = budget_allocation.get("savings", {}).get(
                "percentage", 0
            )
            if savings_percentage < 10:
                recommendations.append(
                    {
                        "type": "increase_savings",
                        "priority": "medium",
                        "description": f"Your savings rate is {savings_percentage:.1f}%, below the recommended minimum of 10%",
                        "suggestion": "Increase your savings rate to build a financial safety net and achieve your goals faster",
                    }
                )

            if not recommendations:
                recommendations.append(
                    {
                        "type": "maintain_good_habits",
                        "priority": "low",
                        "description": "Your budget allocation looks good! You're following recommended spending patterns",
                        "suggestion": "Continue monitoring your spending and consider optimizing your savings rate",
                    }
                )

        except Exception:
            logger.error(
                "Error generating budget recommendations",
                exc_info=True,
            )

        return recommendations

    async def analyze_transaction(
        self, description: str, amount: float | None = None
    ) -> dict[str, Any]:
        """Analyze a single transaction."""
        try:
            # Categorize transaction
            category = self._categorize_transaction(description, amount)

            # Identify transaction type
            transaction_type = self._identify_transaction_type(description, amount)

            # Generate insights
            insights = await self._generate_transaction_insights(
                description, category, transaction_type, amount
            )

            return {
                "description": description,
                "amount": amount,
                "category": category,
                "type": transaction_type,
                "insights": insights,
                "confidence": self._calculate_confidence(
                    description, category, transaction_type
                ),
            }

        except Exception as e:
            logger.error(
                "Error analyzing transaction",
                exc_info=True,
            )
            raise FinancialError(f"Failed to analyze transaction: {str(e)}")

    def _categorize_transaction(self, description: str, amount: float | None) -> str:
        """Categorize a transaction based on description and amount."""
        description_lower = description.lower()

        # Check for specific keywords
        if any(
            keyword in description_lower
            for keyword in ["grocery", "food", "meal", "eat"]
        ):
            return "food"
        elif any(
            keyword in description_lower
            for keyword in ["transport", "car", "gas", "uber", "taxi", "ride"]
        ):
            return "transport"
        elif any(
            keyword in description_lower
            for keyword in ["rent", "mortgage", "housing", "apartment"]
        ):
            return "housing"
        elif any(
            keyword in description_lower
            for keyword in ["movie", "cinema", "entertain", "netflix", "show"]
        ):
            return "entertainment"
        elif any(
            keyword in description_lower
            for keyword in ["shop", "purchase", "buy", "amazon", "mall"]
        ):
            return "shopping"
        elif any(
            keyword in description_lower
            for keyword in ["medical", "doctor", "hospital", "pharmacy", "health"]
        ):
            return "health"
        elif any(
            keyword in description_lower
            for keyword in ["save", "saving", "invest", "savings"]
        ):
            return "savings"
        elif amount and amount > 1000:
            return "large_purchase"
        else:
            return "other"

    def _identify_transaction_type(self, description: str, amount: float | None) -> str:
        """Identify transaction type (expense, income, transfer)."""
        description_lower = description.lower()

        # Check for income keywords
        income_keywords = ["pay", "salary", "income", "bonus", "refund", "interest"]
        if any(keyword in description_lower for keyword in income_keywords):
            return "income"

        # Check for transfer keywords
        transfer_keywords = ["transfer", "wire", "move", "shift"]
        if any(keyword in description_lower for keyword in transfer_keywords):
            return "transfer"

        # Default to expense
        return "expense"

    async def _generate_transaction_insights(
        self,
        description: str,
        category: str,
        transaction_type: str,
        amount: float | None,
    ) -> list[str]:
        """Generate insights for a transaction."""
        insights = []

        try:
            if transaction_type == "expense":
                if amount and amount > 100:
                    insights.append(
                        f"Large expense ({amount:.2f}) in {category} category. Consider reviewing budget allocation."
                    )

                if category == "food" and amount and amount > 50:
                    insights.append(
                        "Food expense is higher than typical. Consider meal planning to reduce costs."
                    )

                if category == "entertainment" and amount and amount > 30:
                    insights.append(
                        "Entertainment expense is above average. Consider free alternatives."
                    )

            elif transaction_type == "income":
                if amount and amount > 5000:
                    insights.append(
                        f"Significant income ({amount:.2f}). Consider increasing savings or investments."
                    )

            # Add category-specific insights
            if category == "shopping" and amount and amount > 200:
                insights.append(
                    "Shopping expense is high this period. Review needs vs wants."
                )

            if not insights:
                insights.append(
                    f"Transaction categorized as {category} {transaction_type}. Track this category for better financial awareness."
                )

        except Exception:
            logger.error(
                "Error generating transaction insights",
                exc_info=True,
            )

        return insights

    def _calculate_confidence(
        self, description: str, category: str, transaction_type: str
    ) -> float:
        """Calculate confidence level for transaction analysis."""
        confidence = 0.5  # Base confidence

        # Increase confidence based on specific keywords
        if any(
            keyword in description.lower()
            for keyword in ["grocery", "restaurant", "uber"]
        ):
            confidence += 0.2

        if transaction_type != "other":
            confidence += 0.2

        # Reduce confidence for vague descriptions
        if len(description.split()) < 3:
            confidence -= 0.1

        return max(0.1, min(1.0, confidence))

    async def analyze_debt_avalanche(
        self, user_id: str, debt_data: dict, monthly_payment: float
    ) -> dict[str, Any]:
        """Analyze and generate debt avalanche payoff strategy.

        This method implements the debt avalanche method where users pay the
        minimum on all debts except the highest interest rate debt, which they
        pay aggressively until paid off, then roll that payment into the next
        highest interest debt.

        Args:
            user_id: The user's ID
            debt_data: List of debt items with interest rates and minimum payments
            monthly_payment: Total available amount for debt repayment

        Returns:
            Dictionary containing the debt avalanche strategy
        """
        try:
            # Sort debts by interest rate (highest first)
            sorted_debts = sorted(
                debt_data,
                key=lambda d: d.get("interest_rate", 0),
                reverse=True,
            )

            # Calculate payoff timeline for each debt
            payoff_schedule = []
            total_months = 0.0
            remaining_payment = monthly_payment

            for debt in sorted_debts:
                balance = debt.get("balance", 0)
                interest_rate = debt.get("interest_rate", 0)
                min_payment = debt.get("minimum_payment", 0)

                # Calculate monthly payment needed to pay this debt off
                monthly_required = min(remaining_payment, min_payment)

                # Calculate months to payoff this debt
                if interest_rate > 0:
                    months = self._calculate_months_to_payoff(
                        balance, monthly_required, interest_rate / 12
                    )
                else:
                    months = (
                        int(balance / monthly_required) if monthly_required > 0 else 0
                    )

                total_months += months
                remaining_payment -= monthly_required

                payoff_schedule.append(
                    {
                        "debt_id": debt.get("id", "unknown"),
                        "name": debt.get("name", "Unknown Debt"),
                        "balance": balance,
                        "interest_rate": interest_rate,
                        "minimum_payment": min_payment,
                        "monthly_payment": monthly_required,
                        "payoff_time_months": months,
                        "total_interest_paid": self._calculate_total_interest(
                            balance, monthly_required, months, interest_rate / 12
                        ),
                    }
                )

                if remaining_payment <= 0:
                    break

            # Calculate total strategy metrics
            total_interest_saved = self._calculate_total_interest_saved(sorted_debts)
            total_time_months = sum(
                schedule["payoff_time_months"] for schedule in payoff_schedule
            )

            return {
                "strategy": "debt_avalanche",
                "payoff_schedule": payoff_schedule,
                "total_months": total_time_months,
                "total_interest_saved": total_interest_saved,
                "total_cost": sum(
                    schedule["balance"] + schedule["total_interest_paid"]
                    for schedule in payoff_schedule
                ),
                "savings_percentage": (
                    (
                        (
                            total_interest_saved
                            / sum(d.get("balance", 0) for d in sorted_debts)
                        )
                        * 100
                    )
                    if sum(d.get("balance", 0) for d in sorted_debts) > 0
                    else 0
                ),
                "recommendations": self._generate_debt_avalanche_recommendations(
                    payoff_schedule, monthly_payment
                ),
            }

        except Exception as e:
            logger.error(
                "Error analyzing debt avalanche strategy",
                exc_info=True,
            )
            raise FinancialError(f"Failed to analyze debt avalanche strategy: {str(e)}")

    def _calculate_months_to_payoff(
        self, balance: float, monthly_payment: float, monthly_rate: float
    ) -> float:
        """Calculate number of months required to pay off a debt.

        Uses the standard loan amortization formula to calculate the time
        required to pay off a debt given the balance, monthly payment, and
        monthly interest rate.
        """
        if monthly_payment <= balance * monthly_rate:
            return float("inf")

        numerator = balance * (1 + monthly_rate) ** 10000 * (1 + monthly_rate - 1)
        denominator = ((1 + monthly_rate) ** 10000 - 1) * monthly_payment

        if denominator <= 0:
            return float("inf")

        months = numerator / denominator
        return int(round(months))

    def _calculate_total_interest(
        self,
        principal: float,
        monthly_payment: float,
        months: float,
        monthly_rate: float,
    ) -> float:
        """Calculate total interest paid over the life of a loan."""
        total_paid = monthly_payment * months
        total_interest = total_paid - principal
        return max(0, total_interest)

    def _calculate_total_interest_saved(self, debts: list[dict[str, Any]]) -> float:
        """Calculate total interest saved by using debt avalanche vs minimum payments."""
        try:
            # Calculate total interest with minimum payments only
            total_min_interest = sum(
                self._calculate_min_payment_interest(debt) for debt in debts
            )

            # Calculate total interest with avalanche method
            sorted_debts = sorted(
                (debt for debt in debts if debt.get("balance", 0) > 0),
                key=lambda d: d.get("interest_rate", 0),
                reverse=True,
            )
            remaining_payment = 1000.0  # Example monthly payment

            avalanche_interest = 0.0
            for debt in sorted_debts:
                balance = debt.get("balance", 0)
                interest_rate = debt.get("interest_rate", 0)
                min_payment = debt.get("minimum_payment", 0)

                monthly_required = min(remaining_payment, min_payment)
                months = self._calculate_months_to_payoff(
                    balance, monthly_required, interest_rate / 12
                )

                total_interest = self._calculate_total_interest(
                    balance, monthly_required, months, interest_rate / 12
                )
                avalanche_interest += total_interest

                remaining_payment -= monthly_required
                if remaining_payment <= 0:
                    break

            return total_min_interest - avalanche_interest

        except Exception:
            logger.error(
                "Error calculating total interest saved",
                exc_info=True,
            )
            return 0.0

    def _calculate_min_payment_interest(self, debt: dict[str, Any]) -> float:
        """Calculate total interest paid if only minimum payments are made."""
        try:
            balance = debt.get("balance", 0)
            interest_rate = debt.get("interest_rate", 0)
            min_payment = debt.get("minimum_payment", 0)

            if min_payment <= 0 or interest_rate <= 0:
                return 0.0

            # Estimate months to payoff (simplified calculation)
            months = int(balance / min_payment) if min_payment > 0 else 0

            return self._calculate_total_interest(
                balance, min_payment, months, interest_rate / 12
            )

        except Exception:
            logger.error(
                "Error calculating minimum payment interest",
                exc_info=True,
            )
            return 0.0

    def _generate_debt_avalanche_recommendations(
        self, payoff_schedule: list[dict[str, Any]], monthly_payment: float
    ) -> list[str]:
        """Generate recommendations based on debt avalanche strategy."""
        recommendations = []

        try:
            # Find highest interest debt
            if payoff_schedule:
                highest_interest_debt = max(
                    payoff_schedule, key=lambda d: d.get("interest_rate", 0)
                )

                recommendations.append(
                    f"Prioritize paying off {highest_interest_debt['name']} first "
                    f"(interest rate: {highest_interest_debt['interest_rate'] * 100:.1f}%). "
                    f"Allocate ${monthly_payment:.2f} monthly toward this debt."
                )

                # Calculate total time saved
                total_avalanche_time = sum(
                    d["payoff_time_months"] for d in payoff_schedule
                )
                total_min_payment_time = sum(
                    (
                        d["balance"] / d["minimum_payment"]
                        if d["minimum_payment"] > 0
                        else 0
                    )
                    for d in payoff_schedule
                )

                time_saved = total_min_payment_time - total_avalanche_time
                if time_saved > 0:
                    years_saved = time_saved / 12
                    recommendations.append(
                        f"Debt avalanche will save approximately {years_saved:.1f} years "
                        f"compared to paying minimum amounts only."
                    )

            recommendations.append(
                "Consider consolidating high-interest debt to reduce interest rates."
            )
            recommendations.append(
                "Build an emergency fund before aggressively paying down debt."
            )
            recommendations.append(
                "Track your spending to free up more money for debt repayment."
            )

        except Exception:
            logger.error(
                "Error generating debt avalanche recommendations",
                exc_info=True,
            )

        return recommendations

    async def generate_debt_paydown_strategy(
        self, user_id: str, debt_data: dict, monthly_income: float
    ) -> dict[str, Any]:
        """Generate a comprehensive debt payoff strategy.

        This method provides a detailed debt payoff strategy using the debt
        avalanche method, including calculations, timelines, and recommendations.
        """
        try:
            # Calculate available monthly payment
            current_expenses = await self._get_current_monthly_expenses(user_id)
            monthly_savings = self._calculate_monthly_savings_target(
                monthly_income, debt_data
            )
            available_for_debt = (
                monthly_income
                - current_expenses
                - monthly_savings
                - await self._calculate_fixed_monthly_obligations(user_id)
            )

            monthly_payment = max(0, available_for_debt)

            # Analyze debt avalanche strategy
            avalanche_analysis = await self.analyze_debt_avalanche(
                user_id, debt_data, monthly_payment
            )

            # Calculate financial goals
            financial_goals = self._calculate_financial_goals(debt_data, monthly_income)

            # Generate recommendations
            recommendations = await self._generate_comprehensive_debt_recommendations(
                avalanche_analysis, financial_goals
            )

            return {
                "strategy_type": "debt_avalanche",
                "monthly_payment": round(monthly_payment, 2),
                "avalanche_analysis": avalanche_analysis,
                "financial_goals": financial_goals,
                "recommendations": recommendations,
                "expected_timeline_months": avalanche_analysis["total_months"],
                "total_interest_saved": avalanche_analysis["total_interest_saved"],
                "next_steps": [
                    "Set up automatic transfers for debt payments",
                    "Track progress regularly",
                    "Review and adjust strategy as needed",
                    "Consider debt consolidation if interest rates are high",
                ],
            }

        except Exception as e:
            logger.error(
                "Error generating debt payoff strategy",
                exc_info=True,
            )
            raise FinancialError(f"Failed to generate debt payoff strategy: {str(e)}")

    async def _get_current_monthly_expenses(self, user_id: str) -> float:
        """Get current monthly expenses from the user's financial data."""
        try:
            expense_data = await self.memory_store.get_expense_data(user_id)

            # Calculate total monthly expenses
            monthly_expenses = 0.0
            for category, data in expense_data.get("category_expenses", {}).items():
                monthly_expenses += data.get("monthly_amount", 0)

            return monthly_expenses

        except Exception:
            logger.error(
                "Error getting current monthly expenses",
                exc_info=True,
            )
            return 0.0

    def _calculate_monthly_savings_target(
        self, monthly_income: float, debt_data: dict
    ) -> float:
        """Calculate monthly savings target."""
        try:
            # Basic savings target: 10% of monthly income
            savings_target = monthly_income * 0.10

            # Adjust based on debt situation
            total_debt = sum(
                d.get("balance", 0) for d in debt_data if d.get("balance", 0) > 0
            )

            if total_debt > 10000:
                # Higher savings for high debt
                savings_target = max(savings_target, monthly_income * 0.15)
            elif total_debt > 5000:
                # Moderate savings for medium debt
                savings_target = max(savings_target, monthly_income * 0.12)

            return savings_target

        except Exception:
            logger.error(
                "Error calculating monthly savings target",
                exc_info=True,
            )
            return monthly_income * 0.10

    async def _calculate_fixed_monthly_obligations(self, user_id: str) -> float:
        """Calculate fixed monthly obligations (rent, utilities, insurance, etc.)."""
        try:
            # Get user profile for fixed obligations
            profile = await self.memory_store.get_financial_profile(user_id)

            if not profile:
                return 0.0

            # Sum up fixed obligations
            fixed_obligations = 0.0

            # Add housing costs
            if profile.get("housing_cost"):
                fixed_obligations += profile["housing_cost"]

            # Add insurance costs
            if profile.get("insurance_cost"):
                fixed_obligations += profile["insurance_cost"]

            # Add other regular obligations
            for obligation in profile.get("regular_obligations", []):
                fixed_obligations += obligation.get("amount", 0)

            return fixed_obligations

        except Exception:
            logger.error(
                "Error calculating fixed monthly obligations",
                exc_info=True,
            )
            return 0.0

    def _calculate_financial_goals(
        self, debt_data: dict, monthly_income: float
    ) -> dict[str, Any]:
        """Calculate financial goals based on debt situation."""
        try:
            total_debt = sum(
                d.get("balance", 0) for d in debt_data if d.get("balance", 0) > 0
            )

            goals = []

            if total_debt > 10000:
                goals.append(
                    {
                        "type": "debt_freedom",
                        "target": f"Pay off ${total_debt:,.0f} debt",
                        "timeline_months": int(total_debt / (monthly_income * 0.15)),
                        "priority": "high",
                    }
                )
            elif total_debt > 5000:
                goals.append(
                    {
                        "type": "debt_reduction",
                        "target": f"Reduce debt to ${total_debt/2:,.0f}",
                        "timeline_months": int(
                            (total_debt / 2) / (monthly_income * 0.10)
                        ),
                        "priority": "medium",
                    }
                )
            else:
                goals.append(
                    {
                        "type": "debt_management",
                        "target": "Manage existing debt effectively",
                        "timeline_months": 12,
                        "priority": "low",
                    }
                )

            # Add savings goal
            savings_goal = monthly_income * 0.10
            goals.append(
                {
                    "type": "emergency_fund",
                    "target": f"Build ${savings_goal:,.0f} emergency fund",
                    "timeline_months": 12,
                    "priority": "high",
                }
            )

            return {"goals": goals, "total_debt": total_debt}

        except Exception:
            logger.error(
                "Error calculating financial goals",
                exc_info=True,
            )
            return {"goals": [], "total_debt": 0}

    async def _generate_comprehensive_debt_recommendations(
        self,
        avalanche_analysis: dict[str, Any],
        financial_goals: dict[str, Any],
    ) -> list[str]:
        """Generate comprehensive debt management recommendations."""
        recommendations = []

        try:
            # Add avalanche strategy recommendations
            recommendations.extend(avalanche_analysis.get("recommendations", []))

            # Add financial goal recommendations
            for goal in financial_goals.get("goals", []):
                recommendations.append(
                    f"{goal['priority'].title()} Priority: {goal['target']} "
                    f"(Timeline: {goal['timeline_months']} months)"
                )

            # Add general debt management recommendations
            recommendations.extend(
                [
                    "Create a realistic budget that includes debt payments",
                    "Track all expenses to identify areas for cost reduction",
                    "Consider debt consolidation if interest rates are high",
                    "Build an emergency fund before aggressively paying down debt",
                    "Stay consistent with debt repayment plan",
                    "Celebrate milestones and small victories along the way",
                ]
            )

        except Exception:
            logger.error(
                "Error generating comprehensive debt recommendations",
                exc_info=True,
            )

        return recommendations

    async def get_financial_advice(
        self, user_id: str, context: str = "general"
    ) -> dict[str, Any]:
        """Get personalized financial advice."""
        try:
            # Get user financial profile
            profile = await self.memory_store.get_financial_profile(user_id)

            # Get current financial situation
            portfolio_data = await self.memory_store.get_portfolio_data(
                user_id, "month"
            )
            income_data = await self.memory_store.get_income_data(user_id)
            expense_data = await self.memory_store.get_expense_data(user_id)

            # Generate advice based on context
            advice = {
                "recommendations": [],
                "priority": "low",
                "next_steps": [],
            }

            # Generate advice based on context
            if context == "budgeting":
                advice = await self._generate_budgeting_advice(
                    profile, income_data, expense_data
                )
            elif context == "investing":
                advice = await self._generate_investing_advice(profile, portfolio_data)
            elif context == "debt":
                advice = await self._generate_debt_advice(
                    profile, income_data, expense_data
                )
            else:
                advice = await self._generate_general_advice(
                    profile, portfolio_data, income_data, expense_data
                )

            return advice

        except Exception as e:
            logger.error(
                "Error generating financial advice",
                exc_info=True,
            )
            raise FinancialError(f"Failed to generate financial advice: {str(e)}")

    async def _generate_budgeting_advice(
        self, profile: Any, income_data: dict[str, Any], expense_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate budgeting-specific advice."""
        recommendations = []
        next_steps = []

        try:
            # Calculate current budget metrics
            monthly_income = self._calculate_monthly_income(income_data)
            monthly_expense_totals = self._calculate_monthly_expenses(expense_data)
            monthly_expenses = sum(monthly_expense_totals.values())
            savings_rate = (
                (monthly_income - monthly_expenses) / monthly_income * 100
                if monthly_income > 0
                else 0
            )

            # Generate recommendations
            if savings_rate < 10:
                recommendations.append(
                    f"Your savings rate is {savings_rate:.1f}%, below the recommended minimum of 10%. Increase savings by setting up automatic transfers."
                )
                next_steps.append(
                    "Set up an automatic savings transfer of 10% of your income"
                )

            # Check for overspending categories
            expense_categories = expense_data.get("category_expenses", {})
            for category, category_data in expense_categories.items():
                monthly_amount = category_data["monthly_amount"]
                percentage = (monthly_amount / monthly_income) * 100

                if percentage > 20:
                    recommendations.append(
                        f"You're spending {percentage:.1f}% of your income on {category}. Consider reducing this category."
                    )
                    next_steps.append(
                        f"Create a 20% budget limit for {category} expenses"
                    )

            # Default advice if no specific recommendations
            if not recommendations:
                recommendations.append(
                    f"Your budget looks good with a savings rate of {savings_rate:.1f}%. Continue monitoring your expenses and consider automating your savings."
                )
                next_steps.append(
                    "Set up automatic savings to maintain your savings rate"
                )

        except Exception:
            logger.error(
                "Error generating budgeting advice",
                exc_info=True,
            )

        return {
            "recommendations": recommendations,
            "priority": "high" if len(recommendations) > 2 else "medium",
            "next_steps": next_steps,
        }

    async def _generate_investing_advice(
        self, profile: Any, portfolio_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate investing-specific advice."""
        recommendations = []
        next_steps = []

        try:
            # Calculate diversification score
            diversification_score = self._calculate_diversification_score(
                portfolio_data
            )

            if diversification_score < 0.7:
                recommendations.append(
                    f"Your portfolio has a low diversification score of {diversification_score:.2f}. Consider diversifying across different asset classes and sectors."
                )
                next_steps.append(
                    "Rebalance your portfolio to achieve better diversification"
                )

            # Get performance metrics
            performance = self._calculate_portfolio_performance(portfolio_data, "month")

            if performance.get("sharpe_ratio", 0) < 1.0:
                recommendations.append(
                    f"Your portfolio's risk-adjusted return (Sharpe ratio) is {performance.get('sharpe_ratio', 0):.2f}, below the ideal of 1.0. Consider adjusting your asset allocation."
                )
                next_steps.append(
                    "Review and optimize your asset allocation for better risk-adjusted returns"
                )

            # Default advice
            if not recommendations:
                recommendations.append(
                    "Your investment portfolio shows good diversification and performance. Continue with your current strategy and consider tax-loss harvesting if you have losses."
                )
                next_steps.append(
                    "Continue monitoring your investments and consider annual tax-loss harvesting"
                )

        except Exception:
            logger.error(
                "Error generating investing advice",
                exc_info=True,
            )

        return {
            "recommendations": recommendations,
            "priority": "high" if len(recommendations) > 2 else "medium",
            "next_steps": next_steps,
        }

    async def _generate_debt_advice(
        self, profile: Any, income_data: dict[str, Any], expense_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate debt-specific advice."""
        recommendations = []
        next_steps = []

        try:
            # Calculate debt-to-income ratio
            monthly_income = self._calculate_monthly_income(income_data)

            # Get debt information from profile
            debt_info = getattr(profile, "debt_info", {})
            monthly_debt_payment = sum(
                debt.get("monthly_payment", 0) for debt in debt_info.get("debts", [])
            )

            debt_to_income = (
                (monthly_debt_payment / monthly_income * 100)
                if monthly_income > 0
                else 0
            )

            if debt_to_income > 40:
                recommendations.append(
                    f"Your debt-to-income ratio is {debt_to_income:.1f}%, which is above the recommended 40%. Consider debt consolidation or refinancing."
                )
                next_steps.append(
                    "Research debt consolidation options to lower your interest rates"
                )

            # Find highest interest debt
            highest_interest_debt = max(
                debt_info.get("debts", []),
                key=lambda d: d.get("interest_rate", 0),
                default=None,
            )

            if highest_interest_debt:
                recommendations.append(
                    f"You have debt with {highest_interest_debt.get('interest_rate', 0):.1f}% interest rate. Consider paying this off first (avalanche method)."
                )
                next_steps.append(
                    "Create a debt avalanche payment plan to eliminate high-interest debt"
                )

            # Default advice
            if not recommendations:
                recommendations.append(
                    "You're managing your debt well with a reasonable debt-to-income ratio. Continue making regular payments and consider building an emergency fund."
                )
                next_steps.append(
                    "Build an emergency fund to avoid new debt in case of unexpected expenses"
                )

        except Exception:
            logger.error(
                "Error generating debt advice",
                exc_info=True,
            )

        return {
            "recommendations": recommendations,
            "priority": "high" if len(recommendations) > 2 else "medium",
            "next_steps": next_steps,
        }

    async def _generate_general_advice(
        self,
        profile: Any,
        portfolio_data: dict[str, Any],
        income_data: dict[str, Any],
        expense_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate general financial advice."""
        recommendations = []
        next_steps = []

        try:
            # Calculate overall financial health score
            health_score = self._calculate_financial_health_score(
                profile, portfolio_data, income_data, expense_data
            )

            if health_score < 70:
                recommendations.append(
                    f"Your overall financial health score is {health_score:.1f}/100. Consider focusing on improving your financial situation."
                )
                next_steps.append(
                    "Create a comprehensive financial plan to improve your overall financial health"
                )

            # Check for emergency fund
            if not getattr(profile, "has_emergency_fund", False):
                recommendations.append(
                    "You don't have an emergency fund. Build one with 3-6 months of expenses saved."
                )
                next_steps.append(
                    "Start building an emergency fund by setting up automatic savings"
                )

            # Check retirement planning
            if not getattr(profile, "has_retirement_plan", False):
                recommendations.append(
                    "You don't have a retirement plan yet. Start contributing to a retirement account as early as possible."
                )
                next_steps.append(
                    "Open a retirement account and set up automatic contributions"
                )

            # Default advice
            if not recommendations:
                recommendations.append(
                    "Your financial situation looks good! Continue with your current financial habits and consider exploring investment opportunities to grow your wealth."
                )
                next_steps.append(
                    "Consider exploring investment options to grow your wealth further"
                )

        except Exception:
            logger.error(
                "Error generating general advice",
                exc_info=True,
            )

        return {
            "recommendations": recommendations,
            "priority": "high" if len(recommendations) > 2 else "medium",
            "next_steps": next_steps,
        }

    def _calculate_financial_health_score(
        self,
        profile: Any,
        portfolio_data: dict[str, Any],
        income_data: dict[str, Any],
        expense_data: dict[str, Any],
    ) -> float:
        """Calculate overall financial health score (0-100)."""
        try:
            score = 0.0

            # Calculate savings rate (0-40 points)
            monthly_income = self._calculate_monthly_income(income_data)
            monthly_expense_totals = self._calculate_monthly_expenses(expense_data)
            monthly_expenses = sum(monthly_expense_totals.values())
            savings_rate = (
                (monthly_income - monthly_expenses) / monthly_income * 100
                if monthly_income > 0
                else 0
            )
            score += min(savings_rate * 0.4, 40)

            # Calculate diversification score (0-20 points)
            diversification_score = self._calculate_diversification_score(
                portfolio_data
            )
            score += diversification_score * 20

            # Check for emergency fund (0-20 points)
            if getattr(profile, "has_emergency_fund", False):
                emergency_fund_months = getattr(profile, "emergency_fund_months", 0)
                score += min(emergency_fund_months * 5, 20)

            # Check for retirement planning (0-20 points)
            if getattr(profile, "has_retirement_plan", False):
                retirement_score = getattr(profile, "retirement_score", 0)
                score += min(retirement_score * 2, 20)

            # Check debt level (0-20 points)
            debt_info = getattr(profile, "debt_info", {})
            total_debt = sum(
                debt.get("amount", 0) for debt in debt_info.get("debts", [])
            )
            debt_to_income = (
                (total_debt / (monthly_income * 10)) * 100 if monthly_income > 0 else 0
            )
            score += max(0, 100 - debt_to_income) * 0.2

            return score

        except Exception:
            logger.error(
                "Error calculating financial health score",
                exc_info=True,
            )
            return 0.0

    def _calculate_daily_returns(self, portfolio_data: dict[str, Any]) -> list[float]:
        """Calculate daily returns from portfolio data.

        Bug fix: this was declared ``async`` but does no actual awaiting,
        and its only caller (``_calculate_portfolio_performance``, a sync
        method) called it without ``await`` -- so ``returns`` was always a
        coroutine object, not a list, and every downstream numpy call on it
        raised (silently turning into a `{"error": ...}` result because the
        surrounding except swallowed it).
        """
        try:
            # This is a simplified calculation
            # In a real implementation, you would get actual historical price data

            returns = []

            # Get historical price data for each holding
            for holding in portfolio_data.get("holdings", []):
                # This would normally fetch actual price data
                # For now, use a simple random return for demonstration
                daily_return = np.random.normal(0.001, 0.02)  # 0.1% mean, 2% volatility
                returns.append(daily_return)

            return returns

        except Exception:
            logger.error(
                "Error calculating daily returns",
                exc_info=True,
            )
            return []

    async def generate_strategy(
        self, user_id: str, goal: str, timeframe: str = "long_term"
    ):
        """Generate a financial strategy for the user."""
        try:
            # Get user financial profile
            profile = await self.memory_store.get_financial_profile(user_id)

            # Generate strategy based on goal
            strategy = self._generate_strategy_for_goal(goal, profile, timeframe)

            # Add execution steps
            strategy["execution_steps"] = await self._generate_execution_steps(
                strategy, profile
            )

            return strategy

        except Exception as e:
            logger.error(
                "Error generating strategy",
                exc_info=True,
            )
            raise FinancialError(f"Failed to generate strategy: {str(e)}")

    def _generate_strategy_for_goal(
        self, goal: str, profile: Any, timeframe: str
    ) -> dict[str, Any]:
        """Generate a strategy based on goal."""
        strategies = {
            "retirement": self._generate_retirement_strategy(profile, timeframe),
            "home_purchase": self._generate_home_purchase_strategy(profile, timeframe),
            "emergency_fund": self._generate_emergency_fund_strategy(
                profile, timeframe
            ),
            "debt_paydown": self._generate_debt_paydown_strategy(profile, timeframe),
            "wealth_building": self._generate_wealth_building_strategy(
                profile, timeframe
            ),
            "income_generation": self._generate_income_generation_strategy(
                profile, timeframe
            ),
        }

        return strategies.get(goal, self._generate_generic_strategy(profile, timeframe))

    def _generate_retirement_strategy(
        self, profile: Any, timeframe: str
    ) -> dict[str, Any]:
        """Generate retirement strategy."""
        return {
            "goal_type": "retirement",
            "target_amount": self._calculate_retirement_target(profile, timeframe),
            "allocation": {
                "stocks": 0.7,
                "bonds": 0.2,
                "cash": 0.1,
            },
            "contributions": {
                "monthly": self._calculate_monthly_retirement_contribution(profile),
                "annual_increase": 0.03,
            },
            "risk_level": "medium_to_high",
            "timeline_years": self._calculate_retirement_timeline(profile),
        }

    def _generate_home_purchase_strategy(
        self, profile: Any, timeframe: str
    ) -> dict[str, Any]:
        """Generate home purchase strategy."""
        return {
            "goal_type": "home_purchase",
            "down_payment_target": self._calculate_home_down_payment_target(profile),
            "savings_plan": {
                "monthly_savings": self._calculate_monthly_home_savings(profile),
                "timeline_months": self._calculate_home_purchase_timeline(profile),
            },
            "credit_score_target": 720,
            "debt_to_income_ratio": 0.36,
        }

    def _generate_emergency_fund_strategy(
        self, profile: Any, timeframe: str
    ) -> dict[str, Any]:
        """Generate emergency fund strategy."""
        return {
            "goal_type": "emergency_fund",
            "target_months": 6,
            "monthly_savings": self._calculate_monthly_emergency_savings(profile),
            "timeline_months": 12,
            "fund_placement": "high_yield_savings",
        }

    def _generate_debt_paydown_strategy(
        self, profile: Any, timeframe: str
    ) -> dict[str, Any]:
        """Generate debt paydown strategy."""
        debt_info = getattr(profile, "debt_info", {})

        return {
            "goal_type": "debt_paydown",
            "total_debt": sum(
                debt.get("amount", 0) for debt in debt_info.get("debts", [])
            ),
            "strategy": "avalanche",
            "highest_interest_debt": self._get_highest_interest_debt(debt_info),
            "monthly_payment": self._calculate_monthly_debt_payment(profile),
            "timeline_months": self._calculate_debt_paydown_timeline(profile),
        }

    def _generate_wealth_building_strategy(
        self, profile: Any, timeframe: str
    ) -> dict[str, Any]:
        """Generate wealth building strategy."""
        return {
            "goal_type": "wealth_building",
            "target_amount": self._calculate_wealth_building_target(profile),
            "investment_strategy": {
                "equity_percentage": 0.7,
                "bond_percentage": 0.2,
                "alternative_percentage": 0.1,
            },
            "monthly_contribution": self._calculate_monthly_wealth_contribution(
                profile
            ),
            "expected_return": 0.08,
        }

    def _generate_income_generation_strategy(
        self, profile: Any, timeframe: str
    ) -> dict[str, Any]:
        """Generate income generation strategy."""
        return {
            "goal_type": "income_generation",
            "target_monthly_income": self._calculate_income_generation_target(profile),
            "strategies": [
                "dividend_investments",
                "rental_properties",
                "business_investments",
            ],
            "initial_investment": self._calculate_initial_income_investment(profile),
        }

    def _generate_generic_strategy(
        self, profile: Any, timeframe: str
    ) -> dict[str, Any]:
        """Generate generic strategy."""
        return {
            "goal_type": "general",
            "focus_areas": ["budgeting", "savings", "debt_management"],
            "priority": "medium",
        }

    async def _generate_execution_steps(
        self, strategy: dict[str, Any], profile: Any
    ) -> list[dict[str, Any]]:
        """Generate execution steps for a strategy."""
        steps = []

        try:
            goal_type = strategy.get("goal_type")

            if goal_type == "retirement":
                steps = await self._generate_retirement_steps(strategy, profile)
            elif goal_type == "home_purchase":
                steps = await self._generate_home_purchase_steps(strategy, profile)
            elif goal_type == "emergency_fund":
                steps = await self._generate_emergency_fund_steps(strategy, profile)
            elif goal_type == "debt_paydown":
                steps = await self._generate_debt_paydown_steps(strategy, profile)
            elif goal_type == "wealth_building":
                steps = await self._generate_wealth_building_steps(strategy, profile)
            elif goal_type == "income_generation":
                steps = await self._generate_income_generation_steps(strategy, profile)

            return steps

        except Exception:
            logger.error(
                "Error generating execution steps",
                exc_info=True,
            )
            return []

    async def _generate_retirement_steps(
        self, strategy: dict[str, Any], profile: Any
    ) -> list[dict[str, Any]]:
        """Generate retirement execution steps."""
        steps = []

        try:
            monthly_contribution = strategy.get("contributions", {}).get("monthly", 0)
            equity_allocation = strategy.get("allocation", {}).get("stocks", 0)

            steps = [
                {
                    "step": 1,
                    "description": f"Set up automatic monthly contribution of ${monthly_contribution:.2f}",
                    "priority": "high",
                    "estimated_time": "immediate",
                    "action": "setup_automatic_transfer",
                },
                {
                    "step": 2,
                    "description": f"Diversify investments with {equity_allocation * 100:.0f}% in equities",
                    "priority": "medium",
                    "estimated_time": "1-2 weeks",
                    "action": "rebalance_portfolio",
                },
                {
                    "step": 3,
                    "description": "Review and adjust contributions annually",
                    "priority": "medium",
                    "estimated_time": "annually",
                    "action": "annual_review",
                },
            ]

        except Exception:
            logger.error(
                "Error generating retirement steps",
                exc_info=True,
            )

        return steps

    async def _generate_home_purchase_steps(
        self, strategy: dict[str, Any], profile: Any
    ) -> list[dict[str, Any]]:
        """Generate home purchase execution steps."""
        steps = []

        try:
            monthly_savings = strategy.get("savings_plan", {}).get("monthly_savings", 0)

            steps = [
                {
                    "step": 1,
                    "description": f"Start monthly savings plan of ${monthly_savings:.2f}",
                    "priority": "high",
                    "estimated_time": "immediate",
                    "action": "setup_savings_plan",
                },
                {
                    "step": 2,
                    "description": "Monitor credit score and work toward 720+",
                    "priority": "medium",
                    "estimated_time": "3-6 months",
                    "action": "credit_improvement",
                },
                {
                    "step": 3,
                    "description": "Save for down payment and pre-approval",
                    "priority": "medium",
                    "estimated_time": "6-12 months",
                    "action": "home_preparation",
                },
            ]

        except Exception:
            logger.error(
                "Error generating home purchase steps",
                exc_info=True,
            )

        return steps

    async def _generate_emergency_fund_steps(
        self, strategy: dict[str, Any], profile: Any
    ) -> list[dict[str, Any]]:
        """Generate emergency fund execution steps."""
        steps = []

        try:
            monthly_savings = strategy.get("monthly_savings", 0)
            timeline_months = strategy.get("timeline_months", 0)

            steps = [
                {
                    "step": 1,
                    "description": f"Set up emergency fund savings of ${monthly_savings:.2f} per month",
                    "priority": "high",
                    "estimated_time": "immediate",
                    "action": "setup_emergency_fund",
                },
                {
                    "step": 2,
                    "description": f"Build fund over {timeline_months} months to reach target",
                    "priority": "medium",
                    "estimated_time": f"{timeline_months} months",
                    "action": "accumulate_fund",
                },
                {
                    "step": 3,
                    "description": "Place funds in high-yield savings account",
                    "priority": "medium",
                    "estimated_time": "1 week",
                    "action": "fund_placement",
                },
            ]

        except Exception:
            logger.error(
                "Error generating emergency fund steps",
                exc_info=True,
            )

        return steps

    async def _generate_debt_paydown_steps(
        self, strategy: dict[str, Any], profile: Any
    ) -> list[dict[str, Any]]:
        """Generate debt paydown execution steps."""
        steps = []

        try:
            monthly_payment = strategy.get("monthly_payment", 0)
            highest_debt = strategy.get("highest_interest_debt", {})

            steps = [
                {
                    "step": 1,
                    "description": f"Implement debt avalanche method with ${monthly_payment:.2f} monthly payment",
                    "priority": "high",
                    "estimated_time": "immediate",
                    "action": "setup_avalanche_plan",
                },
                {
                    "step": 2,
                    "description": f"Prioritize paying off {highest_debt.get('name', 'high-interest debt')}",
                    "priority": "high",
                    "estimated_time": "3-12 months",
                    "action": "prioritize_debt",
                },
                {
                    "step": 3,
                    "description": "Celebrate milestones and adjust strategy as needed",
                    "priority": "medium",
                    "estimated_time": "ongoing",
                    "action": "milestone_tracking",
                },
            ]

        except Exception:
            logger.error(
                "Error generating debt paydown steps",
                exc_info=True,
            )

        return steps

    async def _generate_wealth_building_steps(
        self, strategy: dict[str, Any], profile: Any
    ) -> list[dict[str, Any]]:
        """Generate wealth building execution steps."""
        steps = []

        try:
            monthly_contribution = strategy.get("monthly_contribution", 0)
            expected_return = strategy.get("expected_return", 0.08)

            steps = [
                {
                    "step": 1,
                    "description": f"Set up automatic monthly investment of ${monthly_contribution:.2f}",
                    "priority": "high",
                    "estimated_time": "immediate",
                    "action": "setup_auto_investment",
                },
                {
                    "step": 2,
                    "description": f"Invest in diversified portfolio with expected {expected_return * 100:.1f}% annual return",
                    "priority": "medium",
                    "estimated_time": "1-2 weeks",
                    "action": "portfolio_setup",
                },
                {
                    "step": 3,
                    "description": "Review investment performance and rebalance annually",
                    "priority": "medium",
                    "estimated_time": "annually",
                    "action": "annual_review",
                },
            ]

        except Exception:
            logger.error(
                "Error generating wealth building steps",
                exc_info=True,
            )

        return steps

    async def _generate_income_generation_steps(
        self, strategy: dict[str, Any], profile: Any
    ) -> list[dict[str, Any]]:
        """Generate income generation execution steps."""
        steps = []

        try:
            initial_investment = strategy.get("initial_investment", 0)

            steps = [
                {
                    "step": 1,
                    "description": f"Allocate initial investment of ${initial_investment:.2f}",
                    "priority": "high",
                    "estimated_time": "immediate",
                    "action": "setup_income_investments",
                },
                {
                    "step": 2,
                    "description": "Research and select income-generating investments",
                    "priority": "medium",
                    "estimated_time": "2-4 weeks",
                    "action": "investment_research",
                },
                {
                    "step": 3,
                    "description": "Monitor income streams and reinvest profits",
                    "priority": "medium",
                    "estimated_time": "ongoing",
                    "action": "income_monitoring",
                },
            ]

        except Exception:
            logger.error(
                "Error generating income generation steps",
                exc_info=True,
            )

        return steps

    # Helper methods for strategy calculations

    def _calculate_retirement_target(self, profile: Any, timeframe: str) -> float:
        """Calculate retirement target amount."""
        try:
            annual_expenses = getattr(profile, "annual_expenses", 50000)

            # Simple calculation: need 25x annual expenses for retirement
            target_amount = annual_expenses * 25

            return target_amount

        except Exception:
            logger.error(
                "Error calculating retirement target",
                exc_info=True,
            )
            return 0.0

    def _calculate_monthly_retirement_contribution(self, profile: Any) -> float:
        """Calculate monthly retirement contribution."""
        try:
            monthly_income = getattr(profile, "monthly_income", 10000)
            current_age = getattr(profile, "current_age", 30)
            retirement_age = getattr(profile, "retirement_age", 65)

            # Rule of thumb: save 15% of pre-tax income
            contribution_rate = 0.15

            # Adjust based on age (lower if closer to retirement)
            years_to_retirement = retirement_age - current_age
            if years_to_retirement < 20:
                contribution_rate += 0.05
            elif years_to_retirement < 30:
                contribution_rate += 0.03

            return monthly_income * contribution_rate

        except Exception:
            logger.error(
                "Error calculating monthly retirement contribution",
                exc_info=True,
            )
            return 0.0

    def _calculate_retirement_timeline(self, profile: Any) -> int:
        """Calculate retirement timeline in years."""
        try:
            current_age = getattr(profile, "current_age", 30)
            retirement_age = getattr(profile, "retirement_age", 65)

            return retirement_age - current_age

        except Exception:
            logger.error(
                "Error calculating retirement timeline",
                exc_info=True,
            )
            return 0

    def _calculate_home_down_payment_target(self, profile: Any) -> float:
        """Calculate home down payment target."""
        try:
            current_savings = getattr(profile, "current_savings", 0)
            target_down_payment_percentage = 0.20  # 20% down payment

            # Estimate home price based on income
            monthly_income = getattr(profile, "monthly_income", 10000)
            estimated_home_price = monthly_income * 120  # Assume 10x monthly income

            target_amount = estimated_home_price * target_down_payment_percentage

            # If already have savings, reduce target
            if current_savings > 0:
                target_amount = max(0, target_amount - current_savings)

            return target_amount

        except Exception:
            logger.error(
                "Error calculating home down payment target",
                exc_info=True,
            )
            return 0.0

    def _calculate_monthly_home_savings(self, profile: Any) -> float:
        """Calculate monthly home savings."""
        try:
            monthly_income = getattr(profile, "monthly_income", 10000)
            target_down_payment = self._calculate_home_down_payment_target(profile)

            if target_down_payment <= 0:
                return 0.0

            # Calculate timeline based on timeframe (default 5 years)
            timeline_years = getattr(profile, "home_purchase_timeline_years", 5)
            timeline_months = timeline_years * 12

            monthly_savings = target_down_payment / timeline_months

            # Ensure it's reasonable (not more than 50% of income)
            max_reasonable_savings = monthly_income * 0.5
            return min(monthly_savings, max_reasonable_savings)

        except Exception:
            logger.error(
                "Error calculating monthly home savings",
                exc_info=True,
            )
            return 0.0

    def _calculate_home_purchase_timeline(self, profile: Any) -> int:
        """Calculate home purchase timeline."""
        try:
            return getattr(profile, "home_purchase_timeline_years", 5) * 12

        except Exception:
            logger.error(
                "Error calculating home purchase timeline",
                exc_info=True,
            )
            return 60

    def _calculate_monthly_emergency_savings(self, profile: Any) -> float:
        """Calculate monthly emergency fund savings."""
        try:
            monthly_expenses = getattr(profile, "monthly_expenses", 4000)
            target_months = getattr(profile, "emergency_fund_months", 6)

            # Calculate total target
            target_amount = monthly_expenses * target_months

            # Calculate timeline (default 12 months)
            timeline_months = 12

            monthly_savings = target_amount / timeline_months

            return monthly_savings

        except Exception:
            logger.error(
                "Error calculating monthly emergency savings",
                exc_info=True,
            )
            return 0.0

    def _get_highest_interest_debt(self, debt_info: dict[str, Any]) -> dict[str, Any]:
        """Get highest interest debt."""
        try:
            debts = debt_info.get("debts", [])

            if not debts:
                return {}

            return max(debts, key=lambda d: d.get("interest_rate", 0))

        except Exception:
            logger.error(
                "Error getting highest interest debt",
                exc_info=True,
            )
            return {}

    def _calculate_monthly_debt_payment(self, profile: Any) -> float:
        """Calculate monthly debt payment."""
        try:
            debt_info = getattr(profile, "debt_info", {})
            debts = debt_info.get("debts", [])

            # Calculate minimum payment (1% of balance + interest)
            total_payment = 0.0
            for debt in debts:
                balance = debt.get("balance", 0)
                interest_rate = debt.get("interest_rate", 0)

                minimum_payment = max(balance * 0.01, balance * interest_rate / 12)
                total_payment += minimum_payment

            return total_payment

        except Exception:
            logger.error(
                "Error calculating monthly debt payment",
                exc_info=True,
            )
            return 0.0

    def _calculate_debt_paydown_timeline(self, profile: Any) -> int:
        """Calculate debt paydown timeline."""
        try:
            debt_info = getattr(profile, "debt_info", {})
            total_debt = sum(
                debt.get("balance", 0) for debt in debt_info.get("debts", [])
            )
            monthly_payment = self._calculate_monthly_debt_payment(profile)

            if monthly_payment <= 0:
                return 0

            timeline_months = total_debt / monthly_payment

            return int(timeline_months)

        except Exception:
            logger.error(
                "Error calculating debt paydown timeline",
                exc_info=True,
            )
            return 0

    def _calculate_wealth_building_target(self, profile: Any) -> float:
        """Calculate wealth building target."""
        try:
            monthly_income = getattr(profile, "monthly_income", 10000)

            # Simple target: 10x annual income by retirement
            target_amount = monthly_income * 12 * 10

            return target_amount

        except Exception:
            logger.error(
                "Error calculating wealth building target",
                exc_info=True,
            )
            return 0.0

    def _calculate_monthly_wealth_contribution(self, profile: Any) -> float:
        """Calculate monthly wealth building contribution."""
        try:
            monthly_income = getattr(profile, "monthly_income", 10000)

            # Save 20% of income for wealth building
            return monthly_income * 0.20

        except Exception:
            logger.error(
                "Error calculating monthly wealth contribution",
                exc_info=True,
            )
            return 0.0

    def _calculate_income_generation_target(self, profile: Any) -> float:
        """Calculate income generation target."""
        try:
            monthly_income = getattr(profile, "monthly_income", 10000)

            # Target: replace 50% of current income
            return monthly_income * 0.5

        except Exception:
            logger.error(
                "Error calculating income generation target",
                exc_info=True,
            )
            return 0.0

    def _calculate_initial_income_investment(self, profile: Any) -> float:
        """Calculate initial income investment."""
        try:
            monthly_income = getattr(profile, "monthly_income", 10000)

            # Need at least $50,000 to generate meaningful passive income
            return max(50000, monthly_income * 12 * 2)

        except Exception:
            logger.error(
                "Error calculating initial income investment",
                exc_info=True,
            )
            return 50000

    def __del__(self):
        """Destructor to clean up resources."""
        self.llm_cache.clear()
        self.risk_cache.clear()
        self.pattern_cache.clear()


_fi_singleton: Any = None


def get_financial_intelligence_singleton(
    go_client: Any = None,
) -> FinancialIntelligence:
    """Get the process-wide FinancialIntelligence singleton."""
    global _fi_singleton
    if _fi_singleton is None:
        from miriam_agent.database.memory import get_memory_singleton

        _fi_singleton = FinancialIntelligence(get_memory_singleton(), go_client)
    elif go_client is not None and _fi_singleton.go_client is None:
        _fi_singleton.go_client = go_client
    return _fi_singleton

"""Miriam's proactive financial analyst.

The analyst is the "always watching" half of her proactivity. On demand it
pulls a snapshot of the user's money from the Go backend, thinks about it
like a finance-savvy friend, and decides whether there is ONE thing worth
reaching out about -- if so, a short warm message and a reason.

The analyst never moves money, never stores learnings, and never delivers
anything. Delivery (quiet hours, daily cap, iMessage thread) is the Go
worker's job. The whole module is fail-open: missing data, a dead Go
backend, or an unreachable model all resolve to "stay quiet today".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from miriam_agent.agents.llm import ChatMessage, LLMProvider, get_llm_provider
from miriam_agent.config.settings import get_settings
from miriam_agent.proactive.state import ProactiveStateStore, get_proactive_state

logger = logging.getLogger(__name__)

VALID_PRIORITIES = ("low", "medium", "high")

ANALYST_SYSTEM_PROMPT = """You are Miriam, a warm, finance-savvy best friend
who watches a user's money day and night. You are NOT a customer-service
agent, not a dashboard, not a bank alert system.

Your job: look at the snapshot of the user's financial life below and decide whether
there is ONE genuinely useful thing worth telling them right now. If yes, write the
message you would send them. If nothing is worth it, say no.

Rules:
- Only use numbers that appear in the snapshot. Never estimate, round, or invent values.
  If a number is missing, it does not exist.
- Say no when there is nothing noteworthy. Most snapshots are boring and that is fine.
- One topic per message. A message criticizes/informs/encourages ONE thing, never three.
- Write like a friend texting a friend: plain, warm, a little playful, zero jargon.
  1-3 short sentences. No em dash, no motto, no "Hey there!", no "Great question!",
  no corporate polish, no lectures, no bullet lists.
- End by inviting action only when that adds value: "want me to..." is welcome,
  but only ever ONE question.
- If something is actually urgent for their money (likely overdraft, big bill soon,
  big idle balance, uncovered cash-flow crunch), set priority to "high".
- category must be one of:
  spending, savings, cashflow, investment, bills, income, goal, risk, general.

Respond with ONLY a JSON object like:
{"should_reach_out": true, "priority": "medium", "category": "savings",
 "message": "Your stash is at 2,100 and it has been for three months. Want me to start
 moving 50 a week there?", "reason": "Idle balance has not moved in 90 days"}
or {"should_reach_out": false, "priority": "low", "category": "general",
 "message": "", "reason": "Nothing worth interrupting for"}.
"""


@dataclass
class ProactiveOutcome:
    """The analyst's decision about whether to reach out."""

    should_reach_out: bool = False
    priority: str = "low"
    category: str = "general"
    message: str = ""
    reason: str = ""


def normalize_priority(value: Any) -> str:
    """Coerce a model-provided priority to one of the valid values."""
    v = str(value or "").strip().lower()
    if v in VALID_PRIORITIES:
        return v
    return "low"


def extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort extraction of a JSON object from an LLM response."""
    if not text:
        return None
    text = text.strip()
    # Allow a fenced code block around the JSON.
    unwrapped = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE
    )
    candidates = [unwrapped, text]
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    # Fallback: pull out the first balanced {...} block.
    for candidate in candidates:
        start = candidate.find("{")
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(candidate)):
            if candidate[i] == "{":
                depth += 1
            elif candidate[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(candidate[start : i + 1])
                        if isinstance(data, dict):
                            return data
                    except json.JSONDecodeError:
                        pass
                    break
    return None


def _parse_outcome(text: str) -> ProactiveOutcome:
    data = extract_json(text) or {}
    if not data.get("should_reach_out"):
        return ProactiveOutcome(
            should_reach_out=False,
            reason=str(data.get("reason") or "Nothing worth interrupting for"),
        )
    message = str(data.get("message") or "").strip()
    if not message:
        return ProactiveOutcome(
            should_reach_out=False, reason="Model returned no message"
        )
    return ProactiveOutcome(
        should_reach_out=True,
        priority=normalize_priority(data.get("priority")),
        category=str(data.get("category") or "general").strip().lower(),
        message=message,
        reason=str(data.get("reason") or "").strip(),
    )


def _fmt_snapshot_value(value: Any) -> str:
    """Render an unknown Go payload shape in a compact, safe way."""
    try:
        return json.dumps(value, default=str, ensure_ascii=True)[:1200]
    except Exception:
        return str(value)[:1200]


async def _go_snapshot_lines(
    token: str,
    client: Any,
    *,
    period: str = "last_90_days",
    financial_plan: dict[str, Any] | None = None,
) -> list[str]:
    """Fetch live money data from Go (fail-open; any miss just narrows the view).

    Sources are the real financial-intelligence outputs: the period-aware
    health score and the full plan (health + cash-flow forecast + next steps)
    computed by the Python engine from the ledger-backed snapshot, plus raw
    balances/transactions/bills for detail.
    """
    lines: list[str] = []
    fetchers: list[tuple[str, Any]] = [
        ("Balances", lambda: client.get_balances(token)),
        ("Recent transactions", lambda: client.get_transactions(token, limit=15)),
        (
            "Spending summary",
            lambda: client.get_spending_summary(token, period="month"),
        ),
        ("Upcoming bills", lambda: client.get_upcoming_bills(token)),
        (
            "Financial health",
            lambda: client.get_financial_health(token, period=period),
        ),
    ]
    for label, fetch in fetchers:
        try:
            value = await fetch()
            lines.append(f"{label}: " + _fmt_snapshot_value(value))
        except Exception as e:
            logger.info("Proactive snapshot: %s unavailable (%s)", label, e)
    # The financial plan is the engine's own synthesis; use the caller's copy
    # when one is already loaded (avoids a duplicate Go call).
    if financial_plan is None:
        try:
            financial_plan = await client.get_financial_plan(token)
        except Exception as e:
            logger.info("Proactive snapshot: financial plan unavailable (%s)", e)
    if financial_plan:
        lines.append("Financial plan: " + _fmt_snapshot_value(financial_plan))
    try:
        profile = await client.get_user_profile(token)
        lines.append(
            "Profile: " + _fmt_snapshot_value(profile.get("profile") or profile)
        )
    except Exception as e:
        logger.info("Proactive snapshot: profile unavailable (%s)", e)
    return lines


async def analyze_finances(
    *,
    user_id: str,
    token: str,
    period: str = "last_90_days",
    financial_plan: dict[str, Any] | None = None,
    memory_facts: list[dict[str, Any]] | None = None,
    provider: LLMProvider | None = None,
    state: ProactiveStateStore | None = None,
) -> ProactiveOutcome:
    """Decide whether to reach out to a user, and with what message.

    Fetches a live money snapshot from the Go backend (fail-open), thought
    about through the real financial-intelligence outputs (period-aware
    health + plan), and returned as a ProactiveOutcome. The caller (Go's
    reacher worker) owns quiet hours, the daily cap, and the actual iMessage
    delivery.
    """
    outcome = ProactiveOutcome()
    prov = provider or get_llm_provider()

    # 1. Gather the snapshot. Fail-open: any missing piece just narrows what
    #    she can truthfully see.
    snapshot_lines: list[str] = []
    try:
        from miriam_agent.integrations.go_client import get_go_client

        snapshot_lines = await _go_snapshot_lines(
            token, get_go_client(), period=period, financial_plan=financial_plan
        )
    except Exception as e:
        logger.warning("Proactive snapshot fetch failed entirely: %s", e)

    if memory_facts:
        facts = [
            f"- [{f.get('type', 'fact')}] {f.get('content', '')}"
            for f in memory_facts[:6]
        ]
        snapshot_lines.append("What she knows about this user:\n" + "\n".join(facts))

    snapshot = "\n".join(snapshot_lines)
    if not snapshot.strip():
        return outcome  # No data at all: stay quiet.

    settings = get_settings()
    user_block = (
        "USER CONTEXT\n"
        f"user_id: {user_id}\n\n"
        "FINANCIAL SNAPSHOT (the ONLY numbers that exist right now):\n"
        + snapshot[:6000]
    )

    messages = [
        ChatMessage(role="system", content=ANALYST_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_block),
    ]

    try:
        response = await prov.complete(
            messages=messages,
            tools=None,
            temperature=settings.PROACTIVE_TEMPERATURE,
            max_tokens=settings.PROACTIVE_MAX_TOKENS,
        )
    except Exception as e:
        logger.warning("Proactive analyst LLM call failed: %s", e)
        return outcome

    outcome = _parse_outcome(response.content or "")
    if not outcome.should_reach_out:
        return outcome

    store = state or get_proactive_state()
    if await store.should_stay_quiet(user_id, outcome.priority, outcome.message):
        return ProactiveOutcome(
            should_reach_out=False,
            reason="Same or too-recent a message already sent",
        )
    await store.mark(user_id, outcome.priority, outcome.message)
    return outcome

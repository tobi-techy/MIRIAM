"""Proactive analysis endpoint for Miriam Financial Agent.

``POST /api/v1/proactive/analyze`` lets the Go reacher worker (RAIL_BACKEND)
ask Miriam whether there is something worth telling the user right now. It
returns a decision + message; Go owns quiet hours, the daily cap, and the
actual iMessage delivery. Fail-open: no data, dead Go backend, disabled
feature, or an unreachable model all resolve to "stay quiet".
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends

from miriam_agent.api.chat import _load_financial_plan, _load_memory_facts
from miriam_agent.api.dependencies import (
    get_bearer_token,
    get_current_user,
    get_memory_store,
    get_supermemory_memory_dep,
)
from miriam_agent.config.settings import get_settings
from miriam_agent.database.memory import MemoryStore
from miriam_agent.database.models import User
from miriam_agent.proactive.analyst import analyze_finances
from miriam_agent.proactive.state import get_proactive_state

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/proactive/analyze")
async def proactive_analyze(
    user: User = Depends(get_current_user),
    token: str = Depends(get_bearer_token),
    memory_store: MemoryStore = Depends(get_memory_store),
    supermemory_memory: Any = Depends(get_supermemory_memory_dep),
) -> dict[str, Any]:
    """Ask whether there is a proactive message worth sending this user."""
    settings = get_settings()
    if not settings.PROACTIVE_ENABLED:
        return {
            "should_reach_out": False,
            "priority": "low",
            "category": "general",
            "message": "",
            "reason": "proactive analyst disabled",
        }

    await memory_store.ensure_user(user)

    # Cooldown: after a nudge the store stays quiet for PROACTIVE_MIN_INTERVAL_HOURS
    # (dedupe also silences an identical message for 24h). Checked before any
    # LLM work so the 30-minute Go reacher tick burns no tokens during cooldown.
    # A "high" priority on the latest nudge bypasses the interval; a genuinely
    # high situation on this tick can still be re-checked next pass.
    store = get_proactive_state()
    if await store.should_stay_quiet(user.id, "low", ""):
        return {
            "should_reach_out": False,
            "priority": "low",
            "category": "general",
            "message": "",
            "reason": "already reached out recently (cooldown)",
        }

    memory_facts = await _load_memory_facts(
        memory_store,
        user.id,
        query="What should Miriam notice about this user's money right now?",
        supermemory_memory=supermemory_memory,
    )
    financial_plan = await _load_financial_plan(token)

    outcome = await analyze_finances(
        user_id=user.id,
        token=token,
        financial_plan=financial_plan,
        memory_facts=memory_facts,
    )

    if outcome.should_reach_out:
        await store.mark(user.id, outcome.priority, outcome.message)

    return {
        "should_reach_out": outcome.should_reach_out,
        "priority": outcome.priority,
        "category": outcome.category,
        "message": outcome.message,
        "reason": outcome.reason,
    }

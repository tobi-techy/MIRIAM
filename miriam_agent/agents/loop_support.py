"""Support helpers for the agent loop.

Extracted from ``miriam_agent.agents.agent_loop`` as a pure move for the
blob-ratchet split: message building, grounding-corpus assembly, reply-problem
detection, the reply guard, read-only execution, and tool-call records.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from miriam_agent.agents.llm import ChatMessage
from miriam_agent.agents.system_prompt import build_system_prompt
from miriam_agent.judgment.gates import EGRESS_REGENERATE_INSTRUCTION
from miriam_agent.observability.correlation import current_trace_id
from miriam_agent.observability.metrics import record_reply_guard
from miriam_agent.safety import grounding
from miriam_agent.safety.money_tools import MONEY_TOOL_NAMES

if TYPE_CHECKING:
    from miriam_agent.agents.agent_loop import Agent

logger = logging.getLogger(__name__)


def _rule_of(problem: str) -> str:
    """The rule name from a prefixed problem line, for the runtime counters."""
    return problem.split(":", 1)[0].strip() or "unknown"


def _without_stale(block: Any, *, label: str) -> Any:
    """Drop any part of a context block that declares itself too old to use.

    Applied before the prompt is built, so stale data never reaches the model
    at all, and the guard's corpus cannot disagree with what the model read.
    """
    if isinstance(block, list):
        kept = [item for item in block if not grounding.is_stale_block(item)]
        if len(kept) != len(block):
            logger.info("stale source dropped from %s", label)
        return kept
    if grounding.is_stale_block(block):
        logger.info("stale source dropped from %s", label)
        return None
    return block


# The refusal a model gets when it proposes a tool it cannot have. It is a tool
# result rather than an error so the model can answer the user normally.
_MONEY_TOOL_REFUSAL = (
    "'{name}' moves money and is not available here. Money movements go "
    "through the ledger, not through a tool call. Nothing ran."
)


def _tool_call_record(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """One executed tool call, tagged with the request's trace id.

    The record is what a caller (and the audit trail) uses to reconstruct
    "what did this message actually do", so the id travels with it.
    """
    try:
        from miriam_agent.agents.tools import idempotency_key

        idem = idempotency_key(name, args or {}, "")
    except Exception:
        idem = ""
    return {
        "name": name,
        "arguments": args,
        "trace_id": current_trace_id(),
        "idempotency_key": idem,
    }


def _build_messages(
    message: str,
    history: list[dict[str, Any]] | None,
    user_context: dict[str, Any] | None,
    memory_facts: list[dict[str, Any]] | None,
    financial_plan: dict[str, Any] | None = None,
) -> list[ChatMessage]:
    user_context = _without_stale(user_context, label="user_context")
    memory_facts = _without_stale(memory_facts, label="memory_facts")
    financial_plan = _without_stale(financial_plan, label="financial_plan")
    system_prompt = build_system_prompt(
        user_context=user_context,
        memory_facts=memory_facts,
        financial_plan=financial_plan,
    )
    messages: list[ChatMessage] = [ChatMessage(role="system", content=system_prompt)]
    for h in history or []:
        role = h.get("role")
        content = h.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append(ChatMessage(role=role, content=content))
    messages.append(ChatMessage(role="user", content=message))
    return messages


def _unknown_tool_result(name: str) -> dict[str, Any]:
    """What a model gets when it asks for a tool that is not available.

    A money tool names itself, so the refusal can say what it was and why,
    rather than the generic "unknown tool" a model might try to work around.
    """
    if name in MONEY_TOOL_NAMES:
        return {"error": _MONEY_TOOL_REFUSAL.format(name=name), "_blocked": True}


def _grounding_parts(block: Any, *, label: str) -> list[str]:
    """JSON for one source block, dropping any part that declares itself stale.

    A stale source cannot ground a figure. That is the difference between
    "I was told this" and "I was told this, and it was still true" -- the
    staler class of wrong figure is confidently repeated old data, which no
    amount of source-checking catches on its own.
    """
    if not block:
        return []
    items = block if isinstance(block, list) else [block]
    parts: list[str] = []
    for item in items:
        stale = grounding.is_stale_block(item) or (
            isinstance(item, dict) and grounding.is_stale_block(item.get("result"))
        )
        if stale:
            logger.info("stale source excluded from grounding: %s", label)
            continue
        parts.append(json.dumps(item, default=str))
    return parts


def _grounding_corpus(
    *,
    message: str,
    history: list[dict[str, Any]] | None,
    user_context: dict[str, Any] | None,
    memory_facts: list[dict[str, Any]] | None,
    financial_plan: dict[str, Any] | None,
    tool_results: list[dict[str, Any]] | None,
) -> str:
    """Everything this turn was actually given, as text a figure can be traced to.

    Only the user's own turns are read back out of history. Miriam's earlier
    replies are deliberately left out: a number she invented last turn is not
    a source for the number she states this turn, and including them would let
    one hallucination ground its own successor.
    """
    parts: list[str] = [message or ""]
    parts.extend(
        str(turn.get("content") or turn.get("text") or "")
        for turn in (history or [])
        if isinstance(turn, dict) and turn.get("role") == "user"
    )
    # The blocks go in as their own data, not as the prompt's rendering of
    # them. Two reasons: the checks that need structure (which label a
    # figure wears, whether two sources disagree) cannot read prose, and the
    # data is a superset of the prompt -- so this can only ever accept a
    # figure the turn genuinely holds, which is the direction that matters.
    # Stale blocks are excluded here *and* from the prompt itself.
    for block, label in (
        (user_context, "user_context"),
        (memory_facts, "memory_facts"),
        (financial_plan, "financial_plan"),
    ):
        parts.extend(_grounding_parts(block, label=label))
    for result in tool_results or []:
        parts.extend(_grounding_parts(result, label="tool_result"))
    return "\n".join(part for part in parts if part)


def _reply_problems(reply: str, grounded: str) -> list[str]:
    """Everything wrong with a draft, in one list.

    One function so the rules cannot drift between the paths that apply
    them. Every entry is prefixed with the rule that produced it, so the
    runtime counters and the log lines agree with the eval set about what
    fired.

    The action rule is the reason this loop is special: it never executes
    anything, so a completed-action claim here is not merely unverified, it
    is false. The rest are claims about the user's money that nothing in
    the turn supports -- a prediction, a change, a reading of the account,
    or a merchant who was never mentioned.
    """
    problems = [
        f"figure: no source for {figure}"
        for figure in grounding.ungrounded_figures(reply, grounded)
    ]
    problems.extend(
        f"label: {figure} is labelled against its source"
        for figure in grounding.label_conflicts(reply, grounded)
    )
    problems.extend(
        f"contested: {figure} comes from sources that disagree"
        for figure in grounding.contested_figures(reply, grounded)
    )
    problems.extend(
        f"action: claims an action that did not happen: {phrase!r}"
        for phrase in grounding.action_claims(reply)
    )
    problems.extend(
        f"forecast: predicts {phrase!r} with no projection to point to"
        for phrase in grounding.forecast_claims(reply, grounded)
    )
    problems.extend(
        f"change: asserts {phrase!r} with nothing to compare against"
        for phrase in grounding.change_claims(reply, grounded)
    )
    problems.extend(
        f"observation: claims to have read the account: {phrase!r}"
        for phrase in grounding.observation_claims(reply, grounded)
    )
    problems.extend(
        f"entity: {name!r} appears nowhere in the turn"
        for name in grounding.novel_entities(reply, grounded)
    )
    return problems


async def _guard_reply(
    agent: Agent,
    *,
    draft: str,
    grounded: str,
    llm_messages: list[ChatMessage],
) -> str | None:
    """Refuse to send a claim the turn does not support.

    Returns the text to send -- the draft when it is clean, a rewrite when
    the first attempt only drifted -- or ``None`` when nothing safe can be
    said, which the caller turns into the fixed "I don't have that reliably
    yet" line. One retry at most: a model that invents a figure twice will
    not stop on the third ask, and the user is owed a straight answer
    instead.
    """
    problems = _reply_problems(draft, grounded)
    if not problems:
        record_reply_guard("none", "allowed")
        return draft
    for problem in problems:
        record_reply_guard(_rule_of(problem), "blocked")
    logger.warning(
        "draft rejected before send: %s (trace_id=%s)",
        "; ".join(problems[:5]),
        current_trace_id(),
        extra={"problems": problems[:5]},
    )
    try:
        corrected = await agent.provider.complete(
            messages=llm_messages
            + [ChatMessage(role="user", content=EGRESS_REGENERATE_INSTRUCTION)],
            tools=None,
            temperature=agent.config.temperature,
            max_tokens=agent.config.max_tokens,
        )
        regenerated = corrected.content or ""
    except Exception:
        logger.warning("reply regeneration failed", exc_info=True)
        for problem in problems:
            record_reply_guard(_rule_of(problem), "fell_back")
        return None
    remaining = _reply_problems(regenerated, grounded) if regenerated else []
    if not regenerated or remaining:
        for problem in remaining or problems:
            record_reply_guard(_rule_of(problem), "fell_back")
        return None
    record_reply_guard("none", "retried")
    return regenerated


async def _safe_execute(
    agent: Agent,
    name: str,
    args: dict[str, Any],
    ctx: dict[str, Any],
    user_id: str,
    user_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Enforce RBAC and safety policy, then execute. Reads only.

    The mutation refusal is the load-bearing line in this file. It is not a
    policy check that some future flag could relax: a tool that changes money
    state is returned as blocked, whatever the caller says, because this loop
    has no way to authorise one.
    """
    from miriam_agent.auth.rbac import require_tool_access

    tool = agent.registry.get(name)
    if tool is None:
        return _unknown_tool_result(name)
    if tool.is_mutation or tool.requires_approval or name in MONEY_TOOL_NAMES:
        logger.error(
            "refusing a money tool on the answer path",
            extra={"tool": name, "user_id": user_id},
        )
        return {
            "error": _MONEY_TOOL_REFUSAL.format(name=name),
            "_blocked": True,
        }

    roles = set((user_context or {}).get("roles") or ["guest"])
    require_tool_access(roles, name)
    allowed = await agent.safety_policy.validate_action(
        tool_name=name,
        arguments=args,
        user_id=user_id,
        financial_profile=user_context,
    )
    if not allowed:
        logger.info(
            "Tool denied by safety policy",
            extra={"tool": name, "user_id": user_id},
        )
        return {
            "error": f"'{name}' was blocked by safety checks. Nothing ran.",
            "_blocked": True,
        }

    return await agent.registry.execute(name, args, context=ctx)


def _tool_message(
    call_id: str | None, tool_name: str, result: dict[str, Any]
) -> ChatMessage:
    """Build a tool-result ChatMessage (content truncated for context budget)."""
    return ChatMessage(
        role="tool",
        tool_call_id=call_id or tool_name or "tool",
        name=tool_name or "tool",
        content=json.dumps(result, default=str)[:2000],
    )

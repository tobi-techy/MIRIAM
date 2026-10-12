"""Agent loop for Miriam Financial Agent.

The core orchestration engine, for answerable turns only:

    1. Build system prompt (personality + user context + memory)
    2. Send conversation + read-only tools to the LLM
    3. LLM decides: answer directly, or propose a read
    4. Reads run. Nothing else can: see the invariant below.
    5. Feed tool results back to the LLM for the final answer

**This loop cannot move money, and that is structural.**

Three independent things would have to fail for it to reach a rail:

* the registry it reads from has no money tool in it
  (``tools/definitions.build_tool_registry`` removes them, and
  ``ToolRegistry.llm_schemas`` only ever offers read-only tools),
* the guard in ``agents/loop_support._safe_execute`` refuses a mutation outright
  rather than staging it, so there is no approval path to talk it into running
  one,
* the money tools themselves are only reachable through
  ``miriam_agent.hands``, which this module does not import.

Money turns never arrive here at all. ``api/chat.py`` classifies the turn first
and sends anything that touches money to ``orchestrator.py``, whose only path is
Hands -> Judgment -> Hands -> Voice.

The earlier version of this file staged money actions, held an approval ledger
and replayed Go confirmation tokens. All of that is gone: there were two writers
of a balance, and this was the wrong one.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from miriam_agent.agents.base import AgentConfig
from miriam_agent.agents.llm import ChatMessage, LLMProvider, get_llm_provider
from miriam_agent.agents.loop_support import (
    _build_messages,
    _grounding_corpus,
    _guard_reply,
    _reply_problems,
    _safe_execute,
    _tool_call_record,
    _tool_message,
    _unknown_tool_result,
)
from miriam_agent.agents.tools import ToolRegistry
from miriam_agent.integrations.go_client import GoBackendClient
from miriam_agent.judgment.client import enabled as typesafe_enabled
from miriam_agent.judgment.gates import (
    EGRESS_DONT_KNOW,
    EGRESS_REGENERATE_INSTRUCTION,
    EgressBranch,
    EgressDecision,
    ToolBranch,
    ToolDecision,
    egress_gate,
    tool_gate,
)
from miriam_agent.judgment.schemas import ProposedTool
from miriam_agent.judgment.state import build_state
from miriam_agent.observability.correlation import current_trace_id
from miriam_agent.observability.metrics import record_reply_guard
from miriam_agent.safety.policy import SafetyPolicy
from miriam_agent.utils.text import (
    bubble_sets,
    clean_text,
    lift_reaction,
)

logger = logging.getLogger(__name__)


MAX_TOOL_ROUNDS = 5


def _budgets() -> tuple[int, int, float, float]:
    """Per-task budgets from settings with safe defaults."""
    try:
        from miriam_agent.config.settings import get_settings

        s = get_settings()
        return (
            int(getattr(s, "AGENT_MAX_TOOL_ROUNDS", MAX_TOOL_ROUNDS)),
            int(getattr(s, "AGENT_MAX_TOKENS_PER_TURN", 12000)),
            float(getattr(s, "AGENT_MAX_COST_USD_PER_TURN", 0.05)),
            float(getattr(s, "AGENT_WALL_CLOCK_S", 30.0)),
        )
    except Exception:
        return (MAX_TOOL_ROUNDS, 12000, 0.05, 30.0)


def _abort_result(conv_id: str, budget: str) -> "AgentRunResult":
    try:
        from miriam_agent.observability.metrics import AGENT_BUDGET_EXCEEDED

        AGENT_BUDGET_EXCEEDED.labels(budget=budget).inc()
    except Exception:
        pass
    logger.warning("agent budget exceeded", extra={"budget": budget})
    # Imported lazily to avoid forward-ref at module load; AgentRunResult is
    # defined below in this same file.
    from miriam_agent.agents.agent_loop import AgentRunResult as _R

    return _R(
        response="I've gathered what I can for now. Ask me to continue and I will.",
        conversation_id=conv_id,
        tool_calls=[],
    )


@dataclass
class AgentRunResult:
    response: str
    conversation_id: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Chatty-turn affordances the Go executor relays as native gestures: up to
    # two short wrapper bubbles behind the main reply, and one tapback reaction
    # on the user's message. Empty when the reply stayed one message.
    messages: list[str] = field(default_factory=list)
    reaction: str = ""
    # Correlation id for the request this result belongs to, defaulted from the
    # bound context so every construction site carries it without plumbing.
    trace_id: str = field(default_factory=current_trace_id)


class Agent:
    """Stateless agent loop for answerable turns. One instance per request."""

    def __init__(
        self,
        registry: ToolRegistry,
        provider: LLMProvider | None = None,
        safety_policy: SafetyPolicy | None = None,
        go_client: GoBackendClient | None = None,
        config: AgentConfig | None = None,
    ):
        self.registry = registry
        self.provider = provider or get_llm_provider()
        self.safety_policy = safety_policy or SafetyPolicy()
        self.go_client = go_client
        self.config = config or AgentConfig(name="financial_agent")

    # Kept as a class attribute because eval/hallucination_cases.py runs the
    # production rule set through ``Agent._reply_problems``; the implementation
    # moved to agents/loop_support.py with the rest of the guard helpers.
    @staticmethod
    def _reply_problems(reply: str, grounded: str) -> list[str]:
        return _reply_problems(reply, grounded)

    def _decorate(self, result: AgentRunResult) -> AgentRunResult:
        """Project the chatty-turn affordances onto a finished result: split a
        wall-of-text reply into short bubbles and lift any whitelisted tapback.
        Every user-facing response is scrubbed first so no em/en dash the model
        emitted ever reaches the user."""
        clean = clean_text(result.response or "")
        main, extras = bubble_sets(clean)
        result.response = main
        result.messages = [clean_text(m) for m in extras]
        result.reaction = lift_reaction(clean)
        return result

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def run(
        self,
        *,
        user_id: str,
        token: str,
        message: str,
        conversation_id: str | None = None,
        history: list[dict[str, Any]] | None = None,
        user_context: dict[str, Any] | None = None,
        memory_facts: list[dict[str, Any]] | None = None,
        financial_plan: dict[str, Any] | None = None,
    ) -> AgentRunResult:
        """Run one answerable turn. Returns the reply and the reads it made."""
        conv_id = conversation_id or f"conv_{user_id}"
        ctx = {
            "user_id": user_id,
            "token": token,
        }
        messages = _build_messages(
            message=message,
            history=history,
            user_context=user_context,
            memory_facts=memory_facts,
            financial_plan=financial_plan,
        )

        tool_calls_made: list[dict[str, Any]] = []
        # Untruncated results, kept for the two checks that need the whole
        # evidence: the deterministic figure guard and the TypeSafe judge. The
        # model's own copy is trimmed in ``_tool_message``; the checks that
        # decide whether to trust the model must not be.
        raw_tool_results: list[dict[str, Any]] = []
        # Accumulated assistant-tool_calls + tool-result messages sent to the
        # provider so multi-round tool use is a clean call/result pairing
        # (OpenAI and Concentrate both reject tool results without the
        # preceding assistant tool_calls message).
        llm_extra: list[ChatMessage] = []
        max_rounds, max_tokens, max_cost, wall_s = _budgets()
        started = time.monotonic()
        used_tokens = 0
        used_cost = 0.0

        for _round in range(max_rounds):
            if time.monotonic() - started > wall_s:
                return self._decorate(_abort_result(conv_id, "wall_clock"))
            llm_messages = list(messages) + llm_extra

            schemas = self.registry.llm_schemas()
            remaining = max(0.0, wall_s - (time.monotonic() - started))
            # Wall-clock cap via wait_for: a stuck provider cannot pass the budget.
            try:
                response = await asyncio.wait_for(
                    self.provider.complete(
                        messages=llm_messages,
                        tools=schemas,
                        temperature=self.config.temperature,
                        max_tokens=self.config.max_tokens,
                    ),
                    timeout=remaining if remaining > 0 else None,
                )
            except TimeoutError:
                return self._decorate(_abort_result(conv_id, "wall_clock"))
            try:
                usage = response.usage or {}
                used_tokens += int(usage.get("total_tokens", 0))
                try:
                    from miriam_agent.agents.llm import LLMUsage

                    used_cost += self.provider.cost_estimate(
                        LLMUsage(
                            prompt_tokens=int(usage.get("prompt_tokens", 0)),
                            completion_tokens=int(usage.get("completion_tokens", 0)),
                            total_tokens=int(usage.get("total_tokens", 0)),
                        )
                    )
                except Exception:
                    pass
            except Exception:
                pass
            if used_tokens > max_tokens:
                return self._decorate(_abort_result(conv_id, "tokens"))
            if used_cost > max_cost:
                return self._decorate(_abort_result(conv_id, "cost"))

            # No tools wanted -> final answer, gated before send.
            if not response.tool_calls:
                reply = await self._apply_egress(
                    user_id=user_id,
                    message=message,
                    history=history,
                    user_context=user_context,
                    memory_facts=memory_facts,
                    financial_plan=financial_plan,
                    draft=response.content,
                    llm_messages=llm_messages,
                    tool_results=raw_tool_results,
                    grounded=_grounding_corpus(
                        message=message,
                        history=history,
                        user_context=user_context,
                        memory_facts=memory_facts,
                        financial_plan=financial_plan,
                        tool_results=raw_tool_results,
                    ),
                )
                return self._decorate(
                    AgentRunResult(
                        response=reply,
                        conversation_id=conv_id,
                        tool_calls=tool_calls_made,
                    )
                )

            # Record the assistant's tool_calls message so the next round is a
            # clean call -> result pairing (OpenAI and Concentrate both reject
            # tool results without the preceding assistant tool_calls message).
            llm_extra.append(
                ChatMessage(
                    role="assistant",
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                )
            )

            for call in response.tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                try:
                    args = (
                        json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    )
                except json.JSONDecodeError:
                    args = {}

                tool = self.registry.get(name)
                if tool is None:
                    llm_extra.append(
                        _tool_message(call.get("id"), name, _unknown_tool_result(name))
                    )
                    tool_calls_made.append(_tool_call_record(name, args))
                    continue

                # TypeSafe tool gate: reject irrelevant/bad-arg calls before
                # anything runs. A tool the gate wants confirmed or blocked is
                # not run either: with no money path left in this loop, "confirm"
                # can only mean "do not do this silently", and the answer is no.
                gate = await self._tool_gate_decision(
                    user_id=user_id,
                    message=message,
                    history=history,
                    user_context=user_context,
                    memory_facts=memory_facts,
                    financial_plan=financial_plan,
                    name=name,
                    args=args,
                )
                if gate.branch is not ToolBranch.ALLOW:
                    llm_extra.append(
                        _tool_message(
                            call.get("id"),
                            name,
                            {
                                "error": (
                                    f"tool call not run ({gate.branch.value}): "
                                    f"{gate.reason or 'not permitted here'}"
                                )
                            },
                        )
                    )
                    tool_calls_made.append(_tool_call_record(name, args))
                    continue

                tool_calls_made.append(_tool_call_record(name, args))
                try:
                    result = await _safe_execute(
                        self, name, args, ctx, user_id, user_context
                    )
                    llm_extra.append(_tool_message(call.get("id"), name, result))
                    raw_tool_results.append({"name": name, "result": result})
                except Exception as e:
                    llm_extra.append(
                        _tool_message(call.get("id"), name, {"error": str(e)})
                    )
                    raw_tool_results.append({"name": name, "result": {"error": str(e)}})

        # Tool loop exhausted without a final answer.
        return self._decorate(
            AgentRunResult(
                response=(
                    "I've gathered what you need. Ask me to go further and I will."
                ),
                conversation_id=conv_id,
                tool_calls=tool_calls_made,
            )
        )

    async def stream_run(
        self,
        *,
        user_id: str,
        token: str,
        message: str,
        conversation_id: str | None = None,
        history: list[dict[str, Any]] | None = None,
        user_context: dict[str, Any] | None = None,
        memory_facts: list[dict[str, Any]] | None = None,
        financial_plan: dict[str, Any] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Streaming variant. Yields events: token / tool_result / done / error.

        Money turns do not reach here; ``api/chat.py`` routes them to the
        orchestrator. A model that still asks for a money tool is refused, and
        the refusal is streamed as a tool result so it can answer the user.

        Tokens stream live for responsiveness, but the final text is always
        egress-gated before ``done``: when the gate rewrites the reply, an
        ``egress_correction`` event carries the text to send and ``done``
        carries the same gated text, so no ungrounded prose leaves this path.
        """

        messages = _build_messages(
            message=message,
            history=history,
            user_context=user_context,
            memory_facts=memory_facts,
            financial_plan=financial_plan,
        )
        ctx = {"user_id": user_id, "token": token}
        schemas = self.registry.llm_schemas()
        llm_extra: list[ChatMessage] = []
        # Untruncated results, for the same two reasons as ``run``: the figure
        # guard and the judge both need the whole evidence.
        raw_tool_results: list[dict[str, Any]] = []

        async def _gate(draft: str, llm_messages: list[ChatMessage]) -> str:
            """The full egress gate -- deterministic figure guard, then the
            judge -- applied to a finished draft."""
            return await self._apply_egress(
                user_id=user_id,
                message=message,
                history=history,
                user_context=user_context,
                memory_facts=memory_facts,
                financial_plan=financial_plan,
                draft=draft,
                llm_messages=llm_messages,
                tool_results=raw_tool_results,
                grounded=_grounding_corpus(
                    message=message,
                    history=history,
                    user_context=user_context,
                    memory_facts=memory_facts,
                    financial_plan=financial_plan,
                    tool_results=raw_tool_results,
                ),
            )

        for _round in range(MAX_TOOL_ROUNDS):
            llm_messages = list(messages) + llm_extra

            collected: list[str] = []
            stream_tool_calls: list[dict[str, Any]] = []
            async for event in self.provider.stream(
                messages=llm_messages,
                tools=schemas,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            ):
                if event["type"] == "token":
                    collected.append(event["content"])
                    yield {"type": "token", "content": event["content"]}
                elif event["type"] == "tool_call":
                    tc = event["tool_call"]
                    yield {"type": "tool_call", "tool_call": tc}
                    stream_tool_calls.append(tc)

            if not stream_tool_calls:
                draft = "".join(collected)
                gated = await _gate(draft, llm_messages)
                if gated != draft:
                    yield {"type": "egress_correction", "content": gated}
                yield {"type": "done", "content": gated}
                return

            # Record the assistant's tool_calls so the next round is a clean
            # call -> result pairing (required by OpenAI and Concentrate).
            llm_extra.append(
                ChatMessage(
                    role="assistant",
                    content="".join(collected),
                    tool_calls=stream_tool_calls,
                )
            )

            for call in stream_tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                try:
                    args = (
                        json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    )
                except json.JSONDecodeError:
                    args = {}
                tool = self.registry.get(name)
                if tool is None:
                    result = _unknown_tool_result(name)
                    llm_extra.append(_tool_message(call.get("id"), name, result))
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "error",
                        "result": result,
                    }
                    continue

                gate = await self._tool_gate_decision(
                    user_id=user_id,
                    message=message,
                    history=history,
                    user_context=user_context,
                    memory_facts=memory_facts,
                    financial_plan=financial_plan,
                    name=name,
                    args=args,
                )
                if gate.branch is not ToolBranch.ALLOW:
                    result = {
                        "error": (
                            f"tool call not run ({gate.branch.value}): "
                            f"{gate.reason or 'not permitted here'}"
                        )
                    }
                    llm_extra.append(_tool_message(call.get("id"), name, result))
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "error",
                        "result": result,
                    }
                    continue

                try:
                    result = await _safe_execute(
                        self, name, args, ctx, user_id, user_context
                    )
                    llm_extra.append(_tool_message(call.get("id"), name, result))
                    raw_tool_results.append({"name": name, "result": result})
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "success",
                        "result": result,
                    }
                except Exception as e:
                    llm_extra.append(
                        _tool_message(call.get("id"), name, {"error": str(e)})
                    )
                    raw_tool_results.append({"name": name, "result": {"error": str(e)}})
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "error",
                        "result": {"error": str(e)},
                    }

        fallback = "I've gathered what you need. Ask me to go further and I will."
        gated = await _gate(fallback, list(messages) + llm_extra)
        if gated != fallback:
            yield {"type": "egress_correction", "content": gated}
        yield {"type": "error", "message": "Too many tool rounds; stopping safely."}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _tool_gate_decision(
        self,
        *,
        user_id: str,
        message: str,
        history: list[dict[str, Any]] | None,
        user_context: dict[str, Any] | None,
        name: str,
        args: dict[str, Any],
        memory_facts: list[dict[str, Any]] | None = None,
        financial_plan: dict[str, Any] | None = None,
    ) -> ToolDecision:
        """Judge one proposed tool call before anything runs."""
        if not typesafe_enabled():
            # Fail-closed for writes, fail-open for reads: degraded ALLOW used
            # to permit everything. A mutation must never run unchecked.
            tool = self.registry.get(name)
            if tool is not None and (tool.is_mutation or tool.requires_approval):
                return ToolDecision(
                    branch=ToolBranch.BLOCK,
                    reason="mutating tool blocked while judgment layer degraded",
                    degraded=True,
                )
            return ToolDecision(branch=ToolBranch.ALLOW, degraded=True)
        tool = self.registry.get(name)
        args_schema = getattr(tool, "args_schema", {}) if tool is not None else {}
        return await tool_gate(
            build_state(
                user_id=user_id,
                message=message,
                history=history,
                user_context=user_context,
                registry=self.registry,
                proposed_tool=ProposedTool(
                    name=name,
                    args=args,
                    args_schema=args_schema,
                ),
                memory_facts=memory_facts,
                financial_plan=financial_plan,
            )
        )

    async def _apply_egress(
        self,
        *,
        user_id: str,
        message: str,
        history: list[dict[str, Any]] | None,
        user_context: dict[str, Any] | None,
        draft: str,
        llm_messages: list[ChatMessage],
        tool_results: list[dict[str, Any]],
        grounded: str = "",
        memory_facts: list[dict[str, Any]] | None = None,
        financial_plan: dict[str, Any] | None = None,
    ) -> str:
        """Gate a draft, then return the reply to send.

        The deterministic figure guard runs first and unconditionally: it is
        the one check that must not depend on the judge being reachable, and
        the only one that still runs when the judge is switched off. The
        TypeSafe egress judge then owns policy, secrets, and tone, and keeps
        its fail-open behaviour -- by that point the figures are already safe.
        """
        guarded = await _guard_reply(
            self,
            draft=draft,
            grounded=grounded,
            llm_messages=llm_messages,
        )
        if guarded is None:
            return EGRESS_DONT_KNOW
        draft = guarded

        if not typesafe_enabled():
            if self.config.fail_closed_without_judge:
                logger.error(
                    "judge is off and fail-closed is configured: refusing the reply"
                )
                record_reply_guard("judge", "fell_back")
                return EGRESS_DONT_KNOW
            return draft

        async def _evaluate(text: str) -> EgressDecision:
            return await egress_gate(
                build_state(
                    user_id=user_id,
                    message=message,
                    history=history,
                    user_context=user_context,
                    registry=self.registry,
                    draft_reply=text,
                    tool_results=tool_results,
                    memory_facts=memory_facts,
                    financial_plan=financial_plan,
                )
            )

        decision = await _evaluate(draft)
        if decision.degraded and self.config.fail_closed_without_judge:
            logger.error(
                "judge unavailable and fail-closed is configured: refusing the reply"
            )
            record_reply_guard("judge", "fell_back")
            return EGRESS_DONT_KNOW
        if decision.branch is EgressBranch.DISCARD:
            return decision.reply or EGRESS_DONT_KNOW
        if decision.branch is EgressBranch.REGENERATE:
            instruction = EGRESS_REGENERATE_INSTRUCTION
            if decision.tone_note == "cold":
                instruction += " Keep the tone warm and human, not robotic."
            elif decision.tone_note == "sloppy":
                instruction += " Keep the tone professional and concise."
            try:
                corrected = await self.provider.complete(
                    messages=llm_messages
                    + [ChatMessage(role="user", content=instruction)],
                    tools=None,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                )
                regenerated = corrected.content or ""
            except Exception:
                logger.warning("egress regeneration failed", exc_info=True)
                return EGRESS_DONT_KNOW
            # A rewrite is a fresh draft, so it gets the figure guard too --
            # otherwise the judge would be the only thing between a retry and a
            # new invented number.
            reguarded = await _guard_reply(
                self,
                draft=regenerated,
                grounded=grounded,
                llm_messages=llm_messages,
            )
            if reguarded is None:
                return EGRESS_DONT_KNOW
            regenerated = reguarded
            second = await _evaluate(regenerated)
            if second.branch is EgressBranch.SEND:
                return regenerated
            return EGRESS_DONT_KNOW
        return draft

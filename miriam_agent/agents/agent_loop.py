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
* :meth:`Agent._safe_execute` refuses a mutation outright rather than staging
  it, so there is no approval path to talk it into running one,
* the money tools themselves are only reachable through
  ``miriam_agent.hands``, which this module does not import.

Money turns never arrive here at all. ``api/chat.py`` classifies the turn first
and sends anything that touches money to ``orchestrator.py``, whose only path is
Hands -> Judgment -> Hands -> Voice.

The earlier version of this file staged money actions, held an approval ledger
and replayed Go confirmation tokens. All of that is gone: there were two writers
of a balance, and this was the wrong one.
"""

import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from miriam_agent.agents.base import AgentConfig
from miriam_agent.agents.llm import ChatMessage, LLMProvider, get_llm_provider
from miriam_agent.agents.system_prompt import build_system_prompt
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
from miriam_agent.safety import grounding
from miriam_agent.safety.money_tools import MONEY_TOOL_NAMES
from miriam_agent.safety.policy import SafetyPolicy
from miriam_agent.utils.text import (
    bubble_sets,
    clean_text,
    lift_reaction,
)

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


MAX_TOOL_ROUNDS = 5

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
    return {"name": name, "arguments": args, "trace_id": current_trace_id()}


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
        messages = self._build_messages(
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

        for _round in range(MAX_TOOL_ROUNDS):
            llm_messages = list(messages) + llm_extra

            schemas = self.registry.llm_schemas()
            response = await self.provider.complete(
                messages=llm_messages,
                tools=schemas,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )

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
                    grounded=self._grounding_corpus(
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
                        self._tool_message(
                            call.get("id"), name, self._unknown_tool_result(name)
                        )
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
                        self._tool_message(
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
                    result = await self._safe_execute(
                        name, args, ctx, user_id, user_context
                    )
                    llm_extra.append(self._tool_message(call.get("id"), name, result))
                    raw_tool_results.append({"name": name, "result": result})
                except Exception as e:
                    llm_extra.append(
                        self._tool_message(call.get("id"), name, {"error": str(e)})
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

        messages = self._build_messages(
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
                grounded=self._grounding_corpus(
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
                    result = self._unknown_tool_result(name)
                    llm_extra.append(self._tool_message(call.get("id"), name, result))
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
                    llm_extra.append(self._tool_message(call.get("id"), name, result))
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "error",
                        "result": result,
                    }
                    continue

                try:
                    result = await self._safe_execute(
                        name, args, ctx, user_id, user_context
                    )
                    llm_extra.append(self._tool_message(call.get("id"), name, result))
                    raw_tool_results.append({"name": name, "result": result})
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "success",
                        "result": result,
                    }
                except Exception as e:
                    llm_extra.append(
                        self._tool_message(call.get("id"), name, {"error": str(e)})
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

    def _build_messages(
        self,
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
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=system_prompt)
        ]
        for h in history or []:
            role = h.get("role")
            content = h.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append(ChatMessage(role=role, content=content))
        messages.append(ChatMessage(role="user", content=message))
        return messages

    @staticmethod
    def _unknown_tool_result(name: str) -> dict[str, Any]:
        """What a model gets when it asks for a tool that is not available.

        A money tool names itself, so the refusal can say what it was and why,
        rather than the generic "unknown tool" a model might try to work around.
        """
        if name in MONEY_TOOL_NAMES:
            return {"error": _MONEY_TOOL_REFUSAL.format(name=name), "_blocked": True}
        return {"error": f"unknown tool {name}"}

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
        guarded = await self._guard_reply(
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
            reguarded = await self._guard_reply(
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

    @staticmethod
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

    @staticmethod
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
            parts.extend(Agent._grounding_parts(block, label=label))
        for result in tool_results or []:
            parts.extend(Agent._grounding_parts(result, label="tool_result"))
        return "\n".join(part for part in parts if part)

    @staticmethod
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
        self,
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
        problems = self._reply_problems(draft, grounded)
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
            corrected = await self.provider.complete(
                messages=llm_messages
                + [ChatMessage(role="user", content=EGRESS_REGENERATE_INSTRUCTION)],
                tools=None,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
            regenerated = corrected.content or ""
        except Exception:
            logger.warning("reply regeneration failed", exc_info=True)
            for problem in problems:
                record_reply_guard(_rule_of(problem), "fell_back")
            return None
        remaining = self._reply_problems(regenerated, grounded) if regenerated else []
        if not regenerated or remaining:
            for problem in remaining or problems:
                record_reply_guard(_rule_of(problem), "fell_back")
            return None
        record_reply_guard("none", "retried")
        return regenerated

    async def _safe_execute(
        self,
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

        tool = self.registry.get(name)
        if tool is None:
            return self._unknown_tool_result(name)
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
        allowed = await self.safety_policy.validate_action(
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

        return await self.registry.execute(name, args, context=ctx)

    @staticmethod
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
